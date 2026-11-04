"""E2E-VGuard adapter.

The official repository already provides a single-file protection CLI.  The
adapter executes that CLI in the isolated runtime and returns its protected WAV;
it does not modify or retrain the original method.
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

_REQUIRED = ("protect.py", "E2E_VGuard.py", "checkpoints/pretrained_ljs.pth")


class E2EVGuardEngine(BaseEngine):
    id = "e2e_vguard"
    name = "E2E-VGuard"
    kind = "anti_clone"
    version = "official"
    capabilities = ["perturb"]
    requires = ["E2E-VGuard source", "pretrained_ljs.pth and encoder checkpoints", "isolated runtime"]
    requires_download = False
    quality_note = "Official E2E-VGuard single-WAV protection. Efficacy is not claimed from perturbation alone."
    description = "E2E-VGuard perturbation against end-to-end TTS cloning (timbre and pronunciation)."
    impact = "Optimizes a psychoacoustically concealed perturbation against end-to-end TTS."
    limitations = [
        "External source and checkpoints required; not redistributed.",
        "The official CLI is CUDA-oriented; CPU is used only if its code supports it.",
    ]
    recommended_levels = ["strong"]
    incompatible_with = ["safespeech", "mimic_blocker", "enkidu", "spectral_cloak"]

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
        if os.environ.get("NLVS_E2E_DIR"):
            candidates.append(Path(os.environ["NLVS_E2E_DIR"]).expanduser())
        candidates.append(self._data_dir() / "components" / "e2e_vguard")
        for candidate in candidates:
            if (candidate / "protect.py").is_file():
                return candidate.resolve()
        return None

    def _python(self) -> Path | None:
        override = os.environ.get("NLVS_E2E_PYTHON")
        candidates = [Path(override).expanduser()] if override else []
        candidates += [
            self._data_dir() / "micromamba" / "envs" / "e2e" / "python.exe",
            self._data_dir() / "micromamba" / "envs" / "e2e" / "bin" / "python3",
            self._data_dir() / "runtime" / "ai" / "venv" / "Scripts" / "python.exe",
            self._data_dir() / "runtime" / "ai" / "venv" / "bin" / "python3",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _missing(self) -> list[str]:
        if self._directory is None:
            return list(_REQUIRED)
        return [rel for rel in _REQUIRED if not (self._directory / rel).is_file()]

    def availability(self) -> tuple[bool, str | None]:
        if self._directory is None:
            return False, "E2E-VGuard source not found in <data>/components/e2e_vguard or NLVS_E2E_DIR."
        missing = self._missing()
        if missing:
            return False, "E2E-VGuard checkpoints missing: " + ", ".join(missing)
        if self._python() is None:
            return False, "Isolated runtime not found for E2E-VGuard."
        return True, None

    def path(self) -> str | None:
        return str(self._directory) if self._directory else None

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "perturb":
            raise EngineExecutionError(f"E2E-VGuard supports only 'perturb', not '{action}'")
        available, reason = self.availability()
        if not available:
            raise EngineUnavailable(reason or "E2E-VGuard unavailable")
        assert self._directory and self._python()
        epochs = int(params.get("epochs", 50))
        epsilon = int(params.get("epsilon", 8))
        work = Path(tempfile.mkdtemp(prefix="nlvs_e2e_", dir=str(ctx.work_dir) if ctx.work_dir else None))
        source = work / "input.wav"
        # E2E-VGuard's official single-file path expects 24 kHz mono.
        if buffer.sample_rate != 24000 or buffer.channels != 1:
            import numpy as np
            from scipy.signal import resample_poly
            mono = buffer.samples.mean(axis=1).astype(np.float32)
            if buffer.sample_rate != 24000:
                mono = resample_poly(mono, 24000, buffer.sample_rate).astype(np.float32)
            import soundfile as sf
            sf.write(source, mono, 24000, subtype="PCM_24")
        else:
            write_audio_atomic(buffer, source, subtype="PCM_24", temp_dir=ctx.work_dir)
        command = [str(self._python()), str(self._directory / "protect.py"),
                   "--input_wav", str(source), "--epsilon", str(epsilon), "--epochs", str(epochs)]
        env = dict(os.environ)
        # E2E's StyleTTS2 phonemizer needs the installed eSpeak NG library.
        local_espeak = self._directory / "espeak"
        espeak = local_espeak if local_espeak.is_dir() else Path(r"C:\Program Files\eSpeak NG")
        if espeak.is_dir():
            env.setdefault("PHONEMIZER_ESPEAK_LIBRARY", str(espeak / "libespeak-ng.dll"))
            env.setdefault("PHONEMIZER_ESPEAK_PATH", str(espeak / "espeak-ng-data"))
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        env["HF_HUB_OFFLINE"] = "0"
        env["TRANSFORMERS_OFFLINE"] = "0"
        env["PYTHONUTF8"] = "1"
        if self._python() is not None:
            envroot = self._python().parent
            env["PATH"] = os.pathsep.join([str(envroot), str(envroot / "bin"), str(envroot / "Library" / "bin"), str(envroot / "Scripts"), env.get("PATH", "")])
        device = str(params.get("device") or "").lower()
        if device in ("cpu", "cuda"):
            env["NLVS_E2E_DEVICE"] = device
        proc = subprocess.run(command, cwd=str(self._directory), capture_output=True, text=True,
                              timeout=7200, env=env)
        candidates = [source.with_name(source.stem + "_protected.wav"),
                      source.parent / (source.stem + "_protected.wav")]
        output = next((p for p in candidates if p.is_file()), None)
        if proc.returncode != 0 or output is None:
            raise EngineExecutionError(
                f"E2E-VGuard runner failed ({proc.returncode}): " + (proc.stderr or proc.stdout or "")[-1200:]
            )
        protected, _ = load_audio(output, temp_dir=ctx.work_dir)
        return protected, {"engine": self.id, "action": action, "epochs": epochs, "epsilon": epsilon,
                           "log": (proc.stdout or "").strip().splitlines()[-1:]}

    def verify_effect(self, action: str, buffer: AudioBuffer | None, params: dict[str, Any], ctx: EngineContext):
        import numpy as np

        before = params.get("_input_buffer")
        if before is None or buffer is None:
            return {"check": "e2e_vguard.perturbation", "ok": False, "detail": {}}
        a, b = np.asarray(before.samples), np.asarray(buffer.samples)
        n = min(len(a), len(b))
        d = b[:n] - a[:n]
        return {"check": "e2e_vguard.perturbation", "ok": float(np.abs(d).max()) > 1e-6,
                "detail": {"max_abs_delta": float(np.abs(d).max()), "samples": int(n)}}
