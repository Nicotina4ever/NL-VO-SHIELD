"""Small, dependency-free helpers: hashing, atomic writes, JSON, ids."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def full_id() -> str:
    """A full, collision-resistant identifier (never truncated for receipts)."""
    return str(uuid.uuid4())


def short_id(value: str | None = None, length: int = 8) -> str:
    raw = value or uuid.uuid4().hex
    return raw.replace("-", "")[:length]


def canonical_json(data: Any) -> bytes:
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def json_safe(value: Any) -> Any:
    """Convert numpy scalars/arrays and non-finite floats into JSON-safe data."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    # numpy scalars / arrays
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    if hasattr(value, "item"):
        return json_safe(value.item())
    return str(value)


def dumps(data: Any, *, indent: int | None = 2) -> str:
    return json.dumps(json_safe(data), ensure_ascii=False, indent=indent, allow_nan=False)


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def read_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_rmtree(path: str | Path, *, allowed_root: str | Path) -> bool:
    """Remove a directory only when it is strictly inside ``allowed_root``."""
    path = Path(path).resolve()
    root = Path(allowed_root).resolve()
    if path == root or root not in path.parents:
        return False
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def dir_size(path: str | Path) -> tuple[int, int]:
    """Return (total_bytes, file_count) without following symlinks."""
    total = 0
    count = 0
    root = Path(path)
    if not root.exists():
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                if full.is_symlink():
                    continue
                total += full.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{size} B"
