"""Model/cache environment isolation.

Everything the AI stack may cache (torch, Hugging Face, audioseal) is forced
inside the application data directory *before* torch/audioseal are imported.
This prevents silent writes to global user caches and keeps the product
self-contained. Downloads are never triggered by this module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Set before any torch/audioseal import: disables torch.compile (AudioSeal's
# moshi wrapper would otherwise require a C compiler on Windows) and keeps
# the run deterministic and CPU friendly.
_FORCE_ENV = {
    "NO_TORCH_COMPILE": "1",
    "TORCH_COMPILE_DISABLE": "1",
    "TORCHDYNAMO_DISABLE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    "TOKENIZERS_PARALLELISM": "false",
}

_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

_APPLIED: dict[str, str] = {}
_TORCH_IMPORTED_AT_APPLY: bool | None = None


def is_torch_imported() -> bool:
    return "torch" in sys.modules


def apply_model_env(models_dir: Path, cache_dir: Path, *, allow_download: bool = False) -> dict[str, str]:
    """Point every model/cache location at the application data directory.

    Safe to call multiple times; the last call wins. When torch is already
    imported some library-level defaults were frozen, which ``doctor`` reports.
    """
    global _TORCH_IMPORTED_AT_APPLY
    models_dir = Path(models_dir)
    cache_dir = Path(cache_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    hub = cache_dir / "huggingface"
    values = {
        # All model weights stay under data_dir/models (committed requirement).
        "AUDIOSEAL_CACHE_DIR": str(models_dir / "audioseal"),
        # Framework caches stay under data_dir/cache.
        "TORCH_HOME": str(cache_dir / "torch"),
        "TORCHINDUCTOR_CACHE_DIR": str(cache_dir / "torchinductor"),
        "HF_HOME": str(hub),
        "HF_HUB_CACHE": str(hub / "hub"),
        "TRANSFORMERS_CACHE": str(hub / "transformers"),
        "XDG_CACHE_HOME": str(cache_dir / "xdg"),
        "TRITON_CACHE_DIR": str(cache_dir / "triton"),
        "MPLCONFIGDIR": str(cache_dir / "matplotlib"),
    }
    values.update(_FORCE_ENV)
    for key in _OFFLINE_ENV:
        values[key] = "0" if allow_download else "1"
    for key, value in values.items():
        os.environ[key] = value
    _TORCH_IMPORTED_AT_APPLY = is_torch_imported()
    _APPLIED.clear()
    _APPLIED.update(values)
    for sub in ("audioseal",):
        (models_dir / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("torch", "torchinductor", "huggingface", "xdg", "triton", "matplotlib"):
        (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    return dict(_APPLIED)


def applied() -> dict[str, str]:
    return dict(_APPLIED)


def torch_imported_at_apply() -> bool | None:
    return _TORCH_IMPORTED_AT_APPLY


def model_env_report() -> dict[str, Any]:
    """Report the effective model environment for doctor/diagnostics."""
    keys = (
        "AUDIOSEAL_CACHE_DIR",
        "TORCH_HOME",
        "HF_HOME",
        "HF_HUB_CACHE",
        "XDG_CACHE_HOME",
        "NO_TORCH_COMPILE",
        "HF_HUB_OFFLINE",
    )
    return {
        "values": {key: os.environ.get(key) for key in keys},
        "torch_imported_at_apply": _TORCH_IMPORTED_AT_APPLY,
        "torch_imported_now": is_torch_imported(),
    }
