"""Enkidu UFP adapter (official core, reusable cached profiles)."""

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


class EnkiduEngine(BaseEngine):
    id = "enkidu"
    name = "Enkidu (UFP)"
    kind = "anti_clone"
    version = "ufp"
    capabilities = ["perturb"]
    requires = ["Enkidu source", "ECAPA speaker model", "torch runtime"]
    requires_download = False
    quality_note = (
        "Universal Frequential Perturbation. A UFP profile is optimized once and reused; "
        "this is not model training. Efficacy is measured only with cloning tests."
    )
    description = (
        "Enkidu universal frequential perturbation against TTS/voice conversion, with "
        "reusable UFP profiles."
    )
    impact = "Adds a frequency-domain perturbation that degrades speaker embeddings used by cloners."
    limitations = [
        "The first profile requires a one-time optimization (managed by the app).",
        "External source and ECAPA assets are required; not redistributed.",
        "Efficacy is not claimed from the perturbation alone.",
    ]
    recommended_levels = ["medium", "strong"]
    incompatible_with = ["safespeech", "mimic_blocker", "e2e_vguard", "spectral_cloak"]

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
        if os.environ.get("NLVS_ENKIDU_DIR"):
            candidates.append(Path(os.environ["NLVS_ENKIDU_DIR"]).expanduser())
        candidates.append(self._data_dir() / "components" / "enkidu")
        for candidate in candidates:
            if (candidate / "core" / "enkidu.py").is_file():
                return candidate.resolve()
        return None

    def _python(self) -> Path | None:
        override = os.environ.get("NLVS_ENKIDU_PYTHON")
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
        for candidate in (
            Path(__file__).parent.parent / "enkidu_runner.py",
            self._data_dir() / "components" / "enkidu" / "enkidu_runner.py",
        ):
            if candidate.is_file():
                return candidate
        return None

    def _ecapa(self) -> Path | None:
        candidates = [
            self._data_dir() / "upstream_assets" / "shared" / "ECAPA",
        ]
        override = os.environ.get("NLVS_ECAPA_DIR")
        if override:
            candidates.insert(0, Path(override).expanduser())
        for candidate in candidates:
            if (candidate / "embedding_model.ckpt").is_file():
                return candidate.resolve()
        return None

    def availability(self) -> tuple[bool, str | None]:
        if self._directory is None:
            return False, "Enkidu source not found in <data>/components/enkidu or NLVS_ENKIDU_DIR."
        if self._python() is None:
            return False, "Torch runtime not found; install the AI runtime first."
        if self._runner() is None:
            return False, "Enkidu runner missing from this build."
        if self._ecapa() is None:
            return False, "ECAPA assets not found; set NLVS_ECAPA_DIR or install them in <data>/upstream_assets."
        return True, None

    def path(self) -> str | None:
        return str(self._directory) if self._directory else None

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "perturb":
            raise EngineExecutionError(f"Enkidu supports only 'perturb', not '{action}'")
        available, reason = self.availability()
        if not available:
            raise EngineUnavailable(reason or "Enkidu unavailable")
        assert self._directory and self._python() and self._runner() and self._ecapa()
        steps = int(params.get("steps", 10))
        target_snr = float(params.get("target_snr_db", 24.0))
        target_snr = max(6.0, min(40.0, target_snr))
        noise_level = float(params.get("noise_level", 0.1))
        mask_ratio = float(params.get("mask_ratio", 0.3))
        profile = str(params.get("profile") or "default")
        device = str(params.get("device") or "auto").lower()
        work = Path(tempfile.mkdtemp(prefix="nlvs_enkidu_", dir=str(ctx.work_dir) if ctx.work_dir else None))
        source, output = work / "input.wav", work / "protected.wav"
        write_audio_atomic(buffer, source, subtype="PCM_24", temp_dir=ctx.work_dir)
        command = [
            str(self._python()), str(self._runner()),
            "--input", str(source), "--output", str(output),
            "--enkidu-dir", str(self._directory),
            "--ecapa-dir", str(self._ecapa()),
            "--profile-dir", str(self._data_dir() / "profiles"),
            "--profile", profile,
            "--device", device,
            "--steps", str(steps),
            "--noise-level", str(noise_level),
            "--mask-ratio", str(mask_ratio),
            "--target-snr-db", str(target_snr),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=7200, env=dict(os.environ))
        if proc.returncode != 0 or not output.is_file():
            raise EngineExecutionError(
                f"Enkidu runner failed ({proc.returncode}): " + (proc.stderr or proc.stdout or "")[-1200:]
            )
        protected, _ = load_audio(output, temp_dir=ctx.work_dir)
        if protected.sample_rate != buffer.sample_rate:
            try:
                import numpy as np
                from scipy.signal import resample_poly

                factor = buffer.sample_rate // protected.sample_rate
                if factor > 0:
                    samples = resample_poly(protected.samples.astype(np.float32), factor, 1, axis=0)
                    protected = AudioBuffer(samples, buffer.sample_rate)
            except Exception:
                pass
        return protected, {
            "engine": self.id, "action": action, "profile": profile,
            "reused": "true" if (self._data_dir() / "profiles" / "enkidu" / profile / "noise_real.pt").exists() else "false",
            "steps": steps, "noise_level": noise_level, "target_snr_db": target_snr, "device": device,
            "log": (proc.stdout or "").strip().splitlines()[-1:],
        }

    def verify_effect(self, action: str, buffer: AudioBuffer | None, params: dict[str, Any], ctx: EngineContext):
        import numpy as np

        before = params.get("_input_buffer")
        if before is None or buffer is None:
            return {"check": "enkidu.perturbation", "ok": False, "detail": {}}
        a, b = np.asarray(before.samples), np.asarray(buffer.samples)
        n = min(len(a), len(b))
        d = b[:n] - a[:n]
        return {
            "check": "enkidu.perturbation",
            "ok": float(np.abs(d).max()) > 1e-6,
            "detail": {"max_abs_delta": float(np.abs(d).max()), "samples": int(n)},
        }
