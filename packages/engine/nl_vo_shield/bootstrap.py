"""Executable bootstrap manifests for optional protection engines.

The component manager copies an imported directory; this module can instead
reconstruct a component from official pinned sources, an isolated Python venv
created with the embedded bootstrap interpreter and downloadable checkpoints.
Everything is written below ``<data>/components`` and ``<data>/runtime``.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .errors import NlvsError

HF = "https://huggingface.co"


class BootstrapError(NlvsError):
    code = "COMPONENT_BOOTSTRAP_ERROR"


def _venv_python(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python3"


def _bootstrap_python() -> Path:
    try:
        from .ai_runtime import bootstrap_python

        return bootstrap_python()
    except Exception:
        return Path(sys.executable)


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = 7200) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True,
                               text=True, timeout=timeout, env=env)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "")[-2000:]
        raise BootstrapError("Comando fallito: " + " ".join(command[:3]) + "\n" + tail)
    return completed


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "NL-VO-Shield/1.2.6"})
    with urllib.request.urlopen(request, timeout=300) as response, open(tmp, "wb") as fh:
        shutil.copyfileobj(response, fh, 1024 * 1024)
    tmp.replace(target)


def _source(url: str, commit: str, target: Path, required: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nlvs-src-") as tmp:
        archive = Path(tmp) / "source.zip"
        _download(f"{url}/archive/{commit}.zip", archive)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots:
            raise BootstrapError("Archivio sorgente vuoto")
        shutil.copytree(roots[0], target, dirs_exist_ok=False)
    missing = [rel for rel in required if not (target / rel).is_file()]
    if missing:
        shutil.rmtree(target, ignore_errors=True)
        raise BootstrapError("Sorgente incompleta; mancano " + ", ".join(missing))


def _ensure_venv(venv: Path, packages: list[str], torch_cpu: bool = True) -> Path:
    python = _venv_python(venv)
    if not python.is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        base = str(_bootstrap_python())
        try:
            _run([base, "-m", "venv", str(venv)])
        except BootstrapError:
            _run([base, "-m", "pip", "install", "virtualenv"], timeout=1800)
            _run([base, "-m", "virtualenv", str(venv)])
    python = _venv_python(venv)
    marker = venv / ".nlvs-packages.json"
    wanted = sorted(packages + (["torch-cpu"] if torch_cpu else []))
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == wanted:
            return python
    except Exception:
        pass
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
         timeout=1800)
    if torch_cpu:
        _run([str(python), "-m", "pip", "install", "torch==2.3.1", "torchaudio==2.3.1",
              "--index-url", "https://download.pytorch.org/whl/cpu"], timeout=7200)
    normal = [p for p in packages if not p.startswith("speechbrain")]
    if normal:
        _run([str(python), "-m", "pip", "install", *normal], timeout=7200)
    if any(p.startswith("speechbrain") for p in packages):
        _run([str(python), "-m", "pip", "install", "--no-deps", "speechbrain==1.0.0"], timeout=1800)
    marker.write_text(json.dumps(wanted), encoding="utf-8")
    return python


MANIFESTS: dict[str, dict[str, Any]] = {
    "enkidu": {
        "source": ("https://github.com/NoobCodeNameless/Enkidu", "93c388e4ec5adfa66eabb230c4d8790d300f23a9"),
        "required": ["core/enkidu.py"],
        "runtime": "ai",
        "packages": ["numpy<2", "scipy<1.14", "soundfile", "librosa", "transformers==4.46.3",
                     "huggingface_hub>=0.30", "speechbrain==1.0.0", "safetensors"],
        "models": [{"kind": "hf_snapshot", "repo": "speechbrain/spkrec-ecapa-voxceleb",
                    "target": "upstream_assets/shared/ECAPA"}],
        "verify": "import torch, soundfile, librosa, speechbrain",
    },
    "mimic_blocker": {
        "source": ("https://github.com/yugwangyeol/Mimic-Blocker", "c7b0ae79bc647ce911a50b61262d3d0906185584"),
        "required": ["model/model.py", "model/checkpoints/generator_wavlm.pth"],
        "runtime": "ai",
        "packages": ["numpy<2", "scipy<1.14", "soundfile", "librosa", "transformers==4.46.3",
                     "huggingface_hub>=0.30"],
        "models": [],
        "verify": "import torch, soundfile, transformers",
    },
    "safespeech": {
        "source": ("https://github.com/wxzyd123/SafeSpeech", "3345f42e2891d116acbb8f3c5a49bb0c7534fc6e"),
        "required": ["protect.py"],
        "runtime": "safespeech",
        "packages": ["numpy<2", "scipy", "soundfile", "librosa", "transformers==4.46.3",
                     "tokenizers==0.20.3", "huggingface_hub==0.36.2", "pyyaml", "torch_stoi",
                     "loguru", "pymcd", "pysndfx", "tqdm", "matplotlib", "numba", "phonemizer",
                     "tensorboard", "Unidecode", "amfm_decompy", "jieba", "pypinyin", "cn2an",
                     "av", "num2words", "requests", "jaconv", "psutil", "GPUtil",
                     "vector_quantize_pytorch"],
        "models": [
            {"kind": "hf_file", "repo": "OedoSoldier/Bert-VITS2-2.3", "file": "G_0.pth",
             "target": "components/safespeech/checkpoints/base_models/G_0.pth"},
            {"kind": "hf_file", "repo": "OedoSoldier/Bert-VITS2-2.3", "file": "D_0.pth",
             "target": "components/safespeech/checkpoints/base_models/D_0.pth"},
            {"kind": "hf_file", "repo": "OedoSoldier/Bert-VITS2-2.3", "file": "WD_0.pth",
             "target": "components/safespeech/checkpoints/base_models/WD_0.pth"},
            {"kind": "hf_file", "repo": "OedoSoldier/Bert-VITS2-2.3", "file": "DUR_0.pth",
             "target": "components/safespeech/checkpoints/base_models/DUR_0.pth"},
        ],
        "verify": "import torch, soundfile, librosa, transformers",
    },
    "e2e_vguard": {
        "source": ("https://github.com/wxzyd123/E2E-VGuard", "f303c1d74d0a0309e397b720baf3b22458d97268"),
        "required": ["protect.py", "E2E_VGuard.py"],
        "runtime": "e2e",
        "packages": ["numpy<2", "scipy", "soundfile", "librosa", "transformers==4.40.2",
                     "tokenizers==0.19.1", "huggingface_hub==0.24.7", "gradio", "pytorch-lightning",
                     "onnx", "onnx2torch", "phonemizer", "cn2an", "pypinyin", "jieba_fast",
                     "Unidecode", "LangSegment"],
        "models": [],
        "verify": "import torch, soundfile, transformers, phonemizer",
        "manual": ["checkpoints/VITS/pretrained_ljs.pth"],
    },
}


def install(data_dir: Path, cid: str, progress: Callable[[str, int, str], None] | None = None) -> dict[str, Any]:
    data_dir = Path(data_dir)
    spec = MANIFESTS.get(cid)
    if spec is None:
        raise BootstrapError(f"Nessun manifest bootstrap per {cid}")
    emit = progress or (lambda *_args: None)
    component = data_dir / "components" / cid
    if component.exists():
        raise BootstrapError("Componente già presente; rimuoverlo prima")
    url, commit = spec["source"]
    emit("source", 5, f"Download sorgente {cid}")
    _source(url, commit, component, spec["required"])

    if cid == "safespeech":
        bert_dir = component / "bert"
        bert_dir.mkdir(parents=True, exist_ok=True)
        (bert_dir / "bert_models.json").write_text("{}", encoding="utf-8")
    if cid == "e2e_vguard":
        _patch_e2e(component)
        checkpoint = component / "checkpoints" / "VITS" / "pretrained_ljs.pth"
        if not checkpoint.is_file():
            emit("manual", 15, "Serve pretrained_ljs.pth: importalo dalla GUI")
    emit("runtime", 25, "Creazione ambiente Python isolato")
    runtime_root = data_dir / "runtime" / spec["runtime"]
    venv = runtime_root / "venv"
    python = _ensure_venv(venv, spec["packages"])
    emit("verify", 80, "Verifica dipendenze")
    _run([str(python), "-c", spec["verify"]], timeout=300)

    for model in spec["models"]:
        target = data_dir / model["target"]
        if model["kind"] == "hf_file":
            if not target.is_file():
                emit("models", 85, f"Download {model['file']}")
                _download(f"{HF}/{model['repo']}/resolve/main/{model['file']}", target)
        elif model["kind"] == "hf_snapshot":
            if not (target / "embedding_model.ckpt").is_file():
                emit("models", 85, f"Download {model['repo']}")
                _run([str(python), "-c",
                      "import sys; from huggingface_hub import snapshot_download; "
                      f"snapshot_download({model['repo']!r}, local_dir={str(target)!r})"], timeout=7200)
    missing = [rel for rel in spec.get("manual", []) if not (component / rel).is_file()]
    state = {"id": cid, "component": str(component), "runtime": str(venv),
             "python": str(python), "version": "1.2.6", "manual_missing": missing}
    (component / ".nlvs-bootstrap.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _patch_e2e(component: Path) -> None:
    """Apply the minimal, documented E2E-VGuard adaptations after download."""
    e2e = component / "E2E_VGuard.py"
    if e2e.is_file():
        text = e2e.read_text(encoding="utf-8")
        text = text.replace("import whisper", "try:\n    import whisper\nexcept Exception:\n    whisper = None")
        text = text.replace("import nemo.collections.asr as nemo_asr",
                            "try:\n    import nemo.collections.asr as nemo_asr\nexcept Exception:\n    nemo_asr = None")
        text = text.replace(
            'self.encoder_4 = AutoModel.from_pretrained("microsoft/wavlm-base-plus")',
            'self.encoder_4 = AutoModel.from_pretrained(str(__import__("pathlib").Path(__file__).resolve().parent / "checkpoints" / "wavlm"))',
        )
        e2e.write_text(text, encoding="utf-8")
    protect = component / "protect.py"
    if protect.is_file():
        text = protect.read_text(encoding="utf-8")
        text = text.replace('device = torch.device("cuda")',
                            'device = torch.device(os.environ.get("NLVS_E2E_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu"))')
        if "import os" not in text.split("\n\n")[0]:
            text = "import os\n" + text
        protect.write_text(text, encoding="utf-8")
