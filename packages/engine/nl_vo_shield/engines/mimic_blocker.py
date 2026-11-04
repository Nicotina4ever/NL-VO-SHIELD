"""Mimic Blocker anti-cloning adapter (pretrained generator, no training)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import paths
from ..audio_io import AudioBuffer, load_audio, write_audio_atomic
from ..errors import EngineExecutionError, EngineUnavailable
from .base import BaseEngine, EngineContext


class MimicBlockerEngine(BaseEngine):
    id = "mimic_blocker"
    name = "Mimic Blocker"
    kind = "anti_clone"
    version = "pretrained"
    capabilities = ["perturb"]
    requires = ["Mimic Blocker source", "generator_wavlm.pth", "torch runtime"]
    requires_download = False
    quality_note = (
        "Pretrained Mimic Blocker generator on CPU/GPU. The official inference adds "
        "0.7 * noise; intensity is bounded and output is attenuated to avoid clipping. "
        "Efficacy is measured only by cloning tests."
    )
    description = (
        "Mimic Blocker pretrained perturbation against TTS/voice conversion; applies "
        "the official generator_wavlm.pth without training."
    )
    impact = "Adds a learned perturbation that degrades cloning-oriented speech representations."
    limitations = [
        "External source/checkpoint required; not redistributed.",
        "CPU works; GPU is optional and depends on the shared torch runtime.",
        "Efficacy is not claimed from the perturbation alone.",
    ]
    recommended_levels = ["medium", "strong"]
    incompatible_with = ["safespeech", "enkidu", "e2e_vguard", "spectral_cloak"]

    def __init__(self, context: Any) -> None:
        self.context = context
        self._directory = self._find_directory()

    def _data_dir(self) -> Path:
        ctx = getattr(self, "context", None)
        if isinstance(ctx, EngineContext):
            return ctx.data_dir
        data = getattr(ctx, "data_dir", None)
        return Path(data) if data else paths.data_dir()

    def _find_directory(self) -> Path | None:
        candidates = []
        if os.environ.get("NLVS_MIMIC_DIR"):
            candidates.append(Path(os.environ["NLVS_MIMIC_DIR"]).expanduser())
        candidates += [
            self._data_dir() / "components" / "mimic_blocker",
        ]
        for candidate in candidates:
            if (candidate / "model" / "model.py").is_file():
                return candidate.resolve()
            if (candidate / "model.py").is_file():
                return candidate.resolve()
        return None

    def _model_dir(self) -> Path | None:
        d = self._directory
        if d is None:
            return None
        for candidate in (d / "model", d):
            if (candidate / "model.py").is_file():
                return candidate
        return None

    def _checkpoint(self) -> Path | None:
        d = self._directory
        if d is None:
            return None
        for candidate in (
            d / "model" / "checkpoints" / "generator_wavlm.pth",
            d / "checkpoints" / "generator_wavlm.pth",
        ):
            if candidate.is_file():
                return candidate
        return None

    def _python(self) -> Path | None:
        override = os.environ.get("NLVS_MIMIC_PYTHON")
        candidates = [Path(override).expanduser()] if override else []
        candidates += [
            self._data_dir() / "runtime" / "ai-bootstrap" / "venv" / "Scripts" / "python.exe",
            self._data_dir() / "runtime" / "ai-bootstrap" / "venv" / "python.exe",
            self._data_dir() / "runtime" / "ai" / "venv" / "Scripts" / "python.exe",
            self._data_dir() / "runtime" / "ai" / "venv" / "python.exe",
            self._data_dir() / "runtime" / "ai" / "venv" / "bin" / "python3",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _runner(self) -> Path | None:
        candidates = [
            Path(__file__).parent.parent / "mimic_runner.py",
            self._data_dir() / "components" / "mimic_blocker" / "mimic_runner.py",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def availability(self) -> tuple[bool, str | None]:
        if self._directory is None or self._model_dir() is None:
            return False, (
                "Mimic Blocker source not found. Set NLVS_MIMIC_DIR or install it in "
                "<data>/components/mimic_blocker."
            )
        if self._checkpoint() is None:
            return False, "generator_wavlm.pth not found in the Mimic Blocker component."
        if self._python() is None:
            return False, "Shared torch runtime not found; install the AI runtime first."
        if self._runner() is None:
            return False, "Mimic Blocker runner missing from this build."
        return True, None

    def path(self) -> str | None:
        return str(self._directory) if self._directory else None

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "perturb":
            raise EngineExecutionError(f"Mimic Blocker supports only 'perturb', not '{action}'")
        available, reason = self.availability()
        if not available:
            raise EngineUnavailable(reason or "Mimic Blocker unavailable")
        assert self._model_dir() and self._checkpoint() and self._python() and self._runner()
        level = str(params.get("strength", params.get("level", "medium"))).lower()
        mix = float(params.get("mix", {"low": 0.25, "medium": 0.5, "high": 0.7}.get(level, 0.5)))
        mix = max(0.0, min(1.0, mix))
        target_snr = float(params.get("target_snr_db", {"low": 26.0, "medium": 20.0, "high": 16.0}.get(level, 20.0)))
        target_snr = max(6.0, min(40.0, target_snr))
        device = str(params.get("device") or "auto").lower()
        work = Path(tempfile.mkdtemp(prefix="nlvs_mimic_", dir=str(ctx.work_dir) if ctx.work_dir else None))
        source, output = work / "input.wav", work / "protected.wav"
        write_audio_atomic(buffer, source, subtype="PCM_24", temp_dir=ctx.work_dir)
        command = [
            str(self._python()),
            str(self._runner()),
            "--input", str(source),
            "--output", str(output),
            "--model-dir", str(self._model_dir()),
            "--checkpoint", str(self._checkpoint()),
            "--mix", str(mix),
            "--target-snr-db", str(target_snr),
            "--device", device,
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=3600, env=dict(os.environ))
        if proc.returncode != 0 or not output.is_file():
            raise EngineExecutionError(
                f"Mimic Blocker runner failed ({proc.returncode}): "
                + (proc.stderr or proc.stdout or "")[-1200:]
            )
        protected, _ = load_audio(output, temp_dir=ctx.work_dir)
        return protected, {
            "engine": self.id,
            "action": action,
            "strength": level,
            "mix": mix,
            "target_snr_db": target_snr,
            "device": device,
            "log": (proc.stdout or "").strip().splitlines()[-1:],
        }

    def verify_effect(self, action: str, buffer: AudioBuffer | None, params: dict[str, Any], ctx: EngineContext):
        import numpy as np

        before = params.get("_input_buffer")
        if before is None or buffer is None:
            return {"check": "mimic_blocker.perturbation", "ok": False, "detail": {}}
        a, b = np.asarray(before.samples), np.asarray(buffer.samples)
        n = min(len(a), len(b))
        d = b[:n] - a[:n]
        return {
            "check": "mimic_blocker.perturbation",
            "ok": float(np.abs(d).max()) > 1e-6,
            "detail": {"max_abs_delta": float(np.abs(d).max()), "samples": int(n)},
        }
