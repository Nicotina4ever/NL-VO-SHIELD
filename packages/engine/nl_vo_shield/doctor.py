"""Read-only diagnostics: runtime, data dirs, models, tools and engine statuses.

``doctor`` never downloads anything and never modifies user files. It reports
honest statuses (ok / warn / fail) plus the real reason for every unavailable
engine.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__, env
from .util import dir_size, human_bytes, utc_now_iso

_REQUIRED_PACKAGES = ("numpy", "soundfile", "scipy", "cryptography")
_OPTIONAL_PACKAGES = ("torch", "torchaudio", "audioseal", "pyloudnorm", "librosa")


def _check(check_id: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"id": check_id, "status": status, "detail": detail, **extra}


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def run_doctor(ctx: Any) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    settings = ctx.settings_public()
    models_dir = Path(settings["models_dir_resolved"])
    cache_dir = Path(settings["cache_dir_resolved"])

    # ---------------------------------------------------------------- runtime
    version_info = sys.version_info
    python_status = "ok" if version_info >= (3, 11) else "warn"
    checks.append(
        _check(
            "python.version",
            python_status,
            f"Python {sys.version.split()[0]} ({platform.python_implementation()})",
            executable=sys.executable,
            required=">=3.11",
        )
    )
    checks.append(
        _check(
            "platform",
            "ok",
            f"{platform.system()} {platform.release()} ({platform.machine()})",
        )
    )

    # ---------------------------------------------------------------- packages
    required_missing: list[str] = []
    required_info: dict[str, str | None] = {}
    for name in _REQUIRED_PACKAGES:
        present = _has_module(name)
        required_info[name] = _version(name)
        if not present:
            required_missing.append(name)
    checks.append(
        _check(
            "packages.required",
            "ok" if not required_missing else "fail",
            "Base requirements present" if not required_missing
            else f"Missing base packages: {', '.join(required_missing)}",
            packages=required_info,
        )
    )
    optional_missing: list[str] = []
    optional_info: dict[str, str | None] = {}
    for name in _OPTIONAL_PACKAGES:
        present = _has_module(name)
        optional_info[name] = _version(name)
        if not present:
            optional_missing.append(name)
    checks.append(
        _check(
            "packages.optional",
            "ok" if not optional_missing else "warn",
            "Optional AI stack installed" if not optional_missing
            else f"Optional packages not installed: {', '.join(optional_missing)} "
                 "(the base release does not include or download them)",
            packages=optional_info,
        )
    )

    # ---------------------------------------------------------------- paths
    for check_id, directory in (("data_dir", ctx.data_dir), ("models_dir", models_dir),
                                ("cache_dir", cache_dir)):
        writable = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".nlvs_doctor_{sys.pid if hasattr(sys, 'pid') else 0}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError:
            writable = False
        checks.append(
            _check(
                f"paths.{check_id}",
                "ok" if writable else "fail",
                f"{directory}{'' if writable else ' (not writable)'}",
                path=str(directory),
                writable=writable,
            )
        )
    try:
        free = shutil.disk_usage(str(ctx.data_dir)).free
        checks.append(
            _check("disk.free", "ok" if free > 512 * 1024 * 1024 else "warn",
                   f"{human_bytes(free)} free on the data volume", free_bytes=int(free))
        )
    except OSError as exc:
        checks.append(_check("disk.free", "warn", f"Disk usage unavailable: {exc}"))

    # ---------------------------------------------------------------- model env
    model_env = env.model_env_report()
    model_env_ok = True
    for key in ("AUDIOSEAL_CACHE_DIR", "TORCH_HOME", "HF_HOME", "HF_HUB_CACHE", "XDG_CACHE_HOME"):
        value = (model_env.get("values") or {}).get(key)
        if value is None:
            model_env_ok = False
            break
        resolved = Path(value)
        if not (resolved == models_dir or models_dir in resolved.parents
                or resolved == cache_dir or cache_dir in resolved.parents):
            model_env_ok = False
            break
    checks.append(
        _check(
            "models.environment",
            "ok" if model_env_ok else "warn",
            "All model/cache environment variables point inside the application data directory"
            if model_env_ok
            else "Some model/cache environment variables are missing or point outside the data dir",
            torch_imported_at_apply=model_env.get("torch_imported_at_apply"),
            values=model_env.get("values"),
        )
    )

    # ---------------------------------------------------------------- models
    models = ctx.models.list(verify_hash=True)
    audioseal_ready, audioseal_reason = ctx.models.is_ready("audioseal")
    present_models = [item for item in models if item.get("present")]
    checks.append(
        _check(
            "models.inventory",
            "ok" if audioseal_ready else "warn",
            f"{len(present_models)}/{len(models)} model files present; "
            f"AudioSeal {'ready' if audioseal_ready else 'incomplete'}",
            models=models,
            models_dir=str(models_dir),
            total_bytes=sum(item.get("size_bytes") or 0 for item in present_models),
            audio_seal_ready=audioseal_ready,
            audio_seal_reason=audioseal_reason,
        )
    )
    safespeech = next((item for item in models if item.get("engine") == "safespeech"), None)
    if safespeech:
        checks.append(
            _check(
                "models.safespeech",
                "warn" if not safespeech.get("present") else "ok",
                safespeech.get("reason") or "SafeSpeech weights status",
                external_found=safespeech.get("external_found"),
                external_missing=safespeech.get("external_missing"),
            )
        )

    # ---------------------------------------------------------------- c2patool
    c2pa_engine = ctx.registry.get("c2pa")
    c2pa_available, c2pa_reason = c2pa_engine.availability()
    checks.append(
        _check(
            "tool.c2patool",
            "ok" if c2pa_available else "warn",
            f"c2patool {c2pa_engine.version} at {c2pa_engine.path()}" if c2pa_available
            else (c2pa_reason or "c2patool not found"),
            path=c2pa_engine.path(),
            version=c2pa_engine.version if c2pa_available else None,
        )
    )
    identity = c2pa_engine.identity_info() if c2pa_available else None
    checks.append(
        _check(
            "c2pa.identity",
            "ok" if identity else "warn",
            (
                f"Local signing identity present ({identity.get('alg')}); self-signed, not publicly trusted"
                if identity
                else "No local signing identity yet: generated on first C2PA signing (never in the repo)"
            ),
            identity=identity,
        )
    )

    # ---------------------------------------------------------------- ffmpeg
    from . import paths

    ffmpeg = paths.find_ffmpeg()
    checks.append(
        _check(
            "tool.ffmpeg",
            "ok" if ffmpeg else "warn",
            f"ffmpeg found at {ffmpeg}" if ffmpeg else
            "ffmpeg not found: only WAV/FLAC are supported without it (MP3 encode/decode unavailable)",
            path=str(ffmpeg) if ffmpeg else None,
        )
    )

    # ---------------------------------------------------------------- cache
    cache_info = ctx.cache.size()
    checks.append(
        _check(
            "cache.size",
            "ok",
            f"{human_bytes(cache_info['size_bytes'])} in {cache_info['file_count']} files",
            cache=cache_info,
        )
    )

    # ---------------------------------------------------------------- engines
    engines = ctx.registry.infos()
    unavailable = [item for item in engines if item["status"] == "unavailable"]
    checks.append(
        _check(
            "engines.status",
            "ok" if not unavailable else "warn",
            f"{len(engines) - len(unavailable)}/{len(engines)} engines available",
            engines=engines,
        )
    )

    # ---------------------------------------------------------------- summary
    fail = sum(1 for item in checks if item["status"] == "fail")
    warn = sum(1 for item in checks if item["status"] == "warn")
    summary = {
        "ok": len(checks) - fail - warn,
        "warn": warn,
        "fail": fail,
        "status": "fail" if fail else ("warn" if warn else "ok"),
    }
    return {
        "schema": "nlvs.doctor/1",
        "software": {"name": "NL VO Shield", "version": __version__},
        "checked_at": utc_now_iso(),
        "summary": summary,
        "checks": checks,
        "paths": {
            "data_dir": str(ctx.data_dir),
            "models_dir": str(models_dir),
            "cache_dir": str(cache_dir),
            "c2patool": c2pa_engine.path(),
            "ffmpeg": str(ffmpeg) if ffmpeg else None,
            "safespeech": str(ctx.safespeech_dir) if ctx.safespeech_dir else None,
        },
    }
