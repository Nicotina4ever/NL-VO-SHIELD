"""Path discovery and external tool resolution.

Rules (see docs/ARCHITECTURE.md):
* no absolute development paths are hardcoded anywhere in the package;
* the repository root is found by walking up from this file (or from
  ``NLVS_PROJECT_ROOT``);
* the development workspace root (the legacy folder that also contains the
  project Python and the ``bin/`` tools) is discovered the same way, but is
  never required at runtime;
* the data directory is chosen by the host (``--data-dir`` /
  ``NLVS_DATA_DIR``), defaulting to ``<repo>/.local/data`` in development.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import PathResolutionError

_ENV_PROJECT_ROOT = "NLVS_PROJECT_ROOT"
_ENV_WORKSPACE_ROOT = "NLVS_WORKSPACE_ROOT"
_ENV_DATA_DIR = "NLVS_DATA_DIR"
_ENV_C2PATOOL = "NLVS_C2PATOOL"
_ENV_FFMPEG = "NLVS_FFMPEG"
_ENV_SAFESPEECH = "NLVS_SAFESPEECH_DIR"

_PACKAGE_DIR = Path(__file__).resolve().parent
_ENGINE_DIR = _PACKAGE_DIR.parent              # packages/engine
_CANDIDATE_ROOTS = (_ENGINE_DIR, *_ENGINE_DIR.parents[:6])


def _is_repo_root(candidate: Path) -> bool:
    return (
        (candidate / "packages" / "engine" / "nl_vo_shield").is_dir()
        and (candidate / "docs").is_dir()
    )


def repo_root() -> Path:
    """Return the public repository root (containing ``packages/engine``)."""
    override = os.environ.get(_ENV_PROJECT_ROOT)
    if override:
        candidate = Path(override).expanduser().resolve()
        if (candidate / "packages" / "engine").is_dir():
            return candidate
        raise PathResolutionError(
            f"{_ENV_PROJECT_ROOT} does not point to a valid repository root: {candidate}",
            code="PROJECT_ROOT_INVALID",
        )
    for candidate in _CANDIDATE_ROOTS:
        if _is_repo_root(candidate):
            return candidate
    # Installed/frozen layouts: the package sits next to its resources.
    for candidate in (_PACKAGE_DIR, *_PACKAGE_DIR.parents[:4]):
        if (candidate / "nl_vo_shield").is_dir():
            return candidate
    raise PathResolutionError(
        "Unable to locate the NL VO Shield repository root from "
        f"{_PACKAGE_DIR}; set {_ENV_PROJECT_ROOT}.",
        detail={"searched": [str(p) for p in _CANDIDATE_ROOTS]},
    )


def workspace_root() -> Path:
    """Return the development workspace root (contains python_win/, bin/, tools/).

    In a packaged installation this is simply the repository root's parent; the
    engine never depends on it because tools are resolved through ``bin/``
    candidates below it or through environment variables set by the host.
    """
    override = os.environ.get(_ENV_WORKSPACE_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    repo = repo_root()
    candidates = [repo.parent, *repo.parents[:4]]
    for candidate in candidates:
        if (candidate / "python_win").is_dir() or (candidate / "bin").is_dir():
            return candidate
    return repo.parent


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".nlvs_write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def executable_search_names(name: str) -> list[str]:
    if sys.platform == "win32":
        return [f"{name}.exe", name]
    return [name]


def find_executable(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        for name in executable_search_names(candidate.name) if candidate.suffix == "" else [candidate.name]:
            resolved = candidate.with_name(name)
            if resolved.is_file():
                return resolved
    return None


def find_c2patool(data_dir: Path | None = None, configured: str | None = None) -> Path | None:
    """Locate the ``c2patool`` executable without using the global PATH.

    Search order: explicit setting, environment variable, repository
    ``bin/c2patool`` (bundled), workspace ``bin/c2patool/c2patool`` (dev),
    data-dir ``bin/c2patool``.
    """
    candidates: list[Path] = []
    if configured:
        p = Path(configured).expanduser()
        candidates += [p, p / "c2patool"]
    env_value = os.environ.get(_ENV_C2PATOOL)
    if env_value:
        p = Path(env_value).expanduser()
        candidates += [p, p / "c2patool"]
    try:
        repo = repo_root()
    except PathResolutionError:
        repo = None
    if repo is not None:
        base = repo / "bin" / "c2patool"
        candidates += [
            base / "c2patool",
            base / "c2patool.exe",
        ]
    try:
        workspace = workspace_root()
    except PathResolutionError:
        workspace = None
    if workspace is not None:
        base = workspace / "bin" / "c2patool"
        candidates += [
            base / "c2patool" / "c2patool",
            base / "c2patool",
        ]
    if data_dir is not None:
        base = data_dir / "bin" / "c2patool"
        candidates += [base / "c2patool", base / "c2patool" / "c2patool"]
    return find_executable(candidates)


def find_ffmpeg() -> Path | None:
    env_value = os.environ.get(_ENV_FFMPEG)
    if env_value:
        p = Path(env_value).expanduser()
        if p.is_file():
            return p
    candidates: list[Path] = []
    try:
        workspace = workspace_root()
        candidates += [workspace / "bin" / "ffmpeg", workspace / "bin" / "ffmpeg" / "ffmpeg"]
    except PathResolutionError:
        pass
    found = find_executable(candidates)
    if found:
        return found
    # ffmpeg is an optional system component for compressed formats only.
    from shutil import which

    resolved = which("ffmpeg")
    return Path(resolved) if resolved else None


def find_safespeech_dir() -> Path | None:
    env_value = os.environ.get(_ENV_SAFESPEECH)
    if env_value:
        p = Path(env_value).expanduser()
        if p.is_dir():
            return p
    try:
        workspace = workspace_root()
    except PathResolutionError:
        return None
    for candidate in (
        workspace / "tools" / "SafeSpeech",
        workspace / "SafeSpeech",
    ):
        if candidate.is_dir():
            return candidate
    return None


def default_data_dir(portable: bool | None = None) -> Path:
    """Resolve the default data directory when the host did not pass one.

    * ``NLVS_DATA_DIR`` wins (development and tests use it).
    * portable mode: a sibling folder next to the executable, if writable.
    * development: ``<repo>/.local/data``.
    * packaged non-portable: per-user application data directory.
    """
    env_value = os.environ.get(_ENV_DATA_DIR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    if portable:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = repo_root()
        candidate = base / "NL VO Shield Data"
        if _writable_dir(candidate):
            return candidate
    try:
        repo = repo_root()
        dev = repo / ".local" / "data"
        # In development the repo-local data dir is the intended default.
        if (repo / "packages").is_dir():
            dev.mkdir(parents=True, exist_ok=True)
            return dev
    except (PathResolutionError, OSError):
        pass
    app_name = "NL VO Shield"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / app_name


def resolve_data_dir(data_dir: str | Path | None = None, portable: bool | None = None) -> Path:
    if data_dir is not None:
        resolved = Path(data_dir).expanduser().resolve()
    else:
        resolved = default_data_dir(portable=portable)
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathResolutionError(
            f"Cannot create the data directory: {resolved}: {exc}",
            code="DATA_DIR_UNWRITABLE",
            detail={"data_dir": str(resolved)},
        ) from exc
    return resolved


def is_within(child: Path, parent: Path) -> bool:
    try:
        child = Path(child).resolve()
        parent = Path(parent).resolve()
    except OSError:
        return False
    return child == parent or parent in child.parents
