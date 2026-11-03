"""SafeSpeech official anti-cloning engine adapter.

The official research code is not bundled. The user supplies/installs it and a
Python runtime (CPU or CUDA) that can import its dependencies. This adapter runs
the official ``protect.py`` perturbation loop through a small single-WAV runner;
it never trains a model and never substitutes generic noise.
"""

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

_EXPECTED_WEIGHTS = (
    "checkpoints/base_models/G_0.pth",
    "checkpoints/base_models/D_0.pth",
    "checkpoints/base_models/DUR_0.pth",
    "checkpoints/base_models/WD_0.pth",
)


class SafeSpeechEngine(BaseEngine):
    id = "safespeech"
    name = "SafeSpeech (official)"
    kind = "anti_clone"
    version = "official"
    capabilities = ["perturb"]
    requires = [
        "SafeSpeech official source (external component)",
        "official checkpoints G/D/DUR/WD",
        "isolated Python runtime with SafeSpeech dependencies",
    ]
    requires_download = False
    quality_note = (
        "Official SafeSpeech perturbation. CPU works but is much slower; CUDA is used "
        "when the selected isolated runtime has CUDA. Anti-cloning efficacy is not "
        "claimed from the perturbation alone."
    )
    description = (
        "SafeSpeech (USENIX Security 2025) official perturbation against TTS/voice "
        "conversion, executed on a single WAV with automatically prepared features."
    )
    impact = "Perturbs the waveform using the official surrogate objective to reduce cloning fidelity."
    limitations = [
        "External source and checkpoints are required; not redistributed.",
        "CPU mode is slower than CUDA but is not a different, partial algorithm.",
        "The reference transcript/phone sequence is currently a fixed minimal sequence; "
        "the official surrogate objective is unchanged.",
    ]
    recommended_levels = ["medium", "strong"]
    incompatible_with = ["spectral_cloak", "mimic_blocker", "enkidu", "e2e_vguard"]

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
        override = os.environ.get("NLVS_SAFESPEECH_DIR")
        candidates = [Path(override).expanduser()] if override else []
        found = paths.find_safespeech_dir()
        if found is not None:
            candidates.append(found)
        candidates += [
            self._data_dir() / "components" / "safespeech",
            self._data_dir().parent / "tools" / "SafeSpeech",
        ]
        for candidate in candidates:
            if (candidate / "protect.py").is_file():
                return candidate.resolve()
        return None

    def _python(self) -> Path | None:
        override = os.environ.get("NLVS_SAFESPEECH_PYTHON")
        candidates = [Path(override).expanduser()] if override else []
        candidates += [
            self._data_dir() / "runtime" / "safespeech" / "venv" / "Scripts" / "python.exe",
            self._data_dir() / "runtime" / "safespeech" / "venv" / "bin" / "python3",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _runner(self) -> Path | None:
        candidate = Path(__file__).parent.parent / "safespeech_runner.py"
        return candidate if candidate.is_file() else None

    def _weight_status(self) -> tuple[list[str], list[str]]:
        if self._directory is None:
            return [], list(_EXPECTED_WEIGHTS)
        found, missing = [], []
        for relative in _EXPECTED_WEIGHTS:
            (found if (self._directory / relative).is_file() else missing).append(relative)
        return found, missing

    def availability(self) -> tuple[bool, str | None]:
        if self._directory is None:
            return False, (
                "SafeSpeech official source not found. Set NLVS_SAFESPEECH_DIR or install "
                "the component in <data>/components/safespeech."
            )
        found, missing = self._weight_status()
        if missing:
            return False, (
                "SafeSpeech checkpoints incomplete: missing "
                + ", ".join(missing)
                + ". Install the official checkpoint set."
            )
        if self._python() is None:
            return False, (
                "SafeSpeech isolated runtime not found. Set NLVS_SAFESPEECH_PYTHON or "
                "install it in <data>/runtime/safespeech/venv."
            )
        if self._runner() is None:
            return False, "SafeSpeech single-WAV runner is missing from this build."
        return True, None

    def path(self) -> str | None:
        return str(self._directory) if self._directory else None

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "perturb":
            raise EngineExecutionError(f"SafeSpeech supports only 'perturb', not '{action}'")
        available, reason = self.availability()
        if not available:
            raise EngineUnavailable(reason or "SafeSpeech unavailable")
        assert self._directory is not None and self._python() is not None and self._runner() is not None
        epochs = int(params.get("epochs", params.get("perturbation_epochs", 10)))
        epsilon = int(params.get("epsilon", 8))
        device = str(params.get("device") or "auto").lower()
        mode = str(params.get("mode") or "SPEC")
        if device not in ("cpu", "cuda", "auto"):
            device = "auto"
        work = Path(tempfile.mkdtemp(prefix="nlvs_ss_", dir=str(ctx.work_dir) if ctx.work_dir else None))
        source = work / "input.wav"
        output = work / "protected.wav"
        write_audio_atomic(buffer, source, subtype="PCM_24", temp_dir=ctx.work_dir)
        command = [
            str(self._python()),
            str(self._runner()),
            "--input", str(source),
            "--output", str(output),
            "--device", device,
            "--epochs", str(epochs),
            "--epsilon", str(epsilon),
            "--mode", mode,
            "--official-dir", str(self._directory),
            "--checkpoint-dir", str(self._directory / "checkpoints"),
        ]
        env = dict(os.environ)
        env["NLVS_SAFESPEECH_DIR"] = str(self._directory)
        env.setdefault("PYTHONUTF8", "1")
        proc = subprocess.run(
            command,
            cwd=str(self._directory),
            capture_output=True,
            text=True,
            timeout=7200,
            env=env,
        )
        if proc.returncode != 0 or not output.is_file():
            tail = (proc.stderr or proc.stdout or "")[-1600:]
            raise EngineExecutionError(
                f"SafeSpeech runner failed ({proc.returncode}): {tail}",
                detail={"returncode": proc.returncode},
            )
        protected, _info = load_audio(output, temp_dir=ctx.work_dir)
        record = {
            "engine": self.id,
            "action": action,
            "device": device,
            "epochs": epochs,
            "epsilon": epsilon,
            "mode": mode,
            "official_dir": str(self._directory),
            "log": (proc.stdout or "").strip().splitlines()[-1:],
        }
        return protected, record

    def verify_effect(self, action: str, buffer: AudioBuffer | None, params: dict[str, Any], ctx: EngineContext):
        before = params.get("_input_buffer")
        if before is None or buffer is None:
            return {"check": "safespeech.perturbation", "ok": False, "detail": {"reason": "missing buffers"}}
        import numpy as np

        a = np.asarray(before.samples, dtype=np.float32)
        b = np.asarray(buffer.samples, dtype=np.float32)
        n = min(a.shape[0], b.shape[0])
        if n == 0:
            return {"check": "safespeech.perturbation", "ok": False, "detail": {"reason": "empty"}}
        delta = b[:n] - a[:n]
        max_delta = float(np.abs(delta).max())
        signal = float(np.sum(a[:n] * a[:n]))
        noise = float(np.sum(delta * delta))
        snr = None if noise <= 1e-20 else float(10.0 * np.log10(max(signal, 1e-20) / noise))
        return {
            "check": "safespeech.perturbation",
            "ok": max_delta > 1e-5,
            "detail": {"max_abs_delta": max_delta, "snr_db": snr, "samples": int(n)},
        }
