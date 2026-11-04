"""Model inventory and explicit, checksummed downloads.

Policy (committed requirement):
* all model weights live under ``data_dir/models`` — never in global caches;
* nothing is ever downloaded implicitly. ``models.download`` must be called
  explicitly by the user (CLI/RPC) and reports progress;
* every download is verified against a pinned SHA-256 before it is moved into
  place; a mismatch is a hard failure and the temp file is removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .errors import JobCancelled, ModelDownloadError, ModelError, ModelIntegrityError
from .util import atomic_write_text, dir_size, human_bytes, read_json, sha256_file, utc_now_iso

ProgressCallback = Callable[[int | None, str, int, int | None], None]

_CHUNK = 1 << 20


@dataclass(frozen=True)
class ModelSpec:
    id: str
    engine: str
    kind: str
    filename: str | None
    url: str | None
    sha256: str | None
    size_bytes: int | None
    download_allowed: bool
    source: str
    license_note: str
    required_for: tuple[str, ...] = ()
    external_relative_paths: tuple[str, ...] = ()
    reason: str | None = None
    details: str | None = None


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="audioseal-generator",
        engine="audioseal",
        kind="watermark_generator",
        filename="generator_base.pth",
        url="https://huggingface.co/facebook/audioseal/resolve/main/generator_base.pth",
        sha256="7a845b5fbe9364a63a3909d8ab3fe064d13a76ae4c2e983573e08c69b7b51748",
        size_bytes=58805980,
        download_allowed=True,
        source="facebook/audioseal (Hugging Face)",
        license_note="AudioSeal weights: CC-BY-NC 4.0 (research/non-commercial); see upstream model card.",
        required_for=("audioseal", "watermark"),
    ),
    ModelSpec(
        id="audioseal-detector",
        engine="audioseal",
        kind="watermark_detector",
        filename="detector_base.pth",
        url="https://huggingface.co/facebook/audioseal/resolve/main/detector_base.pth",
        sha256="8a78e8a83584113523e161fc599fcab10fd0e94c04d2eb9d2fa1e9ec91ab69d9",
        size_bytes=34667641,
        download_allowed=True,
        source="facebook/audioseal (Hugging Face)",
        license_note="AudioSeal weights: CC-BY-NC 4.0 (research/non-commercial); see upstream model card.",
        required_for=("audioseal", "watermark"),
    ),
    ModelSpec(
        id="safespeech-weights",
        engine="safespeech",
        kind="anti_clone_weights",
        filename=None,
        url=None,
        sha256=None,
        size_bytes=None,
        download_allowed=False,
        source="external (user-provided SafeSpeech checkout)",
        license_note="Research code/weights; not redistributed. Must be installed by the user.",
        required_for=("safespeech",),
        external_relative_paths=(
            "checkpoints/base_models/DUR_0.pth",
            "checkpoints/base_models/D_0.pth",
        ),
        reason="SafeSpeech checkpoints are managed by the component manager (Components > SafeSpeech > Installa/Ripara).",
    ),
)

_SPEC_BY_ID = {spec.id: spec for spec in MODEL_SPECS}
_INDEX_NAME = ".models-index.json"
_PART_SUFFIX = ".part"


def model_spec(model_id: str) -> ModelSpec:
    spec = _SPEC_BY_ID.get(model_id)
    if spec is None:
        raise ModelError(
            f"Unknown model id: {model_id}",
            code="MODEL_UNKNOWN",
            detail={"known": sorted(_SPEC_BY_ID)},
        )
    return spec


def specs_for_engine(engine_id: str) -> list[ModelSpec]:
    return [spec for spec in MODEL_SPECS if spec.engine == engine_id]


class ModelStore:
    """Inventory + explicit downloader for the known model set."""

    def __init__(self, models_dir: str | Path, *, safespeech_dir: str | Path | None = None) -> None:
        self.models_dir = Path(models_dir)
        self.safespeech_dir = Path(safespeech_dir) if safespeech_dir else None
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index_path = self.models_dir / _INDEX_NAME

    # ------------------------------------------------------------------ index
    def _read_index(self) -> dict[str, Any]:
        data = read_json(self._index_path, default={}) or {}
        if not isinstance(data, dict):
            return {}
        return data.get("models", {}) if "models" in data else data

    def _write_index(self, index: dict[str, Any]) -> None:
        payload = {"schema": "nlvs.models-index/1", "models": index}
        atomic_write_text(self._index_path, json.dumps(payload, indent=2, sort_keys=True))

    def _record(self, spec: ModelSpec, path: Path, digest: str) -> None:
        with self._lock:
            index = self._read_index()
            stat = path.stat()
            index[spec.id] = {
                "sha256": digest,
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "verified_at": utc_now_iso(),
            }
            self._write_index(index)

    # ------------------------------------------------------------------ paths
    def path_for(self, spec: ModelSpec) -> Path | None:
        if spec.filename is None:
            return None
        return self.models_dir / spec.engine / spec.filename

    def _external_present(self, spec: ModelSpec) -> tuple[bool, list[str], list[str]]:
        if not spec.external_relative_paths:
            return False, [], []
        base = self.safespeech_dir
        if base is None:
            return False, [], list(spec.external_relative_paths)
        found, missing = [], []
        for relative in spec.external_relative_paths:
            candidate = base / relative
            (found if candidate.is_file() else missing).append(relative)
        return (bool(found) and not missing), found, missing

    def digest(self, spec: ModelSpec, *, force: bool = False) -> str | None:
        """Return the cached SHA-256 when file identity (size+mtime) matches."""
        path = self.path_for(spec)
        if path is None or not path.is_file():
            return None
        stat = path.stat()
        with self._lock:
            cached = self._read_index().get(spec.id)
        if (
            not force
            and cached
            and cached.get("size_bytes") == int(stat.st_size)
            and cached.get("mtime_ns") == int(stat.st_mtime_ns)
            and isinstance(cached.get("sha256"), str)
        ):
            return str(cached["sha256"])
        digest = sha256_file(path)
        self._record(spec, path, digest)
        return digest

    # ------------------------------------------------------------------ inventory
    def entry(self, spec: ModelSpec, *, verify_hash: bool = True) -> dict[str, Any]:
        path = self.path_for(spec)
        present = False
        size = 0
        digest: str | None = None
        integrity = "unknown"
        found: list[str] = []
        missing: list[str] = []
        if path is not None:
            present = path.is_file()
            if present:
                size = int(path.stat().st_size)
                if verify_hash and spec.sha256:
                    digest = self.digest(spec)
                    integrity = "ok" if digest == spec.sha256 else "mismatch"
                elif spec.sha256:
                    integrity = "unverified"
        else:
            present, found, missing = self._external_present(spec)
            if present and self.safespeech_dir is not None:
                base = self.safespeech_dir
                for relative in found:
                    size += (base / relative).stat().st_size
            integrity = "external" if present else "missing"
        expected = spec.size_bytes
        entry = {
            "id": spec.id,
            "kind": spec.kind,
            "path": str(path) if path is not None else (str(self.safespeech_dir) if self.safespeech_dir else None),
            "size_bytes": size,
            "present": present,
            "source": spec.source,
            "sha256": digest,
            "engine": spec.engine,
            "expected_sha256": spec.sha256,
            "expected_size_bytes": expected,
            "integrity": integrity,
            "download_allowed": spec.download_allowed,
            "requires_download": bool(spec.download_allowed and not present),
            "required_for": list(spec.required_for),
            "license_note": spec.license_note,
            "reason": spec.reason,
            "details": spec.details,
            "external_found": found,
            "external_missing": missing,
        }
        if spec.download_allowed and present and spec.sha256:
            entry["size_ok"] = size == spec.size_bytes
        return entry

    def list(self, *, verify_hash: bool = True) -> list[dict[str, Any]]:
        return [self.entry(spec, verify_hash=verify_hash) for spec in MODEL_SPECS]

    def total_bytes(self) -> int:
        return dir_size(self.models_dir)[0]

    def is_ready(self, engine_id: str) -> tuple[bool, str | None]:
        """True when every downloadable model for an engine is present and valid."""
        specs = [spec for spec in specs_for_engine(engine_id) if spec.download_allowed]
        if not specs:
            return True, None
        missing: list[str] = []
        bad: list[str] = []
        for spec in specs:
            path = self.path_for(spec)
            if path is None or not path.is_file():
                missing.append(spec.id)
                continue
            digest = self.digest(spec)
            if spec.sha256 and digest != spec.sha256:
                bad.append(spec.id)
        if missing:
            return False, (
                "Missing model weights: "
                + ", ".join(missing)
                + ". Run the explicit download command (models download) first."
            )
        if bad:
            return False, (
                "Model checksum mismatch: " + ", ".join(bad) + ". Re-download the affected weights."
            )
        return True, None

    # ------------------------------------------------------------------ download
    def download(
        self,
        model_id: str,
        *,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        spec = model_spec(model_id)
        if not spec.download_allowed or not spec.url or not spec.filename:
            raise ModelError(
                f"Model '{model_id}' cannot be downloaded automatically: {spec.reason or 'external model'}",
                code="MODEL_DOWNLOAD_NOT_ALLOWED",
                detail={"model_id": model_id, "source": spec.source, "reason": spec.reason},
            )
        target = self.path_for(spec)
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            current = self.digest(spec, force=True)
            if current == spec.sha256:
                self._emit(progress, 100, "already present and verified", target.stat().st_size, target.stat().st_size)
                return {
                    "model_id": model_id,
                    "status": "already_present",
                    "path": str(target),
                    "size_bytes": int(target.stat().st_size),
                    "sha256": current,
                }
            raise ModelIntegrityError(
                f"Local file for {model_id} exists but checksum mismatch; delete it explicitly first",
                detail={"path": str(target), "expected": spec.sha256, "actual": current},
            )
        expected_size = spec.size_bytes or 0
        free = shutil.disk_usage(str(self.models_dir.parent)).free
        if expected_size and free < expected_size * 1.25:
            raise ModelDownloadError(
                f"Not enough free disk space for {model_id}: need ~{human_bytes(int(expected_size * 1.25))}, "
                f"available {human_bytes(free)}",
                detail={"required": expected_size, "free": free},
            )
        temp = target.with_name(target.name + _PART_SUFFIX)
        if temp.exists():
            temp.unlink()
        digest = hashlib.sha256()
        downloaded = 0
        total: int | None = expected_size or None
        started = time.time()
        self._emit(progress, 0, f"downloading {spec.filename}", 0, total)
        request = urllib.request.Request(
            spec.url,
            headers={
                "User-Agent": f"NL-VO-Shield/{__version__} (offline local engine; explicit download)",
                "Accept": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit():
                    total = int(declared)
                last_emit = 0.0
                with temp.open("wb") as stream:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise JobCancelled("Model download cancelled")
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        stream.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if progress is not None and (now - last_emit >= 0.25):
                            last_emit = now
                            pct = int(downloaded * 100 / total) if total else None
                            self._emit(
                                progress,
                                pct,
                                f"downloading {spec.filename} ({human_bytes(downloaded)}"
                                + (f" / {human_bytes(total)})" if total else ")"),
                                downloaded,
                                total,
                            )
                    stream.flush()
                    os.fsync(stream.fileno())
        except JobCancelled:
            self._safe_unlink(temp)
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            self._safe_unlink(temp)
            raise ModelDownloadError(
                f"Download of {model_id} failed: {exc}",
                detail={"model_id": model_id, "url": spec.url},
            ) from exc
        actual_size = temp.stat().st_size
        if spec.size_bytes and actual_size != spec.size_bytes:
            self._safe_unlink(temp)
            raise ModelIntegrityError(
                f"Downloaded size mismatch for {model_id}: expected {spec.size_bytes}, got {actual_size}",
                detail={"model_id": model_id},
            )
        actual_digest = digest.hexdigest()
        if spec.sha256 and actual_digest != spec.sha256:
            self._safe_unlink(temp)
            raise ModelIntegrityError(
                f"Checksum mismatch for {model_id}: expected {spec.sha256}, got {actual_digest}",
                detail={"model_id": model_id, "path": str(target)},
            )
        os.replace(temp, target)
        self._record(spec, target, actual_digest)
        self._emit(progress, 100, "download verified", actual_size, actual_size)
        return {
            "model_id": model_id,
            "status": "downloaded",
            "path": str(target),
            "size_bytes": int(actual_size),
            "sha256": actual_digest,
            "elapsed_seconds": round(time.time() - started, 2),
        }

    # ------------------------------------------------------------------ delete/prune
    def delete(self, model_id: str) -> dict[str, Any]:
        spec = model_spec(model_id)
        if not spec.download_allowed:
            raise ModelError(
                f"Model '{model_id}' is external and cannot be deleted by NL Vo Shield",
                code="MODEL_DELETE_REFUSED",
                detail={"model_id": model_id, "reason": spec.reason},
            )
        path = self.path_for(spec)
        assert path is not None
        self._assert_inside_models(path)
        removed = False
        if path.is_file():
            path.unlink()
            removed = True
        for leftover in (path.with_name(path.name + _PART_SUFFIX),):
            self._safe_unlink(leftover)
        with self._lock:
            index = self._read_index()
            index.pop(spec.id, None)
            self._write_index(index)
        return {"model_id": model_id, "deleted": removed, "path": str(path)}

    def prune(self) -> dict[str, Any]:
        removed_files = 0
        removed_bytes = 0
        with self._lock:
            for current, _dirs, files in os.walk(self.models_dir):
                for name in files:
                    if name.endswith(_PART_SUFFIX) or name.endswith(".tmp"):
                        candidate = Path(current) / name
                        try:
                            removed_bytes += candidate.stat().st_size
                            candidate.unlink()
                            removed_files += 1
                        except OSError:
                            pass
            index = self._read_index()
            stale = [
                model_id
                for model_id in index
                if model_id in _SPEC_BY_ID
                and (self.path_for(_SPEC_BY_ID[model_id]) is None
                     or not self.path_for(_SPEC_BY_ID[model_id]).is_file())  # type: ignore[union-attr]
            ]
            for model_id in stale:
                index.pop(model_id, None)
            if stale:
                self._write_index(index)
        removed_dirs = 0
        for current, dirs, _files in os.walk(self.models_dir, topdown=False):
            for name in dirs:
                candidate = Path(current) / name
                try:
                    candidate.rmdir()
                    removed_dirs += 1
                except OSError:
                    pass
        return {
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "removed_dirs": removed_dirs,
            "stale_index_entries": len(stale),
            "models_dir": str(self.models_dir),
        }

    # ------------------------------------------------------------------ helpers
    def _assert_inside_models(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            root = self.models_dir.resolve()
        except OSError as exc:
            raise ModelError(f"Cannot resolve model path: {path}") from exc
        if resolved != root and root not in resolved.parents:
            raise ModelError(
                f"Refusing to touch a path outside the models directory: {path}",
                code="MODEL_PATH_OUTSIDE_ROOT",
            )

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _emit(callback: ProgressCallback | None, pct: int | None, message: str,
              downloaded: int, total: int | None) -> None:
        if callback is not None:
            try:
                callback(pct, message, downloaded, total)
            except Exception:
                pass
