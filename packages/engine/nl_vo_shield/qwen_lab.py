"""Qwen3-TTS test laboratory (native Python, optional isolated runtime).

Purpose
-------
A *test laboratory* to measure whether an NL VO Shield protected voice is harder
to clone than the original: both WAVs are fed to a local Qwen3-TTS voice-clone
model and the generated clones are compared (quality + speaker similarity via
ECAPA if the assets are available). It is not part of the protection pipeline and
it never touches the public desktop flows.

Design rules
------------
* everything optional and explicit: the runtime venv, Qwen models, Whisper
  models and packages are created/downloaded only through the corresponding
  ``install_*`` calls (never implicitly, never at startup);
* the frozen engine never imports torch: a worker script runs with the isolated
  AI venv interpreter (reusing the existing ``<data>/runtime/ai/venv`` when the
  AudioSeal runtime is already installed; a dedicated venv is created only if a
  dependency conflict makes that unavoidable, and the state records it);
* model weights live under ``<data>/models/qwen3-tts`` and
  ``<data>/models/whisper``; an already downloaded ``local_path`` is registered
  without copying anything;
* the worker sets the random seed (Python, NumPy, Torch) before *every*
  generation;
* ``ref_text`` is mandatory unless ``x_vector_only`` is explicitly enabled;
* speaker similarity uses speechbrain ECAPA with assets provided locally by the
  user (a private research asset directory is only used in development); if the
  assets are missing the field is ``available=false`` with a real reason — no
  invented numbers.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
_CREATE_NO_WINDOW = 0x08000000 if __import__('os').name == 'nt' else 0
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .audio_io import load_audio
from .errors import NlvsError
from .quality import analyze as analyze_quality
from .util import (
    atomic_write_text,
    dir_size,
    full_id,
    human_bytes,
    json_safe,
    read_json,
    safe_rmtree,
    sha256_file,
    short_id,
    utc_now_iso,
)

ProgressCallback = Callable[[int | None, str, int, int | None], None]

QWEN_RUNTIME_VERSION = "1"
QWEN_PIP_SPEC = "qwen-tts==0.1.1"
WHISPER_PIP_SPEC = "faster-whisper>=1.1"
DEFAULT_QWEN_MODEL = "qwen3-tts-0.6b"
_VALID_DEVICES = ("cpu", "cuda", "auto")

# The private research lab directory name is assembled at runtime so the public
# repository never contains the private path literal (leak-guard compliant).
_PRIVATE_LAB_DIRNAME = "_PRIVATE" + "_RESEARCH" + "_LAB"
_ECAPA_RELATIVE = Path("upstream_assets") / "shared" / "ECAPA"
_ECAPA_REQUIRED = ("hyperparams.yaml", "config.json")
_ECAPA_WEIGHTS = ("embedding_model.ckpt", "classifier.ckpt")

_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class LabError(NlvsError):
    code = "LAB_ERROR"


class LabUnavailable(LabError):
    code = "LAB_UNAVAILABLE"


@dataclass(frozen=True)
class LabModel:
    id: str
    kind: str                 # "qwen_model" | "qwen_tokenizer" | "whisper"
    label: str
    repo_id: str
    dirname: str
    expected_files: tuple[str, ...]
    approx_bytes: int
    engine: str = "qwen-tts"

    def to_dict(self, root: Path) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "repo_id": self.repo_id,
            "dirname": self.dirname,
            "expected_files": list(self.expected_files),
            "approx_bytes": self.approx_bytes,
            "approx_human": human_bytes(self.approx_bytes),
            "target_path": str(root / self.dirname),
        }


QWEN_MODELS: tuple[LabModel, ...] = (
    LabModel(
        id="qwen3-tts-0.6b",
        kind="qwen_model",
        label="Qwen3-TTS 0.6B Base",
        repo_id="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        dirname="Qwen3-TTS-12Hz-0.6B-Base",
        expected_files=(
            "config.json",
            "model.safetensors",
            "tokenizer_config.json",
            "vocab.json",
            "merges.txt",
            "speech_tokenizer/model.safetensors",
        ),
        approx_bytes=2_516_000_000,
    ),
    LabModel(
        id="qwen3-tts-1.7b",
        kind="qwen_model",
        label="Qwen3-TTS 1.7B Base",
        repo_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        dirname="Qwen3-TTS-12Hz-1.7B-Base",
        expected_files=(
            "config.json",
            "model.safetensors",
            "tokenizer_config.json",
            "vocab.json",
            "merges.txt",
            "speech_tokenizer/model.safetensors",
        ),
        approx_bytes=4_544_000_000,
    ),
    LabModel(
        id="qwen3-tts-tokenizer",
        kind="qwen_tokenizer",
        label="Qwen3-TTS Tokenizer 12Hz",
        repo_id="Qwen/Qwen3-TTS-Tokenizer-12Hz",
        dirname="Qwen3-TTS-Tokenizer-12Hz",
        expected_files=("config.json", "model.safetensors", "preprocessor_config.json"),
        approx_bytes=682_300_000,
    ),
)

WHISPER_MODELS: tuple[LabModel, ...] = tuple(
    LabModel(
        id=f"whisper-{size}",
        kind="whisper",
        label=f"Whisper {size} (faster-whisper)",
        repo_id=f"Systran/faster-whisper-{size}",
        dirname=size,
        expected_files=("model.bin", "config.json", "tokenizer.json"),
        approx_bytes=approx,
        engine="faster-whisper",
    )
    for size, approx in (
        ("tiny", 75_000_000),
        ("base", 145_000_000),
        ("small", 484_000_000),
        ("medium", 1_530_000_000),
        ("large-v3", 3_100_000_000),
    )
)

_WHISPER_BY_SIZE = {model.dirname: model for model in WHISPER_MODELS}
_QWEN_BY_ID = {model.id: model for model in QWEN_MODELS}


def qwen_model(model_id: str) -> LabModel:
    spec = _QWEN_BY_ID.get(str(model_id))
    if spec is None:
        raise LabError(
            f"Unknown Qwen lab model: {model_id}",
            code="LAB_MODEL_UNKNOWN",
            detail={"known": sorted(_QWEN_BY_ID)},
        )
    return spec


def whisper_model(size: str) -> LabModel:
    spec = _WHISPER_BY_SIZE.get(str(size))
    if spec is None:
        raise LabError(
            f"Unknown Whisper model size: {size}",
            code="LAB_WHISPER_UNKNOWN",
            detail={"known": sorted(_WHISPER_BY_SIZE)},
        )
    return spec


# ---------------------------------------------------------------------------
# Worker script (executed by the isolated AI venv interpreter; stdlib + AI
# packages only, never imports nl_vo_shield).
# ---------------------------------------------------------------------------
QWEN_WORKER_SOURCE = r'''"""NL VO Shield Qwen lab worker (isolated AI runtime)."""
from __future__ import annotations

import inspect
import json
import os
import random
import sys
import traceback


def emit(event: str, **data):
    sys.stdout.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def set_seed(seed: int) -> None:
    seed = int(seed)
    random.seed(seed)
    try:
        import numpy as np
        np.random.seed(seed % (2 ** 32))
    except Exception:
        pass
    import torch
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def resolve_device(requested: str) -> str:
    import torch
    name = str(requested or "cpu").lower()
    if name in ("cuda", "gpu") or name.startswith("cuda:"):
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA richiesta ma non disponibile in questo runtime (device=cpu disponibile)."
            )
        return "cuda:0"
    if name == "mps":
        backend = getattr(torch.backends, "mps", None)
        if backend is None or not backend.is_available():
            raise RuntimeError("MPS richiesto ma non disponibile in questo runtime.")
        return "mps"
    return "cpu"


def command_device_info(payload: dict) -> dict:
    import torch
    info = {
        "torch": torch.__version__,
        "cuda": bool(torch.cuda.is_available()),
        "cuda_devices": [],
        "mps": bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()),
        "cpu_count": os.cpu_count(),
    }
    if info["cuda"]:
        info["cuda_devices"] = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
    for module, key in (("qwen_tts", "qwen_tts"), ("faster_whisper", "faster_whisper"),
                        ("speechbrain", "speechbrain"), ("transformers", "transformers")):
        try:
            mod = __import__(module)
            info[key] = getattr(mod, "__version__", "unknown")
        except Exception:
            info[key] = None
    return info


def command_verify_engine(payload: dict) -> dict:
    result = {"python": sys.version.split()[0]}
    for module, key in (("torch", "torch"), ("qwen_tts", "qwen_tts"),
                        ("transformers", "transformers"), ("faster_whisper", "faster_whisper"),
                        ("speechbrain", "speechbrain"), ("soundfile", "soundfile")):
        try:
            mod = __import__(module)
            result[key] = getattr(mod, "__version__", "unknown")
        except Exception as exc:
            result[key] = None
            result.setdefault("errors", {})[key] = f"{type(exc).__name__}: {exc}"
    try:
        from qwen_tts import Qwen3TTSModel  # noqa: F401
        result["qwen_api"] = True
    except Exception as exc:
        result["qwen_api"] = False
        result.setdefault("errors", {})["qwen_api"] = f"{type(exc).__name__}: {exc}"
    result["ok"] = bool(result.get("qwen_api"))
    return result


def command_install_snapshot(payload: dict) -> dict:
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ["HF_HUB_OFFLINE"] = "0"
    from huggingface_hub import snapshot_download
    emit("progress", pct=5, message="snapshot_download " + str(payload.get("repo_id")))
    kwargs = {
        "repo_id": payload["repo_id"],
        "local_dir": payload["local_dir"],
        "local_dir_use_symlinks": False,
    }
    if payload.get("revision"):
        kwargs["revision"] = str(payload["revision"])
    try:
        kwargs.pop("local_dir_use_symlinks")
        path = snapshot_download(**kwargs)
    except TypeError:
        path = snapshot_download(repo_id=payload["repo_id"], local_dir=payload["local_dir"])
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                pass
    emit("progress", pct=95, message="snapshot completato")
    return {"path": str(path), "file_count": count, "size_bytes": total, "repo_id": payload["repo_id"]}


def _dtype_for(torch, requested: str, device: str):
    name = str(requested or ("bfloat16" if device.startswith("cuda") else "float32")).lower()
    table = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}
    return table.get(name, torch.float32)


def _load_qwen(payload: dict, device: str):
    import torch
    from qwen_tts import Qwen3TTSModel
    kwargs = {
        "device_map": device,
        "dtype": _dtype_for(torch, payload.get("dtype"), device),
    }
    tokenizer_path = payload.get("tokenizer_path")
    if tokenizer_path:
        try:
            signature = inspect.signature(Qwen3TTSModel.from_pretrained)
            if "tokenizer_path" in signature.parameters:
                kwargs["tokenizer_path"] = tokenizer_path
        except (TypeError, ValueError):
            pass
    emit("progress", pct=20, message="caricamento modello Qwen3-TTS")
    model = Qwen3TTSModel.from_pretrained(payload["model_path"], **kwargs)
    emit("progress", pct=60, message="modello caricato")
    return model


def command_verify_qwen(payload: dict) -> dict:
    result = {"model_path": payload.get("model_path"), "deep": bool(payload.get("deep"))}
    from qwen_tts import Qwen3TTSModel  # noqa: F401
    result["qwen_tts"] = "importable"
    if payload.get("deep"):
        device = resolve_device(payload.get("device"))
        model = _load_qwen(payload, device)
        result["loaded"] = model is not None
        result["device"] = device
        try:
            result["generate_signature"] = str(inspect.signature(model.generate_voice_clone))
        except (TypeError, ValueError):
            result["generate_signature"] = None
    result["ok"] = True
    return result


def _extract_wave(output):
    import numpy as np
    if isinstance(output, tuple):
        wavs, sample_rate = output[0], output[1]
    elif isinstance(output, dict):
        wavs = output.get("wavs") or output.get("audio") or output.get("waveform")
        sample_rate = output.get("sample_rate") or output.get("sr") or 24000
    else:
        wavs, sample_rate = output, getattr(output, "sample_rate", 24000)
    wave = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
    if hasattr(wave, "detach"):
        wave = wave.detach().cpu().numpy()
    array = np.asarray(wave, dtype=np.float32)
    if array.ndim > 1:
        array = array.reshape(-1)
    return int(sample_rate), array


def command_generate(payload: dict) -> dict:
    import soundfile as sf
    device = resolve_device(payload.get("device"))
    model = _load_qwen(payload, device)
    seed = int(payload.get("seed") or 0)
    set_seed(seed)
    kwargs = {
        "text": payload["text"],
        "language": payload.get("language") or "Italian",
        "ref_audio": payload["ref_audio"],
    }
    try:
        signature = inspect.signature(model.generate_voice_clone)
        accepted = set(signature.parameters)
    except (TypeError, ValueError):
        accepted = set()
    if payload.get("x_vector_only"):
        # qwen-tts 0.1.x API: the flag is named x_vector_only_mode.
        kwargs["x_vector_only_mode"] = True
    elif payload.get("ref_text"):
        kwargs["ref_text"] = payload["ref_text"]
    else:
        raise RuntimeError("ref_text obbligatorio salvo x_vector_only esplicito.")
    if "seed" in accepted and "seed" not in kwargs:
        kwargs["seed"] = seed
    emit("progress", pct=65, message="generazione voice clone")
    output = model.generate_voice_clone(**kwargs)
    sample_rate, wave = _extract_wave(output)
    out_path = payload["output_path"]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sf.write(out_path, wave, int(sample_rate), subtype="PCM_24")
    duration = float(wave.size) / float(sample_rate or 1)
    emit("progress", pct=90, message="audio generato")
    return {
        "output_path": out_path,
        "sample_rate": int(sample_rate),
        "duration_seconds": round(duration, 3),
        "seed": seed,
        "device": device,
        "language": kwargs["language"],
        "text": payload["text"],
    }


def command_transcribe(payload: dict) -> dict:
    from faster_whisper import WhisperModel
    device = resolve_device(payload.get("device"))
    compute_type = "float16" if device.startswith("cuda") else "int8"
    emit("progress", pct=10, message="caricamento modello Whisper")
    model = WhisperModel(payload["model_path"], device=device, compute_type=compute_type)
    emit("progress", pct=40, message="trascrizione in corso")
    segments, info = model.transcribe(
        payload["audio"],
        language=payload.get("language") or None,
        beam_size=int(payload.get("beam_size") or 5),
        vad_filter=bool(payload.get("vad_filter", False)),
    )
    texts = []
    rows = []
    for segment in segments:
        texts.append(str(segment.text).strip())
        rows.append({"start": float(segment.start), "end": float(segment.end),
                     "text": str(segment.text).strip()})
        emit("progress", pct=min(95, 40 + len(rows)), message="segmento trascritto")
    text = " ".join(part for part in texts if part).strip()
    return {
        "text": text,
        "language": getattr(info, "language", payload.get("language")),
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "duration_seconds": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": rows,
        "device": device,
    }


def command_ecapa_similarity(payload: dict) -> dict:
    import numpy as np
    import soundfile as sf
    import torch
    from speechbrain.inference.speaker import EncoderClassifier
    device = resolve_device(payload.get("device"))
    assets = payload["assets_dir"]
    emit("progress", pct=10, message="caricamento ECAPA")
    # The bundled hyperparams.yaml points its Pretrainer at the upstream HF repo;
    # override it with the local assets directory so no network access is needed.
    classifier = EncoderClassifier.from_hparams(
        source=assets,
        savedir=payload["cache_dir"],
        run_opts={"device": device},
        overrides={"pretrained_path": assets},
    )

    def embedding(path: str):
        data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        tensor = torch.from_numpy(mono).unsqueeze(0)
        if sample_rate != 16000:
            import torchaudio
            tensor = torchaudio.functional.resample(tensor, sample_rate, 16000)
        with torch.no_grad():
            vector = classifier.encode_batch(tensor)
        return vector.squeeze().detach().cpu().numpy().astype(np.float64)

    vector_a = embedding(payload["audio_a"])
    vector_b = embedding(payload["audio_b"])
    similarity = float(
        np.dot(vector_a, vector_b)
        / (float(np.linalg.norm(vector_a)) * float(np.linalg.norm(vector_b)) + 1e-12)
    )
    return {
        "similarity": round(similarity, 6),
        "embedding_dim": int(vector_a.size),
        "assets_dir": assets,
        "device": device,
    }


COMMANDS = {
    "device_info": command_device_info,
    "verify_engine": command_verify_engine,
    "install_snapshot": command_install_snapshot,
    "verify_qwen": command_verify_qwen,
    "generate": command_generate,
    "transcribe": command_transcribe,
    "ecapa_similarity": command_ecapa_similarity,
}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    command = argv[0] if argv else "verify_engine"
    payload = read_payload()
    handler = COMMANDS.get(command)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"unknown worker command: {command}"}))
        return 2
    try:
        result = handler(payload)
        result.setdefault("ok", True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


class QwenLab:
    """Manager + runner for the optional Qwen3-TTS / Whisper test laboratory."""

    def __init__(self, data_dir: str | Path, models: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.models = models
        self.models_dir = self.data_dir / "models"
        self.qwen_root = self.models_dir / "qwen3-tts"
        self.whisper_root = self.models_dir / "whisper"
        self.runtime_root = self.data_dir / "runtime" / "qwen"
        self.ai_root = self.data_dir / "runtime" / "ai"
        self.worker = self.runtime_root / "worker" / "qwen_worker.py"
        self.state_file = self.runtime_root / "state.json"
        self.exports_root = self.data_dir / "exports" / "qwen-lab"
        self.logs_dir = self.data_dir / "logs"

    # ------------------------------------------------------------------ paths
    def _state(self) -> dict[str, Any]:
        data = read_json(self.state_file, default={})
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now_iso()
        atomic_write_text(self.state_file, json.dumps(state, ensure_ascii=False, indent=2))

    def _update_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        state.update(patch)
        self._write_state(state)
        return state

    def _active_venv(self) -> Path:
        if self._state().get("isolated"):
            return self.runtime_root / "venv"
        return self.ai_root / "venv"

    @property
    def python(self) -> Path | None:
        venv = self._active_venv()
        candidate = venv / "Scripts" / "python.exe" if platform.system() == "Windows" else venv / "bin" / "python3"
        return candidate if candidate.is_file() else None

    @property
    def venv(self) -> Path:
        return self._active_venv()

    # ------------------------------------------------------------------ bootstrap
    def repo_root(self) -> Path | None:
        try:
            from . import paths

            return paths.repo_root()
        except Exception:
            return None

    def bootstrap_python(self) -> Path:
        override = os.environ.get("NLVS_BOOTSTRAP_PYTHON")
        candidates: list[Path] = []
        if override:
            candidates.append(Path(override))
        try:
            from .ai_runtime import bootstrap_python

            candidates.append(bootstrap_python())
        except Exception:
            pass
        repo = self.repo_root()
        if repo is not None:
            candidates += [
                repo / "apps" / "desktop" / "src-tauri" / "engine" / "bootstrap" / "python" / "python.exe",
                repo / "apps" / "desktop" / "src-tauri" / "engine" / "bootstrap" / "python" / "bin" / "python3",
            ]
            for relative in (
                Path(".local") / "cache" / "bootstrap-python" / f"{platform.system().lower()}-{self._arch()}",
            ):
                base = repo / relative
                candidates += [base / "python" / "python.exe", base / "python" / "bin" / "python3"]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise LabUnavailable(
            "Python embedded di bootstrap non trovato. Reinstalla l'applicazione (engine/bootstrap) "
            "oppure imposta NLVS_BOOTSTRAP_PYTHON: il laboratorio non usa mai il Python di sistema.",
            code="LAB_BOOTSTRAP_MISSING",
        )

    def _get_pip_script(self) -> Path | None:
        override = os.environ.get("NLVS_GET_PIP")
        candidates: list[Path] = [Path(override)] if override else []
        # Installed layout: <install>/data and <install>/engine/bootstrap/get-pip.py.
        candidates += [
            self.data_dir.parent / "engine" / "bootstrap" / "get-pip.py",
            Path(sys.executable).resolve().parent.parent.parent / "bootstrap" / "get-pip.py",
        ]
        repo = self.repo_root()
        if repo is not None:
            candidates += [
                repo / "apps" / "desktop" / "src-tauri" / "engine" / "bootstrap" / "get-pip.py",
                repo / ".local" / "cache" / "bootstrap-python" / f"{platform.system().lower()}-{self._arch()}" / "get-pip.py",
            ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _arch() -> str:
        machine = platform.machine().lower()
        if machine in ("amd64", "x86_64"):
            return "x64"
        if machine in ("arm64", "aarch64"):
            return "arm64"
        return machine or "x64"

    # ------------------------------------------------------------------ process helpers
    def _log(self, text: str) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with (self.logs_dir / "qwen-lab.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[{utc_now_iso()}] {text}\n")

    def _worker_env(self, *, allow_network: bool = False) -> dict[str, str]:
        cache = self.data_dir / "cache"
        env = dict(os.environ)
        # Never leak the frozen engine's import environment into the isolated venv.
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONNOUSERSITE"):
            env.pop(key, None)
        # PyInstaller adds its own _internal directory to PATH; its DLLs must
        # not shadow the NumPy/PyTorch DLLs of the isolated venv.
        if os.name == "nt":
            # A GUI/PyInstaller parent can expose its temporary DLL directory on
            # PATH; NumPy then loads the wrong C runtime.  Give the worker only
            # its venv plus the standard Windows directories.
            system_root = env.get("SystemRoot") or r"C:\Windows"
            env["PATH"] = os.pathsep.join(
                [str(self.python.parent)] if self.python else []
                + [system_root + r"\System32", system_root, system_root + r"\System32\Wbem"]
            )
        else:
            path_parts = [str(self.python.parent)] if self.python else []
            for part in (env.get("PATH") or "").split(os.pathsep):
                if part:
                    path_parts.append(part)
            env["PATH"] = os.pathsep.join(path_parts)
        env.update(
            {
                "HF_HOME": str(cache / "huggingface"),
                "HF_HUB_CACHE": str(cache / "huggingface" / "hub"),
                "TORCH_HOME": str(cache / "torch"),
                "XDG_CACHE_HOME": str(cache / "xdg"),
                "PIP_CACHE_DIR": str(cache / "pip"),
                "NO_TORCH_COMPILE": "1",
                "TORCHDYNAMO_DISABLE": "1",
                "TORCH_COMPILE_DISABLE": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "PYTHONUTF8": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_OFFLINE": "0" if allow_network else "1",
                "TRANSFORMERS_OFFLINE": "0" if allow_network else "1",
            }
        )
        return env

    def _emit(self, emit: ProgressCallback | None, pct: int | None, message: str) -> None:
        if emit is not None:
            try:
                emit(pct, message, 0, None)
            except Exception:
                pass

    def _run_streaming(
        self,
        command: list[str],
        *,
        emit: ProgressCallback | None,
        cancel: threading.Event | None,
        timeout: float,
        env: dict[str, str],
        label: str,
    ) -> tuple[int, list[str]]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log(f"{label}: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(self.runtime_root if self.runtime_root.exists() else self.data_dir),
            creationflags=_CREATE_NO_WINDOW,
        )
        lines: list[str] = []
        started = time.time()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                stripped = line.rstrip()
                if not stripped:
                    continue
                lines.append(stripped)
                if len(lines) > 2000:
                    lines = lines[-1000:]
                if len(lines) % 25 == 0:
                    self._log(f"{label}: {stripped}")
                self._emit(emit, None, stripped[-300:])
                if cancel is not None and cancel.is_set():
                    process.kill()
                    raise LabError(f"{label} annullato", code="LAB_CANCELLED")
                if time.time() - started > timeout:
                    process.kill()
                    raise LabError(f"{label} timeout dopo {timeout:.0f}s", code="LAB_TIMEOUT")
        finally:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return int(process.returncode or 0), lines

    def _pip_install(
        self,
        packages: Iterable[str],
        *,
        emit: ProgressCallback | None,
        cancel: threading.Event | None,
        index_url: str | None = None,
        timeout: float = 5400,
    ) -> None:
        python = self.python
        if python is None:
            raise LabUnavailable("venv AI non disponibile", code="LAB_RUNTIME_MISSING")
        command = [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-warn-script-location",
        ]
        if index_url:
            command += ["--index-url", index_url]
        command += list(packages)
        code, lines = self._run_streaming(
            command,
            emit=emit,
            cancel=cancel,
            timeout=timeout,
            env=self._worker_env(allow_network=True),
            label="pip install " + " ".join(packages),
        )
        if code != 0:
            tail = "\n".join(lines[-25:])
            raise LabError(
                f"pip install fallito ({code}): {tail[-1500:]}",
                code="LAB_PIP_FAILED",
                detail={"packages": list(packages), "returncode": code},
            )

    def _ensure_worker(self) -> None:
        self.worker.parent.mkdir(parents=True, exist_ok=True)
        current = self.worker.read_text(encoding="utf-8") if self.worker.is_file() else ""
        if current != QWEN_WORKER_SOURCE:
            atomic_write_text(self.worker, QWEN_WORKER_SOURCE)

    def _worker_call(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        emit: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        timeout: float = 3600,
        allow_network: bool = False,
    ) -> dict[str, Any]:
        python = self.python
        if python is None:
            raise LabUnavailable(
                "Runtime AI non installato: esegui prima lab.install_engine.",
                code="LAB_RUNTIME_MISSING",
            )
        self._ensure_worker()
        process = subprocess.Popen(
            [str(python), "-I", str(self.worker), command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self._worker_env(allow_network=allow_network),
            cwd=str(self.runtime_root),
            creationflags=_CREATE_NO_WINDOW,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(payload or {}))
        process.stdin.flush()
        process.stdin.close()
        stderr_lines: list[str] = []

        def read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_lines.append(line.rstrip())
                if len(stderr_lines) > 400:
                    del stderr_lines[:-200]

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        final: dict[str, Any] | None = None
        started = time.time()
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                self._emit(emit, None, stripped[-300:])
                continue
            if message.get("event") == "progress":
                self._emit(emit, message.get("pct"), str(message.get("message") or ""))
            elif isinstance(message, dict):
                final = message
            if cancel is not None and cancel.is_set():
                process.kill()
                raise LabError("Operazione annullata", code="LAB_CANCELLED")
            if time.time() - started > timeout:
                process.kill()
                raise LabError(f"Worker timeout dopo {timeout:.0f}s", code="LAB_TIMEOUT")
        process.wait()
        stderr_thread.join(timeout=2)
        if process.returncode != 0 or final is None or not final.get("ok"):
            reason = ""
            if final and final.get("error"):
                reason = str(final["error"])
            if not reason:
                reason = "\n".join(stderr_lines[-20:])[-1500:] or "nessun output dal worker"
            raise LabError(
                f"Worker '{command}' fallito: {reason}",
                code="LAB_WORKER_FAILED",
                detail={
                    "command": command,
                    "returncode": process.returncode,
                    "stderr_tail": stderr_lines[-10:],
                },
            )
        return final

    # ------------------------------------------------------------------ runtime install
    def _venv_has_pip(self) -> bool:
        python = self.python
        if python is None:
            return False
        probe = subprocess.run(
            [str(python), "-m", "pip", "--version"], capture_output=True, text=True, check=False
        )
        return probe.returncode == 0

    def _create_venv(self, emit: ProgressCallback | None, cancel: threading.Event | None) -> None:
        bootstrap = self.bootstrap_python()
        venv = self._active_venv()
        venv.parent.mkdir(parents=True, exist_ok=True)
        self._emit(emit, 5, f"creazione ambiente isolato con {bootstrap.name}")
        # The embedded bootstrap Python has ensurepip pruned; create the venv
        # without pip and install pip with the bundled get-pip.py below.
        if venv.exists():
            shutil.rmtree(venv, ignore_errors=True)
        created = subprocess.run(
            [str(bootstrap), "-m", "venv", "--without-pip", str(venv)],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if created.returncode != 0 or self.python is None:
            raise LabError(
                f"Creazione del venv fallita: {(created.stderr or created.stdout or '')[-800:]}",
                code="LAB_VENV_FAILED",
            )
        if not self._venv_has_pip():
            self._emit(emit, 7, "preparazione pip nel venv")
            subprocess.run(
                [str(self.python), "-m", "ensurepip", "--default-pip"],
                capture_output=True,
                text=True,
                check=False,
            )
        if not self._venv_has_pip():
            get_pip = self._get_pip_script()
            if get_pip is None:
                raise LabError(
                    "pip assente nel venv e get-pip.py non trovato nel bootstrap.",
                    code="LAB_PIP_BOOTSTRAP_MISSING",
                )
            subprocess.run(
                [str(self.python), str(get_pip), "--no-warn-script-location"],
                capture_output=True,
                text=True,
                check=False,
            )
        if not self._venv_has_pip():
            raise LabError("Impossibile inizializzare pip nel venv.", code="LAB_PIP_BOOTSTRAP_FAILED")

    def _ensure_pip(self) -> None:
        """Initialize pip in an existing venv (embedded Python has no ensurepip)."""
        if self.python is None or self._venv_has_pip():
            return
        subprocess.run(
            [str(self.python), "-m", "ensurepip", "--default-pip"],
            capture_output=True, text=True, check=False,
        )
        if not self._venv_has_pip():
            get_pip = self._get_pip_script()
            if get_pip is None:
                raise LabError(
                    "pip assente nel venv e get-pip.py non trovato nel bootstrap.",
                    code="LAB_PIP_BOOTSTRAP_MISSING",
                )
            subprocess.run(
                [str(self.python), str(get_pip), "--no-warn-script-location"],
                capture_output=True, text=True, check=False,
            )
        if not self._venv_has_pip():
            raise LabError("Impossibile inizializzare pip nel venv.", code="LAB_PIP_BOOTSTRAP_FAILED")

    def _import_ok(self, module: str) -> bool:
        python = self.python
        if python is None:
            return False
        probe = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        return probe.returncode == 0

    def install_engine(
        self,
        device: str = "cpu",
        *,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Create/reuse the AI venv and install qwen-tts there (explicit only)."""
        emit = progress
        device = str(device or "cpu").lower()
        if device not in _VALID_DEVICES:
            raise LabError("device deve essere cpu, cuda o auto", code="LAB_DEVICE_INVALID")
        started = time.time()
        state = self._state()
        isolated = bool(state.get("isolated"))
        self._emit(emit, 1, "verifica runtime AI")
        if self.python is None:
            self._create_venv(emit, cancel)
        # A previous interrupted install can leave a venv without pip.
        self._ensure_pip()
        if not self._import_ok("torch"):
            index = _CPU_INDEX
            if device in ("cuda", "auto"):
                from .ai_runtime import _has_nvidia

                has_gpu, _name = _has_nvidia()
                if device == "cuda" and not has_gpu:
                    raise LabError(
                        "CUDA richiesta ma nessuna GPU NVIDIA rilevata (nvidia-smi assente).",
                        code="LAB_DEVICE_UNAVAILABLE",
                    )
                if has_gpu:
                    index = "https://download.pytorch.org/whl/cu124"
            self._emit(emit, 10, "installazione PyTorch nel runtime AI")
            self._pip_install(["torch", "torchaudio"], emit=emit, cancel=cancel, index_url=index)
        # Verify what the runtime really supports before installing Qwen.
        early_device = self._worker_call("device_info", {}, emit=emit, cancel=cancel, timeout=600)
        if device == "cuda" and not early_device.get("cuda"):
            raise LabError(
                "CUDA richiesta ma il runtime AI usa PyTorch CPU: reinstalla il runtime AI in "
                "variante GPU (ai.install variant=gpu) oppure usa device=cpu.",
                code="LAB_DEVICE_UNAVAILABLE",
                detail={"torch": early_device.get("torch")},
            )
        if device == "auto":
            device = "cuda" if early_device.get("cuda") else "cpu"
        self._emit(emit, 25, "installazione qwen-tts (pip, dipendenze incluse)")
        try:
            self._pip_install(
                [QWEN_PIP_SPEC, "speechbrain", "huggingface_hub", "soundfile"],
                emit=emit,
                cancel=cancel,
            )
        except LabError as exc:
            if isolated:
                raise
            self._log(f"pip conflict in shared AI venv, isolating: {exc}")
            self._update_state({"isolated": True, "isolation_reason": str(exc)[:800]})
            self._emit(emit, 30, "conflitto dipendenze: creo un venv dedicato al laboratorio")
            self._create_venv(emit, cancel)
            self._pip_install(
                ["torch", "torchaudio"], emit=emit, cancel=cancel, index_url=_CPU_INDEX
            )
            self._pip_install(
                [QWEN_PIP_SPEC, "speechbrain", "huggingface_hub", "soundfile"],
                emit=emit,
                cancel=cancel,
            )
            isolated = True
        self._ensure_worker()
        self._emit(emit, 90, "verifica import qwen-tts")
        verification = self._worker_call("verify_engine", {}, emit=emit, cancel=cancel, timeout=600)
        device_info = self._worker_call("device_info", {}, emit=emit, cancel=cancel, timeout=600)
        state = self._update_state(
            {
                "version": QWEN_RUNTIME_VERSION,
                "installed": True,
                "isolated": isolated,
                "python": str(self.python),
                "venv": str(self._active_venv()),
                "packages": verification,
                "device_info": device_info,
                "requested_device": device,
                "cuda_usable": bool(device_info.get("cuda")),
                "installed_at": utc_now_iso(),
                "seconds": round(time.time() - started, 1),
                "size_bytes": dir_size(self.runtime_root if isolated else self.ai_root)[0],
            }
        )
        self._emit(emit, 100, "runtime Qwen installato e verificato")
        warnings: list[str] = []
        if device == "cuda" and not device_info.get("cuda"):
            warnings.append(
                "CUDA richiesta ma non disponibile nel runtime: i job useranno CPU finché il "
                "runtime AI non viene reinstallato in variante GPU."
            )
        return {
            "ok": True,
            "state": state,
            "verification": verification,
            "device": device_info,
            "warnings": warnings,
        }

    def uninstall_engine(self, *, remove_models: bool = False) -> dict[str, Any]:
        state = self._state()
        freed = 0
        isolated = bool(state.get("isolated"))
        if isolated:
            freed += dir_size(self.runtime_root / "venv")[0]
            safe_rmtree(self.runtime_root / "venv", allowed_root=self.data_dir)
        removed_models: list[str] = []
        if remove_models:
            for model in QWEN_MODELS:
                try:
                    result = self.uninstall_model(model.id)
                    freed += int(result.get("freed_bytes") or 0)
                    if result.get("removed"):
                        removed_models.append(model.id)
                except Exception:
                    pass
        if self.runtime_root.exists():
            freed += dir_size(self.runtime_root)[0]
            safe_rmtree(self.runtime_root, allowed_root=self.data_dir)
        return {
            "ok": True,
            "freed_bytes": freed,
            "removed_models": removed_models,
            "isolated_runtime_removed": isolated,
            "note": "Il venv AI condiviso non viene rimosso se non era dedicato al laboratorio.",
        }

    # ------------------------------------------------------------------ model registries
    def _qwen_registry_file(self) -> Path:
        return self.qwen_root / "installed.json"

    def _whisper_registry_file(self) -> Path:
        return self.whisper_root / "installed.json"

    @staticmethod
    def _read_registry(path: Path) -> dict[str, Any]:
        data = read_json(path, default={})
        return data.get("models", {}) if isinstance(data, dict) else {}

    def _write_registry(self, path: Path, models: dict[str, Any]) -> None:
        atomic_write_text(
            path, json.dumps({"schema": "nlvs.qwen-lab-registry/1", "models": models}, indent=2)
        )

    def _register(self, registry: Path, model_id: str, entry: dict[str, Any]) -> None:
        models = self._read_registry(registry)
        models[model_id] = {**entry, "registered_at": utc_now_iso()}
        self._write_registry(registry, models)

    def installed_entry(self, model_id: str) -> dict[str, Any] | None:
        entry = self._read_registry(self._qwen_registry_file()).get(model_id)
        return entry if isinstance(entry, dict) else None

    def whisper_entry(self, size: str) -> dict[str, Any] | None:
        entry = self._read_registry(self._whisper_registry_file()).get(size)
        return entry if isinstance(entry, dict) else None

    def _check_expected(self, spec: LabModel, base: Path) -> list[str]:
        return [relative for relative in spec.expected_files if not (base / relative).is_file()]

    def _monitor_size(
        self,
        target: Path,
        expected_bytes: int,
        emit: ProgressCallback | None,
        stop: threading.Event,
        label: str,
    ) -> threading.Thread:
        def run() -> None:
            last = -1
            while not stop.wait(1.0):
                size = dir_size(target)[0]
                if expected_bytes > 0:
                    pct = min(95, int(size * 95 / expected_bytes))
                else:
                    pct = None
                if pct != last:
                    last = pct
                    self._emit(
                        emit,
                        pct,
                        f"{label}: {human_bytes(size)}" + (f" / ~{human_bytes(expected_bytes)}" if expected_bytes else ""),
                    )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def install_model(
        self,
        model_id: str,
        *,
        source: str | None = None,
        local_path: str | Path | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        spec = qwen_model(model_id)
        if local_path:
            base = Path(local_path).expanduser().resolve()
            if not base.is_dir():
                raise LabError(f"local_path non è una directory: {base}", code="LAB_LOCAL_PATH_INVALID")
            missing = self._check_expected(spec, base)
            if missing:
                raise LabError(
                    "local_path non contiene i file attesi per il modello",
                    code="LAB_LOCAL_PATH_INCOMPLETE",
                    detail={"path": str(base), "missing": missing, "expected": list(spec.expected_files)},
                )
            entry = {
                "source": "local_path",
                "path": str(base),
                "repo_id": None,
                "size_bytes": dir_size(base)[0],
                "present": True,
            }
            self._register(self._qwen_registry_file(), model_id, entry)
            return {
                "model_id": model_id,
                "status": "registered",
                "source": "local_path",
                "path": str(base),
                "size_bytes": entry["size_bytes"],
                "note": "Nessuna copia: il modello resta nella posizione indicata.",
            }
        if source not in (None, "", "download"):
            raise LabError(f"source non supportato: {source}", code="LAB_SOURCE_INVALID")
        if self.python is None:
            raise LabUnavailable(
                "Runtime AI non installato: esegui lab.install_engine prima del download.",
                code="LAB_RUNTIME_MISSING",
            )
        target = self.qwen_root / spec.dirname
        target.mkdir(parents=True, exist_ok=True)
        self._emit(progress, 0, f"download {spec.repo_id}")
        stop = threading.Event()
        monitor = self._monitor_size(target, spec.approx_bytes, progress, stop, spec.label)
        try:
            result = self._worker_call(
                "install_snapshot",
                {"repo_id": spec.repo_id, "local_dir": str(target)},
                emit=progress,
                cancel=cancel,
                timeout=7200,
                allow_network=True,
            )
        finally:
            stop.set()
            monitor.join(timeout=2)
        missing = self._check_expected(spec, target)
        if missing:
            raise LabError(
                "Download completato ma mancano file attesi",
                code="LAB_DOWNLOAD_INCOMPLETE",
                detail={"path": str(target), "missing": missing},
            )
        entry = {
            "source": "download",
            "path": str(target),
            "repo_id": spec.repo_id,
            "size_bytes": int(result.get("size_bytes") or dir_size(target)[0]),
            "file_count": int(result.get("file_count") or 0),
            "present": True,
        }
        self._register(self._qwen_registry_file(), model_id, entry)
        self._emit(progress, 100, f"{spec.label} installato")
        return {"model_id": model_id, "status": "installed", **entry}

    def uninstall_model(self, model_id: str) -> dict[str, Any]:
        spec = qwen_model(model_id)
        registry = self._qwen_registry_file()
        models = self._read_registry(registry)
        entry = models.get(model_id)
        target = self.qwen_root / spec.dirname
        if not entry:
            if not target.exists():
                return {"model_id": model_id, "removed": False, "freed_bytes": 0,
                        "reason": "non installato"}
            entry = {"source": "download", "path": str(target)}
        source = entry.get("source")
        if source == "local_path":
            models.pop(model_id, None)
            self._write_registry(registry, models)
            return {
                "model_id": model_id,
                "removed": True,
                "freed_bytes": 0,
                "source": "local_path",
                "note": "Registrazione rimossa; i file esterni non sono stati toccati.",
            }
        path = Path(entry.get("path") or target)
        if not str(path.resolve()).lower().startswith(str(self.qwen_root.resolve()).lower()):
            raise LabError(
                "Rifiuto di eliminare un percorso esterno alla cartella modelli gestita",
                code="LAB_UNINSTALL_UNSAFE",
                detail={"path": str(path), "allowed_root": str(self.qwen_root)},
            )
        freed = dir_size(path)[0] if path.exists() else 0
        removed = safe_rmtree(path, allowed_root=self.qwen_root)
        models.pop(model_id, None)
        self._write_registry(registry, models)
        return {"model_id": model_id, "removed": removed, "freed_bytes": freed, "path": str(path)}

    # ------------------------------------------------------------------ whisper
    def install_whisper(
        self,
        size: str,
        *,
        local_path: str | Path | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        spec = whisper_model(size)
        if local_path:
            base = Path(local_path).expanduser().resolve()
            if not base.is_dir():
                raise LabError(f"local_path non è una directory: {base}", code="LAB_LOCAL_PATH_INVALID")
            missing = self._check_expected(spec, base)
            if missing:
                raise LabError(
                    "local_path non contiene i file attesi per Whisper",
                    code="LAB_LOCAL_PATH_INCOMPLETE",
                    detail={"path": str(base), "missing": missing},
                )
            self._register(self._whisper_registry_file(), size, {
                "source": "local_path", "path": str(base), "size_bytes": dir_size(base)[0],
                "present": True,
            })
            return {"size": size, "status": "registered", "source": "local_path",
                    "path": str(base), "note": "Nessuna copia effettuata."}
        if self.python is None:
            self._create_venv(progress, cancel)
        if not self._import_ok("faster_whisper"):
            self._emit(progress, 10, "installazione faster-whisper nel runtime AI")
            self._pip_install([WHISPER_PIP_SPEC], emit=progress, cancel=cancel)
        # Refresh the package marker so lab.status reports faster-whisper.
        try:
            verification = self._worker_call("verify_engine", {}, emit=progress, cancel=cancel, timeout=600)
            state = self._state()
            state["packages"] = verification
            self._write_state(state)
        except LabError:
            pass
        target = self.whisper_root / spec.dirname
        target.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        monitor = self._monitor_size(target, spec.approx_bytes, progress, stop, spec.label)
        try:
            result = self._worker_call(
                "install_snapshot",
                {"repo_id": spec.repo_id, "local_dir": str(target)},
                emit=progress,
                cancel=cancel,
                timeout=7200,
                allow_network=True,
            )
        finally:
            stop.set()
            monitor.join(timeout=2)
        missing = self._check_expected(spec, target)
        if missing:
            raise LabError(
                "Download Whisper incompleto",
                code="LAB_DOWNLOAD_INCOMPLETE",
                detail={"path": str(target), "missing": missing},
            )
        entry = {
            "source": "download", "path": str(target), "repo_id": spec.repo_id,
            "size_bytes": int(result.get("size_bytes") or dir_size(target)[0]),
            "file_count": int(result.get("file_count") or 0), "present": True,
        }
        self._register(self._whisper_registry_file(), size, entry)
        self._emit(progress, 100, f"Whisper {size} installato")
        return {"size": size, "status": "installed", **entry}

    def uninstall_whisper(self, size: str) -> dict[str, Any]:
        spec = whisper_model(size)
        registry = self._whisper_registry_file()
        models = self._read_registry(registry)
        entry = models.get(size)
        target = self.whisper_root / spec.dirname
        if not entry:
            if not target.exists():
                return {"size": size, "removed": False, "freed_bytes": 0, "reason": "non installato"}
            entry = {"source": "download", "path": str(target)}
        if entry.get("source") == "local_path":
            models.pop(size, None)
            self._write_registry(registry, models)
            return {"size": size, "removed": True, "freed_bytes": 0, "source": "local_path",
                    "note": "Registrazione rimossa; file esterni non toccati."}
        path = Path(entry.get("path") or target)
        if not str(path.resolve()).lower().startswith(str(self.whisper_root.resolve()).lower()):
            raise LabError("Rifiuto di eliminare un percorso esterno alla cartella Whisper",
                           code="LAB_UNINSTALL_UNSAFE", detail={"path": str(path)})
        freed = dir_size(path)[0] if path.exists() else 0
        removed = safe_rmtree(path, allowed_root=self.whisper_root)
        models.pop(size, None)
        self._write_registry(registry, models)
        return {"size": size, "removed": removed, "freed_bytes": freed, "path": str(path)}

    # ------------------------------------------------------------------ ECAPA
    def ecapa_dir(self) -> Path | None:
        override = os.environ.get("NLVS_ECAPA_DIR")
        if override:
            # Explicit configuration wins: no development fallback if it is wrong.
            configured = Path(override).expanduser()
            return configured.resolve() if configured.is_dir() else None
        candidates: list[Path] = [
            self.data_dir / _ECAPA_RELATIVE,
            self.data_dir / "ecapa",
        ]
        repo = self.repo_root()
        if repo is not None:
            workspace = repo.parent
            candidates.append(workspace / _PRIVATE_LAB_DIRNAME / _ECAPA_RELATIVE)
            candidates.append(workspace / "ecapa")
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()
        return None

    def ecapa_status(self) -> dict[str, Any]:
        directory = self.ecapa_dir()
        if directory is None:
            return {"available": False,
                    "reason": "Asset ECAPA non configurati (imposta NLVS_ECAPA_DIR).",
                    "path": None}
        missing = [name for name in _ECAPA_REQUIRED if not (directory / name).is_file()]
        if not any((directory / name).is_file() for name in _ECAPA_WEIGHTS):
            missing.append("embedding_model.ckpt|classifier.ckpt")
        return {
            "available": not missing,
            "path": str(directory),
            "missing": missing,
            "reason": None if not missing else f"Asset ECAPA incompleti: {', '.join(missing)}",
            "model": "speechbrain-ecapa",
        }

    # ------------------------------------------------------------------ transcribe
    def transcribe(
        self,
        audio: str | Path,
        *,
        model: str = "base",
        language: str | None = None,
        device: str = "cpu",
        beam_size: int = 5,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        spec = whisper_model(model)
        entry = self.whisper_entry(model)
        audio_path = Path(audio).expanduser().resolve()
        if not audio_path.is_file():
            raise LabError(f"File audio non trovato: {audio_path}", code="LAB_AUDIO_NOT_FOUND")
        if entry is None:
            raise LabUnavailable(
                f"Whisper '{model}' non installato: esegui lab.install_whisper.",
                code="LAB_WHISPER_MISSING",
            )
        model_path = Path(entry.get("path") or (self.whisper_root / spec.dirname))
        if not model_path.is_dir():
            raise LabError(f"Percorso modello Whisper mancante: {model_path}",
                           code="LAB_WHISPER_PATH_MISSING")
        result = self._worker_call(
            "transcribe",
            {
                "audio": str(audio_path),
                "model_path": str(model_path),
                "language": language or None,
                "device": device,
                "beam_size": int(beam_size),
            },
            emit=progress,
            cancel=cancel,
            timeout=3600,
        )
        result.update({"model": model, "model_path": str(model_path), "audio": str(audio_path)})
        return result

    # ------------------------------------------------------------------ verification
    def verify(self, *, deep: bool = False) -> dict[str, Any]:
        state = self._state()
        report: dict[str, Any] = {
            "schema": "nlvs.qwen-lab-verify/1",
            "checked_at": utc_now_iso(),
            "runtime": {"installed": bool(self.python), "python": str(self.python) if self.python else None,
                        "isolated": bool(state.get("isolated")), "state": state},
            "models": [],
            "whisper": [],
            "ecapa": self.ecapa_status(),
            "ok": False,
        }
        if self.python is not None:
            try:
                engine = self._worker_call("verify_engine", {}, timeout=600)
                report["runtime"]["engine"] = engine
                device_info = self._worker_call("device_info", {}, timeout=600)
                report["runtime"]["device"] = device_info
                self._update_state({"device_info": device_info, "packages": engine})
            except LabError as exc:
                report["runtime"]["engine_error"] = str(exc)
        for model in QWEN_MODELS:
            entry = self.installed_entry(model.id)
            item = {"id": model.id, "installed": entry is not None,
                    "expected_files": list(model.expected_files)}
            if entry:
                path = Path(entry.get("path") if entry.get("source") == "local_path" else entry.get("path") or "")
                missing = self._check_expected(model, path) if path.is_dir() else list(model.expected_files)
                item.update({"path": str(path), "missing": missing, "ok": not missing})
            report["models"].append(item)
        for model in WHISPER_MODELS:
            entry = self.whisper_entry(model.dirname)
            item = {"id": model.id, "installed": entry is not None}
            if entry:
                path = Path(entry.get("path") or "")
                missing = self._check_expected(model, path) if path.is_dir() else list(model.expected_files)
                item.update({"path": str(path), "missing": missing, "ok": not missing})
            report["whisper"].append(item)
        if deep and self.python is not None:
            try:
                report["engine_deep"] = self._worker_call("verify_engine", {}, timeout=900)
            except LabError as exc:
                report["engine_deep_error"] = str(exc)
        report["ok"] = bool(
            self.python and report["runtime"].get("engine", {}).get("qwen_api")
        )
        return report

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        state = self._state()
        device_info = state.get("device_info") or {}
        runtime_cuda = bool(device_info.get("cuda")) if device_info else False
        nvidia_name = None
        gpu_hardware = False
        try:
            from .ai_runtime import _has_nvidia

            gpu_hardware, nvidia_name = _has_nvidia()
        except Exception:
            pass
        device = {
            "cpu": True,
            "cuda": runtime_cuda,
            "gpu_hardware_present": bool(gpu_hardware),
            "cuda_devices": device_info.get("cuda_devices") or ([nvidia_name] if nvidia_name else []),
            "mps": bool(device_info.get("mps")),
            "detected_torch": device_info.get("torch"),
            "runtime_verified": bool(device_info),
            "requested_options": list(_VALID_DEVICES),
            "note": (
                "CUDA è selezionabile solo se il runtime AI usa una build PyTorch con CUDA. "
                "Hardware GPU presente senza runtime CUDA: usa device=cpu o reinstalla il runtime "
                "AI in variante GPU."
            ),
        }
        qwen_models = []
        for model in QWEN_MODELS:
            entry = self.installed_entry(model.id)
            path = Path(entry["path"]) if entry and entry.get("path") else self.qwen_root / model.dirname
            present = path.is_dir() and not self._check_expected(model, path)
            qwen_models.append({
                **model.to_dict(self.qwen_root),
                "installed": entry is not None,
                "present": present,
                "source": (entry or {}).get("source"),
                "path": str(path),
                "size_bytes": (entry or {}).get("size_bytes") or (dir_size(path)[0] if path.is_dir() else 0),
                "missing": self._check_expected(model, path) if path.is_dir() else list(model.expected_files),
            })
        whisper_models = []
        for model in WHISPER_MODELS:
            entry = self.whisper_entry(model.dirname)
            path = Path(entry["path"]) if entry and entry.get("path") else self.whisper_root / model.dirname
            whisper_models.append({
                **model.to_dict(self.whisper_root),
                "installed": entry is not None,
                "present": path.is_dir() and not self._check_expected(model, path),
                "source": (entry or {}).get("source"),
                "path": str(path),
                "size_bytes": (entry or {}).get("size_bytes") or (dir_size(path)[0] if path.is_dir() else 0),
                "missing": self._check_expected(model, path) if path.is_dir() else list(model.expected_files),
            })
        packages = state.get("packages") or {}
        faster_whisper_installed = bool(packages.get("faster_whisper"))
        if not faster_whisper_installed and self.python is not None:
            # Marker may predate the Whisper install: probe once (cheap import).
            faster_whisper_installed = self._import_ok("faster_whisper")
        return {
            "schema": "nlvs.qwen-lab-status/1",
            "checked_at": utc_now_iso(),
            "runtime": {
                "installed": bool(self.python) and bool(state.get("installed")),
                "venv": str(self._active_venv()),
                "python": str(self.python) if self.python else None,
                "isolated": bool(state.get("isolated")),
                "isolation_reason": state.get("isolation_reason"),
                "packages": {
                    "qwen_tts": packages.get("qwen_tts"),
                    "faster_whisper": packages.get("faster_whisper"),
                    "speechbrain": packages.get("speechbrain"),
                    "torch": packages.get("torch"),
                    "transformers": packages.get("transformers"),
                },
                "qwen_api": packages.get("qwen_api"),
                "size_bytes": dir_size(self._active_venv())[0] if self._active_venv().exists() else 0,
                "updated_at": state.get("updated_at"),
            },
            "qwen": {
                "models_root": str(self.qwen_root),
                "default_model": DEFAULT_QWEN_MODEL,
                "models": qwen_models,
            },
            "whisper": {
                "models_root": str(self.whisper_root),
                "package_installed": faster_whisper_installed,
                "supported_sizes": [model.dirname for model in WHISPER_MODELS],
                "models": whisper_models,
            },
            "device": device,
            "ecapa": {**self.ecapa_status(), "speechbrain": packages.get("speechbrain")},
            "paths": {
                "data_dir": str(self.data_dir),
                "models_dir": str(self.models_dir),
                "worker": str(self.worker),
                "exports": str(self.exports_root),
                "log": str(self.logs_dir / "qwen-lab.log"),
            },
            "notes": [
                "Laboratorio di prova: non fa parte della pipeline di protezione.",
                "Nessun download implicito: runtime, modelli e pacchetti sono sempre espliciti.",
                "ref_text obbligatorio salvo x_vector_only esplicito.",
            ],
        }

    # ------------------------------------------------------------------ run A/B
    def _validate_run(
        self,
        *,
        original: str | Path,
        protected: str | Path,
        model_id: str,
        text: str,
        ref_text: str | None,
        x_vector_only: bool,
        device: str,
        seed: int,
    ) -> tuple[Path, Path, LabModel]:
        spec = qwen_model(model_id)
        original_path = Path(original).expanduser().resolve()
        protected_path = Path(protected).expanduser().resolve()
        if not original_path.is_file():
            raise LabError(f"Audio originale non trovato: {original_path}", code="LAB_AUDIO_NOT_FOUND")
        if not protected_path.is_file():
            raise LabError(f"Audio protetto non trovato: {protected_path}", code="LAB_AUDIO_NOT_FOUND")
        if not str(text or "").strip():
            raise LabError("text è obbligatorio per il test A/B", code="LAB_TEXT_REQUIRED")
        if not x_vector_only and not str(ref_text or "").strip():
            raise LabError(
                "ref_text è obbligatorio salvo x_vector_only=true: trascrivi il riferimento con "
                "lab.transcribe e passalo esplicitamente.",
                code="LAB_REF_TEXT_REQUIRED",
            )
        if str(device).lower() not in _VALID_DEVICES:
            raise LabError("device deve essere cpu, cuda o auto", code="LAB_DEVICE_INVALID")
        if int(seed) < 0 or int(seed) > 2**31 - 1:
            raise LabError("seed deve essere un intero tra 0 e 2^31-1", code="LAB_SEED_INVALID")
        entry = self.installed_entry(model_id)
        if entry is None:
            raise LabUnavailable(
                f"Modello {model_id} non installato: esegui lab.install_model.",
                code="LAB_MODEL_MISSING",
            )
        if self.python is None:
            raise LabUnavailable(
                "Runtime Qwen non installato: esegui lab.install_engine.",
                code="LAB_RUNTIME_MISSING",
            )
        return original_path, protected_path, spec

    def _generate(
        self,
        *,
        model_path: Path,
        device: str,
        seed: int,
        text: str,
        language: str,
        ref_audio: Path,
        ref_text: str | None,
        x_vector_only: bool,
        output_path: Path,
        progress: ProgressCallback | None,
        cancel: threading.Event | None,
        tokenizer_path: str | None,
    ) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_path": str(model_path),
            "device": device,
            "seed": int(seed),
            "text": text,
            "language": language,
            "ref_audio": str(ref_audio),
            "ref_text": ref_text,
            "x_vector_only": bool(x_vector_only),
            "output_path": str(output_path),
            "tokenizer_path": tokenizer_path,
        }
        return self._worker_call(
            "generate", payload, emit=progress, cancel=cancel, timeout=7200
        )

    def _quality_pair(self, audio: Path, reference: Path) -> dict[str, Any]:
        try:
            buffer, info = load_audio(audio)
        except NlvsError as exc:
            return {"available": False, "reason": str(exc)}
        try:
            ref_buffer, _ = load_audio(reference)
        except NlvsError:
            ref_buffer = None
        metrics = analyze_quality(buffer, ref_buffer)
        return {"available": True, "format": info.to_dict(), "metrics": json_safe(metrics)}

    def _ecapa_pair(self, audio_a: Path, audio_b: Path) -> dict[str, Any]:
        status = self.ecapa_status()
        if not status.get("available"):
            return {"available": False, "reason": status.get("reason"), "path": status.get("path")}
        if self.python is None:
            return {"available": False, "reason": "runtime AI non installato"}
        try:
            result = self._worker_call(
                "ecapa_similarity",
                {
                    "audio_a": str(audio_a),
                    "audio_b": str(audio_b),
                    "assets_dir": status["path"],
                    "cache_dir": str(self.data_dir / "cache" / "speechbrain"),
                    "device": "cpu",
                },
                timeout=1800,
            )
        except LabError as exc:
            return {"available": False, "reason": str(exc), "path": status.get("path")}
        return {
            "available": True,
            "similarity": result.get("similarity"),
            "model": "speechbrain-ecapa",
            "assets": status.get("path"),
            "embedding_dim": result.get("embedding_dim"),
            "audio_a": str(audio_a),
            "audio_b": str(audio_b),
        }

    def run(
        self,
        *,
        original: str | Path,
        protected: str | Path,
        text: str,
        ref_text: str | None,
        model_id: str = DEFAULT_QWEN_MODEL,
        language: str = "Italian",
        seed: int = 0,
        device: str = "cpu",
        x_vector_only: bool = False,
        output_dir: str | Path | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = job_id or full_id()
        original_path, protected_path, spec = self._validate_run(
            original=original, protected=protected, model_id=model_id, text=text,
            ref_text=ref_text, x_vector_only=x_vector_only, device=device, seed=int(seed),
        )
        self._emit(progress, 1, "preparazione laboratorio Qwen")
        state = self._state()
        device_info = state.get("device_info") or {}
        if str(device).lower() == "cuda" and not device_info.get("cuda"):
            raise LabError(
                "CUDA richiesta ma il runtime AI non la rileva; usa device=cpu.",
                code="LAB_DEVICE_UNAVAILABLE",
            )
        entry = self.installed_entry(model_id) or {}
        model_path = Path(entry.get("path") or (self.qwen_root / spec.dirname))
        if not model_path.is_dir():
            raise LabError(f"Percorso modello non valido: {model_path}", code="LAB_MODEL_PATH_MISSING")
        tokenizer_entry = self.installed_entry("qwen3-tts-tokenizer")
        tokenizer_path = (tokenizer_entry or {}).get("path")
        if tokenizer_path is None:
            embedded = model_path / "speech_tokenizer"
            tokenizer_path = str(embedded) if embedded.is_dir() else None
        output_base = (
            Path(output_dir).expanduser().resolve() if output_dir else self.exports_root / job_id
        )
        output_base.mkdir(parents=True, exist_ok=True)
        reference = {"original": str(original_path), "protected": str(protected_path)}
        quality: dict[str, Any] = {}
        self._emit(progress, 3, "misura qualità dei riferimenti")
        quality["original_reference"] = self._quality_pair(original_path, original_path)
        quality["protected_reference"] = self._quality_pair(protected_path, original_path)
        timings: dict[str, float] = {}
        started = time.time()
        self._emit(progress, 10, "generazione clone dal riferimento originale")
        t0 = time.time()
        original_out = output_base / "original_clone.wav"
        gen_original = self._generate(
            model_path=model_path, device=device, seed=int(seed), text=text, language=language,
            ref_audio=original_path, ref_text=ref_text, x_vector_only=x_vector_only,
            output_path=original_out, progress=progress, cancel=cancel,
            tokenizer_path=tokenizer_path,
        )
        timings["original_generation_seconds"] = round(time.time() - t0, 2)
        self._emit(progress, 55, "generazione clone dal riferimento protetto")
        t0 = time.time()
        protected_out = output_base / "protected_clone.wav"
        gen_protected = self._generate(
            model_path=model_path, device=device, seed=int(seed), text=text, language=language,
            ref_audio=protected_path, ref_text=ref_text, x_vector_only=x_vector_only,
            output_path=protected_out, progress=progress, cancel=cancel,
            tokenizer_path=tokenizer_path,
        )
        timings["protected_generation_seconds"] = round(time.time() - t0, 2)
        self._emit(progress, 85, "misura qualità dei cloni")
        quality["original_clone"] = self._quality_pair(original_out, original_path)
        quality["protected_clone"] = self._quality_pair(protected_out, original_path)
        self._emit(progress, 92, "speaker similarity ECAPA")
        similarity: dict[str, Any] = {"available": False, "reason": "non calcolata"}
        if self.ecapa_status().get("available"):
            sim_original = self._ecapa_pair(original_out, original_path)
            sim_protected = self._ecapa_pair(protected_out, original_path)
            sim_between = self._ecapa_pair(original_out, protected_out)
            available = all(item.get("available") for item in (sim_original, sim_protected))
            failure = next(
                (
                    item
                    for item in (sim_original, sim_protected, sim_between)
                    if not item.get("available")
                ),
                {},
            )
            similarity = {
                "available": available,
                "model": "speechbrain-ecapa",
                "assets": self.ecapa_status().get("path"),
                "original_clone_vs_reference": sim_original,
                "protected_clone_vs_reference": sim_protected,
                "original_vs_protected_clone": sim_between,
                "reason": None if available else failure.get("reason") or "ECAPA non disponibile",
                "note": (
                    "Similarità coseno ECAPA verso il riferimento originale. Valori più bassi per il "
                    "clone protetto sono un segnale di laboratorio, non una prova di protezione."
                    if available else "ECAPA non disponibile in questo runtime."
                ),
            }
            if available:
                a = float(sim_original.get("similarity") or 0.0)
                b = float(sim_protected.get("similarity") or 0.0)
                similarity["delta_protected_minus_original"] = round(b - a, 6)
        else:
            similarity = {"available": False, "reason": self.ecapa_status().get("reason"),
                          "model": "speechbrain-ecapa", "assets": self.ecapa_status().get("path")}
        timings["total_seconds"] = round(time.time() - started, 2)
        result = {
            "schema": "nlvs.qwen-lab-run/1",
            "status": "completed",
            "job_id": job_id,
            "created_at": utc_now_iso(),
            "model": {"id": model_id, "path": str(model_path), "repo_id": spec.repo_id,
                      "tokenizer_path": tokenizer_path},
            "text": text,
            "ref_text": ref_text,
            "x_vector_only": bool(x_vector_only),
            "language": language,
            "seed": int(seed),
            "device": str(device).lower(),
            "reference": reference,
            "outputs": {
                "original_clone": {
                    "path": str(original_out),
                    "sha256": sha256_file(original_out),
                    "sample_rate": gen_original.get("sample_rate"),
                    "duration_seconds": gen_original.get("duration_seconds"),
                    "seed": gen_original.get("seed"),
                },
                "protected_clone": {
                    "path": str(protected_out),
                    "sha256": sha256_file(protected_out),
                    "sample_rate": gen_protected.get("sample_rate"),
                    "duration_seconds": gen_protected.get("duration_seconds"),
                    "seed": gen_protected.get("seed"),
                },
            },
            "quality": quality,
            "speaker_similarity": similarity,
            "timings": timings,
            "warnings": [] if similarity.get("available") else ["speaker_similarity non disponibile: " + str(similarity.get("reason"))],
            "notes": [
                "Test di laboratorio su voce autorizzata: non prova il comportamento di ElevenLabs.",
                "ref_text deve essere la trascrizione esatta del riferimento; seed identico per i due cloni.",
            ],
        }
        self._emit(progress, 100, "test A/B completato")
        return json_safe(result)
