"""Optional, isolated AI runtime for NL VO Shield.

Design rules:
* the base installer ships a small embedded CPython ("bootstrap") inside
  ``<install>/engine/bootstrap`` together with ``get-pip.py``; nothing ever uses
  the system Python;
* on an explicit user click the core creates an isolated virtual environment
  under ``<data>/runtime/ai`` with that embedded Python and installs the
  optional packages (torch/torchaudio/audioseal/soundfile) there;
* AudioSeal checkpoints are downloaded only through the existing, explicit,
  SHA-256 pinned ModelStore into ``<data>/models``;
* the frozen engine never imports torch: it runs a worker script with the
  runtime interpreter through subprocess JSON.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .util import dir_size, human_bytes, utc_now_iso

ProgressCallback = Callable[[int | None, str, int, int | None], None]

RUNTIME_VERSION = "1"
AUDIOSEAL_MODELS = ("audioseal-generator", "audioseal-detector")

_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_CUDA_INDEX = "https://download.pytorch.org/whl/cu124"


def _has_nvidia() -> tuple[bool, str | None]:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return True, completed.stdout.strip().splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return False, None


def bootstrap_python() -> Path:
    """Locate the embedded installation Python (never the system Python)."""
    override = os.environ.get("NLVS_BOOTSTRAP_PYTHON")
    if override and Path(override).is_file():
        return Path(override)
    exe = Path(sys.executable).resolve()
    candidates: list[Path] = []
    for parent in exe.parents:
        candidates.append(parent / "engine" / "bootstrap")
        candidates.append(parent / "bootstrap")
        if parent.name == "engine":
            candidates.append(parent / "bootstrap")
    windows = platform.system() == "Windows"
    for base in candidates:
        for relative in ("python/python.exe", "python/bin/python3", "python.exe", "bin/python3"):
            candidate = base / relative
            if candidate.is_file():
                return candidate
    raise RuntimeError(
        "Python embedded di bootstrap non trovato dentro l'installazione "
        "(engine/bootstrap). Reinstalla l'applicazione: non viene mai usato il Python di sistema."
    )


def bootstrap_get_pip() -> Path | None:
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        candidate = parent / "engine" / "bootstrap" / "get-pip.py"
        if candidate.is_file():
            return candidate
    return None


@dataclass
class InstallResult:
    ok: bool
    message: str
    freed_bytes: int = 0
    detail: dict[str, Any] | None = None


class AIRuntime:
    """Manager for the optional AudioSeal AI runtime (install/verify/remove)."""

    def __init__(self, data_dir: Path, models: Any) -> None:
        self.data_dir = Path(data_dir)
        self.models = models
        self.root = self.data_dir / "runtime" / "ai"
        self.venv = self.root / "venv"
        self.logs = self.data_dir / "logs"
        self.worker = self.root / "worker" / "audioseal_worker.py"
        self.state_file = self.root / "state.json"

    # ------------------------------------------------------------------ utils
    @property
    def python(self) -> Path | None:
        if platform.system() == "Windows":
            candidate = self.venv / "Scripts" / "python.exe"
        else:
            candidate = self.venv / "bin" / "python3"
        return candidate if candidate.is_file() else None

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.state_file)

    def _log(self, text: str) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        with (self.logs / "ai-runtime.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[{utc_now_iso()}] {text}\n")

    def installed(self) -> bool:
        return bool(self.python and self.worker.is_file() and self._read_state().get("version"))

    def status(self) -> dict[str, Any]:
        gpu, gpu_name = _has_nvidia()
        model_state: dict[str, Any] = {}
        try:
            for item in self.models.list(verify_hash=False):
                if item.get("id") in AUDIOSEAL_MODELS:
                    model_state[item["id"]] = {
                        "present": item.get("present"),
                        "size_bytes": item.get("size_bytes"),
                        "integrity": item.get("integrity"),
                        "path": item.get("path"),
                    }
        except Exception as exc:  # pragma: no cover - defensive
            model_state = {"error": str(exc)}
        state = self._read_state()
        return {
            "id": "ai-runtime",
            "name": "Runtime AI (AudioSeal)",
            "version": state.get("version", RUNTIME_VERSION),
            "installed": self.installed(),
            "state": "available" if self.installed() else ("error" if state.get("error") else "not_installed"),
            "path": str(self.root),
            "python": str(self.python) if self.python else None,
            "bootstrap_python": str(bootstrap_python()) if _bootstrap_exists() else None,
            "variant": state.get("variant"),
            "device": state.get("device"),
            "size_bytes": _dir_bytes(self.root),
            "installed_at": state.get("installed_at"),
            "error": state.get("error"),
            "expected_download_bytes": _expected_download_bytes(state.get("variant")),
            "variants": self.variants(),
            "gpu": {"available": gpu, "name": gpu_name},
            "models": model_state,
            "models_ready": self.models_ready(),
            "platform": {"os": platform.system().lower(), "arch": platform.machine().lower()},
        }

    def variants(self) -> list[dict[str, Any]]:
        gpu, gpu_name = _has_nvidia()
        items = [
            {
                "id": "cpu",
                "label": "CPU",
                "available": True,
                "description": "Funziona su qualsiasi PC; più lento ma senza requisiti GPU.",
                "expected_download_bytes": _expected_download_bytes("cpu"),
                "device": "cpu",
            }
        ]
        items.append(
            {
                "id": "gpu",
                "label": "GPU (CUDA)",
                "available": gpu,
                "description": (
                    f"CUDA rilevata: {gpu_name}. Richiede driver NVIDIA e molto spazio."
                    if gpu
                    else "Nessuna GPU NVIDIA/CUDA rilevata: usa CPU."
                ),
                "expected_download_bytes": _expected_download_bytes("gpu"),
                "device": "cuda",
            }
        )
        return items

    def models_ready(self) -> bool:
        try:
            ready, _ = self.models.is_ready("audioseal")
            return bool(ready)
        except Exception:
            return False

    # ------------------------------------------------------------------ install
    def install(
        self,
        variant: str = "cpu",
        *,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        variant = "gpu" if str(variant).lower().startswith("gpu") else "cpu"
        self.root.mkdir(parents=True, exist_ok=True)
        emit = progress or (lambda *args: None)
        try:
            embedded = bootstrap_python()
        except RuntimeError as exc:
            self._write_state({"version": RUNTIME_VERSION, "error": str(exc), "variant": variant})
            raise RuntimeError(str(exc)) from exc
        if variant == "gpu" and not _has_nvidia():
            raise RuntimeError("GPU richiesta ma nessuna GPU NVIDIA/CUDA rilevata (nvidia-smi assente).")

        started = time.time()
        try:
            emit(2, f"Python embedded: {embedded}", 0, None)
            self._ensure_venv(embedded, emit, cancel)
            python = self.python
            if python is None:
                raise RuntimeError("Creazione del virtualenv fallita: interprete non trovato.")
            self._ensure_pip(python, emit, cancel)
            self._pip_install_torch(python, variant, emit, cancel)
            self._pip_install_audioseal(python, emit, cancel)
            self._write_worker(emit)
            self._download_models(emit, cancel)
            emit(96, "verifica del modello AudioSeal", 0, None)
            verification = self.verify()
            if not verification.get("ok"):
                raise RuntimeError(
                    "Verifica AudioSeal fallita: " + str(verification.get("message") or "errore sconosciuto")
                )
            state = {
                "version": RUNTIME_VERSION,
                "variant": variant,
                "device": "cuda" if variant == "gpu" else "cpu",
                "installed_at": utc_now_iso(),
                "python": str(python),
                "bootstrap_python": str(embedded),
                "seconds": round(time.time() - started, 1),
                "size_bytes": _dir_bytes(self.root),
                "verification": verification,
            }
            self._write_state(state)
            emit(100, "runtime AI installato e verificato", 0, None)
            return {"ok": True, "state": "available", **state}
        except Exception as exc:
            self._write_state(
                {
                    "version": RUNTIME_VERSION,
                    "error": f"{type(exc).__name__}: {exc}",
                    "variant": variant,
                }
            )
            self._log(f"install failed: {type(exc).__name__}: {exc}")
            raise

    def _ensure_venv(self, embedded: Path, emit: ProgressCallback, cancel: threading.Event | None) -> None:
        if self.python is not None:
            emit(10, "virtualenv già presente", 0, None)
            return
        if self.venv.exists():
            shutil.rmtree(self.venv, ignore_errors=True)
        self._run(
            [str(embedded), "-m", "venv", "--without-pip", str(self.venv)],
            stage=(8, 18),
            label="creazione ambiente isolato",
            emit=emit,
            cancel=cancel,
        )

    def _ensure_pip(self, python: Path, emit: ProgressCallback, cancel: threading.Event | None) -> None:
        probe = subprocess.run(
            [str(python), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            emit(20, "pip disponibile nel runtime", 0, None)
            return
        get_pip = bootstrap_get_pip()
        if get_pip is None:
            raise RuntimeError("pip assente nel virtualenv e get-pip.py non trovato nel bootstrap dell'installazione.")
        self._run(
            [str(python), str(get_pip), "--no-warn-script-location"],
            stage=(18, 24),
            label="preparazione pip",
            emit=emit,
            cancel=cancel,
        )

    def _pip_install_torch(
        self, python: Path, variant: str, emit: ProgressCallback, cancel: threading.Event | None
    ) -> None:
        command = [
            str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
            "--no-warn-script-location",
        ]
        if variant == "gpu":
            command += ["--index-url", _CUDA_INDEX]
        elif platform.system() != "Darwin":
            command += ["--index-url", _CPU_INDEX]
        command += ["torch", "torchaudio"]
        self._run(
            command,
            stage=(24, 70),
            label=f"installazione PyTorch ({variant.upper()})",
            emit=emit,
            cancel=cancel,
        )

    def _pip_install_audioseal(
        self, python: Path, emit: ProgressCallback, cancel: threading.Event | None
    ) -> None:
        self._run(
            [
                str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                "--no-warn-script-location", "audioseal", "soundfile", "numpy", "scipy",
            ],
            stage=(70, 88),
            label="installazione AudioSeal",
            emit=emit,
            cancel=cancel,
        )

    def _download_models(self, emit: ProgressCallback, cancel: threading.Event | None) -> None:
        for index, model_id in enumerate(AUDIOSEAL_MODELS):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Installazione annullata")
            start = 88 + index * 4
            end = start + 4
            emit(start, f"checkpoint {model_id}", 0, None)

            def sub_progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                local = start + int((end - start) * (pct if pct is not None else 0) / 100)
                emit(local, message, downloaded, total)

            self.models.download(model_id, progress=sub_progress, cancel=cancel)

    def _write_worker(self, emit: ProgressCallback) -> None:
        self.worker.parent.mkdir(parents=True, exist_ok=True)
        self.worker.write_text(WORKER_SOURCE, encoding="utf-8")
        emit(90, "worker AudioSeal installato", 0, None)

    # ------------------------------------------------------------------ verify
    def verify(self) -> dict[str, Any]:
        python = self.python
        if python is None or not self.worker.is_file():
            return {"ok": False, "message": "runtime AI non installato"}
        gen, det = self.model_paths()
        if not gen or not det:
            return {"ok": False, "message": "checkpoint AudioSeal mancanti"}
        state = self._read_state()
        device = str(state.get("device") or "cpu")
        completed = subprocess.run(
            [
                str(python), str(self.worker), "selftest",
                "--generator", str(gen), "--detector", str(det),
                "--device", device,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=_worker_env(self),
        )
        payload = _last_json(completed.stdout)
        if completed.returncode != 0 or not payload:
            return {
                "ok": False,
                "message": (completed.stderr or completed.stdout or "worker error").strip()[-800:],
            }
        return payload

    def model_paths(self) -> tuple[Path | None, Path | None]:
        from .models import model_spec

        gen = self.models.path_for(model_spec("audioseal-generator"))
        det = self.models.path_for(model_spec("audioseal-detector"))
        return gen, det

    # ------------------------------------------------------------------ worker
    def run_worker(
        self,
        args: list[str],
        *,
        timeout: float = 600.0,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        python = self.python
        if python is None or not self.worker.is_file():
            raise RuntimeError("Runtime AI AudioSeal non installato")
        state = self._read_state()
        device = str(state.get("device") or "cpu")
        command = [str(python), str(self.worker), *args, "--device", device]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_worker_env(self),
        )
        payload = _last_json(completed.stdout)
        if completed.returncode != 0 or not payload:
            raise RuntimeError(
                f"AudioSeal worker failed: {(completed.stderr or completed.stdout or 'no output').strip()[-1200:]}"
            )
        return payload

    # ------------------------------------------------------------------ uninstall
    def uninstall(self, *, remove_models: bool = False) -> dict[str, Any]:
        from .models import model_spec

        freed = _dir_bytes(self.root)
        removed_models: list[str] = []
        if remove_models:
            for model_id in AUDIOSEAL_MODELS:
                try:
                    spec_path = self.models.path_for(model_spec(model_id))
                    size = spec_path.stat().st_size if spec_path is not None and spec_path.is_file() else 0
                    self.models.delete(model_id)
                    freed += int(size)
                    removed_models.append(model_id)
                except Exception:
                    pass
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        # Runtime-specific caches created by the explicit install; shared model
        # weights are handled above only when the user asked to remove them.
        for relative in ("pip", "torch", "huggingface", "xdg"):
            cache = self.data_dir / "cache" / relative
            if cache.exists():
                freed += _dir_bytes(cache)
                shutil.rmtree(cache, ignore_errors=True)
        return {
            "ok": True,
            "freed_bytes": freed,
            "removed_models": removed_models,
            "runtime_removed": not self.root.exists(),
        }

    # ------------------------------------------------------------------ process
    def _run(
        self,
        command: list[str],
        *,
        stage: tuple[int, int],
        label: str,
        emit: ProgressCallback,
        cancel: threading.Event | None,
    ) -> str:
        start, end = stage
        emit(start, label, 0, None)
        self.logs.mkdir(parents=True, exist_ok=True)
        log_path = self.logs / "ai-runtime.log"
        env = dict(os.environ)
        env.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "PYTHONUTF8": "1",
                "PIP_PROGRESS_BAR": "off",
                "HF_HOME": str(self.data_dir / "cache" / "huggingface"),
                "TORCH_HOME": str(self.data_dir / "cache" / "torch"),
                "XDG_CACHE_HOME": str(self.data_dir / "cache" / "xdg"),
                "PIP_CACHE_DIR": str(self.data_dir / "cache" / "pip"),
                "TMP": str(self.data_dir / "cache" / "tmp"),
                "TEMP": str(self.data_dir / "cache" / "tmp"),
                "TORCHDYNAMO_DISABLE": "1",
                "TORCH_COMPILE_DISABLE": "1",
            }
        )
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n$ {' '.join(command)}\n")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            assert process.stdout is not None
            last_emit = 0.0
            for line in process.stdout:
                log.write(line)
                log.flush()
                if cancel is not None and cancel.is_set():
                    process.kill()
                    raise RuntimeError("Installazione annullata")
                now = time.time()
                if now - last_emit > 0.4:
                    last_emit = now
                    text = line.strip()
                    if text:
                        emit(None, f"{label}: {text[-160:]}", 0, None)
            code = process.wait()
        if code != 0:
            raise RuntimeError(f"{label} fallita (exit {code}). Vedi {log_path}")
        emit(end, f"{label}: completato", 0, None)
        return log_path.read_text(encoding="utf-8", errors="replace")


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    value = dir_size(path)
    return int(value[0]) if isinstance(value, (tuple, list)) else int(value)


def _bootstrap_exists() -> bool:
    try:
        bootstrap_python()
        return True
    except RuntimeError:
        return False


def _worker_env(runtime: "AIRuntime") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "TORCHDYNAMO_DISABLE": "1",
            "TORCH_COMPILE_DISABLE": "1",
            "AUDIOSEAL_CACHE_DIR": str(runtime.data_dir / "models" / "audioseal"),
            "TORCH_HOME": str(runtime.data_dir / "cache" / "torch"),
            "HF_HOME": str(runtime.data_dir / "cache" / "huggingface"),
            "XDG_CACHE_HOME": str(runtime.data_dir / "cache" / "xdg"),
        }
    )
    return env


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    return None


def _expected_download_bytes(variant: str | None) -> int:
    # torch CPU (~200 MB) + torchaudio (~5 MB) + audioseal/soundfile (~20 MB)
    # + AudioSeal checkpoints (~90 MB). CUDA wheels are far larger.
    if variant == "gpu":
        return int(3.2 * 1024**3)
    return int(0.40 * 1024**3)


WORKER_SOURCE = r'''#!/usr/bin/env python3
"""AudioSeal worker executed by the isolated AI runtime (no frozen-engine imports)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")

import numpy as np
import soundfile as sf
import torch
import torchaudio
from audioseal import AudioSeal

RATE = 16000


def _device(name: str):
    if name.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA richiesta ma non disponibile")
    return torch.device(name)


def _bits(payload: int):
    return torch.tensor([[(int(payload) >> bit) & 1 for bit in range(16)]], dtype=torch.int32)


def _load(path: str, kind: str, device):
    if kind == "generator":
        model = AudioSeal.load_generator(path, nbits=16, device=device)
    else:
        model = AudioSeal.load_detector(path, nbits=16, device=device)
    model.eval()
    return model


def _resample(mono: np.ndarray, sr: int, device):
    tensor = torch.from_numpy(mono.astype(np.float32)).unsqueeze(0)
    if sr != RATE:
        tensor = torchaudio.functional.resample(tensor, sr, RATE)
    return tensor.squeeze(0).to(device)


def embed(args) -> None:
    device = _device(args.device)
    generator = _load(args.generator, "generator", device)
    audio, sr = sf.read(args.input, always_2d=True, dtype="float32")
    out = np.array(audio, dtype=np.float32, copy=True)
    for channel in range(audio.shape[1]):
        mono = audio[:, channel]
        tensor = _resample(mono, sr, device).unsqueeze(0).unsqueeze(0)
        if tensor.shape[-1] < int(RATE * 0.3):
            raise RuntimeError("audio troppo corto per AudioSeal (minimo 0.3 s a 16 kHz)")
        with torch.no_grad():
            watermark = generator.get_watermark(tensor, message=_bits(args.payload).to(device))
        wm = watermark.squeeze().detach().cpu().numpy().astype(np.float32)
        wm_tensor = torch.from_numpy(wm).unsqueeze(0)
        if sr != RATE:
            wm_tensor = torchaudio.functional.resample(wm_tensor, RATE, sr)
        wm = wm_tensor.squeeze(0).numpy()
        if wm.shape[0] != audio.shape[0]:
            wm = np.resize(wm, audio.shape[0])
        peak_in = float(np.max(np.abs(mono))) if mono.size else 0.0
        headroom = 0.995 - peak_in
        needed = float(np.max(np.abs(wm))) if wm.size else 0.0
        alpha = min(1.0, headroom / needed) if needed > 1e-9 else 1.0
        if alpha < 0.05:
            raise RuntimeError("headroom insufficiente per il watermark AudioSeal")
        out[:, channel] = mono + np.float32(alpha) * wm
    if float(np.max(np.abs(out))) > 0.999:
        raise RuntimeError("output in clipping: AudioSeal annullato")
    sf.write(args.output, out, sr, subtype=args.subtype or None)
    print(json.dumps({"ok": True, "output": args.output, "sample_rate": sr, "channels": int(out.shape[1])}), flush=True)


def detect(args) -> None:
    device = _device(args.device)
    detector = _load(args.detector, "detector", device)
    audio, sr = sf.read(args.input, always_2d=True, dtype="float32")
    per_channel = []
    best = None
    threshold = float(args.threshold)
    for channel in range(audio.shape[1]):
        mono = audio[:, channel]
        tensor = _resample(mono, sr, device).unsqueeze(0).unsqueeze(0)
        if tensor.shape[-1] < int(RATE * 0.3):
            per_channel.append({"channel": channel, "error": "audio troppo corto"})
            continue
        with torch.no_grad():
            score, bits = detector.detect_watermark(tensor)
        confidence = float(torch.as_tensor(score).float().mean().item())
        values = torch.as_tensor(bits).float().reshape(-1)
        payload = None
        if values.numel() >= 16:
            payload = sum((int(float(values[i]) > 0.5) << i) for i in range(16))
        entry = {
            "channel": channel,
            "score": round(confidence, 6),
            "payload_16bit": payload,
            "payload_hex": f"{payload:04x}" if payload is not None else None,
        }
        per_channel.append(entry)
        if best is None or confidence > best["score"]:
            best = entry
    if best is None:
        print(json.dumps({"ok": True, "detected": False, "confidence": 0.0, "per_channel": per_channel, "available": True}), flush=True)
        return
    print(json.dumps({
        "ok": True,
        "detected": bool(best["score"] >= threshold),
        "confidence": best["score"],
        "payload_16bit": best["payload_16bit"],
        "payload_hex": best["payload_hex"],
        "threshold": threshold,
        "per_channel": per_channel,
        "available": True,
    }), flush=True)


def selftest(args) -> None:
    device = _device(args.device)
    generator = _load(args.generator, "generator", device)
    detector = _load(args.detector, "detector", device)
    t = torch.arange(int(RATE * 1.0), dtype=torch.float32) / RATE
    tone = (0.12 * torch.sin(2 * torch.pi * 440 * t)).unsqueeze(0).unsqueeze(0).to(device)
    payload = 0x5A3C
    with torch.no_grad():
        watermark = generator.get_watermark(tone, message=_bits(payload).to(device))
        watermarked = tone + watermark
        score, bits = detector.detect_watermark(watermarked)
    confidence = float(torch.as_tensor(score).float().mean().item())
    values = torch.as_tensor(bits).float().reshape(-1)
    decoded = sum((int(float(values[i]) > 0.5) << i) for i in range(16)) if values.numel() >= 16 else None
    print(json.dumps({
        "ok": confidence >= 0.5 and decoded == payload,
        "confidence": round(confidence, 6),
        "payload_16bit": decoded,
        "expected": payload,
        "device": str(device),
        "torch": torch.__version__,
        "audioseal": getattr(__import__("audioseal"), "__version__", "unknown"),
    }), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["embed", "detect", "selftest"])
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--generator", required=True)
    parser.add_argument("--detector", required=True)
    parser.add_argument("--payload", type=lambda value: int(value, 16), default=0)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--subtype", default=None)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if args.command == "embed":
        embed(args)
    elif args.command == "detect":
        detect(args)
    else:
        selftest(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''



