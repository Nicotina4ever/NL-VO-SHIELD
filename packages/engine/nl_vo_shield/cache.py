"""Safe management of the application's own cache directory.

``cache.clear`` removes only files inside the configured cache directory and
only when it carries the marker file written by NL VO Shield when the
directory was created. This prevents an accidental user-configured path (for
example a Documents folder) from ever being wiped.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

from . import paths
from .errors import CacheError
from .util import dir_size

MARKER_NAME = ".nlvs-cache-marker"


class CacheManager:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self._lock = threading.RLock()

    @property
    def marker(self) -> Path:
        return self.cache_dir / MARKER_NAME

    def ensure(self) -> None:
        """Create the cache dir and its ownership marker.

        A marker is only written when the directory is new or empty: pointing
        ``cache_dir`` at an existing directory that already contains unrelated
        files must never authorise ``cache.clear`` to delete them.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.marker.is_file():
            return
        try:
            entries = [entry for entry in self.cache_dir.iterdir() if entry.name != MARKER_NAME]
        except OSError:
            return
        if entries:
            # Foreign, non-empty directory: do not claim ownership.
            return
        self.marker.write_text(
            "NL VO Shield cache directory. Only NL VO Shield cache files belong here.\n",
            encoding="utf-8",
        )

    @property
    def owned(self) -> bool:
        return self.marker.is_file()

    def size(self) -> dict[str, Any]:
        total, count = dir_size(self.cache_dir)
        return {
            "cache_dir": str(self.cache_dir),
            "size_bytes": total,
            "file_count": count,
            "marker_present": self.marker.is_file(),
            "owned": self.marker.is_file(),
            "note": (
                "cache.clear only removes files from a directory owned by NL VO Shield "
                "(marker present)"
            ),
        }

    def clear(self) -> dict[str, Any]:
        if not self.cache_dir.exists():
            return {"cache_dir": str(self.cache_dir), "removed_bytes": 0,
                    "removed_files": 0, "removed_dirs": 0, "cleared": False,
                    "reason": "cache directory does not exist"}
        if not self.marker.is_file():
            raise CacheError(
                "Refusing to clear a directory that was not created by NL VO Shield "
                f"(marker '{MARKER_NAME}' missing): {self.cache_dir}",
                code="CACHE_MARKER_MISSING",
                detail={"cache_dir": str(self.cache_dir)},
            )
        with self._lock:
            before, _count = dir_size(self.cache_dir)
            removed_files = 0
            removed_dirs = 0
            for entry in list(self.cache_dir.iterdir()):
                if entry.name == MARKER_NAME:
                    continue
                try:
                    if entry.is_symlink():
                        # Never follow symlinks out of the cache directory.
                        entry.unlink()
                        removed_files += 1
                    elif entry.is_dir():
                        shutil.rmtree(entry)
                        removed_dirs += 1
                    else:
                        entry.unlink()
                        removed_files += 1
                except OSError:
                    continue
            self.ensure()
            return {
                "cache_dir": str(self.cache_dir),
                "removed_bytes": before,
                "removed_files": removed_files,
                "removed_dirs": removed_dirs,
                "cleared": True,
            }


def validate_cache_location(cache_dir: Path, data_dir: Path) -> None:
    """Sanity checks before the cache manager is allowed to operate."""
    cache_dir = Path(cache_dir)
    data_dir = Path(data_dir)
    if cache_dir == data_dir:
        raise CacheError(
            "cache_dir must be a dedicated directory, not the data directory itself",
            code="CACHE_DIR_INVALID",
        )
    if cache_dir.exists() and not cache_dir.is_dir():
        raise CacheError(f"cache_dir exists but is not a directory: {cache_dir}")
    # A cache dir outside the application data dir is allowed (user choice on a
    # different disk) but never a filesystem root or a home directory.
    try:
        resolved = cache_dir.resolve()
    except OSError:
        return
    protected = {Path(resolved.anchor), Path.home().resolve()}
    if any(resolved == p for p in protected):
        raise CacheError(
            f"Refusing to use a filesystem root or home directory as cache_dir: {resolved}",
            code="CACHE_DIR_UNSAFE",
        )
    if paths.is_within(data_dir, cache_dir):
        raise CacheError(
            "cache_dir cannot contain the data directory",
            code="CACHE_DIR_UNSAFE",
        )
