"""Portable Micromamba component manager (no global PATH/shell changes)."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .errors import NlvsError
from .util import atomic_write_text, dir_size, utc_now_iso

VERSION = "2.10.0-0"
_BASE = f"https://github.com/mamba-org/micromamba-releases/releases/download/{VERSION}/"
_ASSETS = {
    ("Windows", "AMD64"): ("micromamba-win-64", "f8b4284bd4eb1134122eaf93b3ea6c7be567e596de5829e1a6f478e34e41974a"),
    ("Linux", "x86_64"): ("micromamba-linux-64", "701f39c06f8e6a670a0e186c8479b9e5c0502a2f0cda6cef65d83bdd2875e8bd"),
    ("Darwin", "arm64"): ("micromamba-osx-arm64", "c547639f5caf742236641c362746abb4a3505774753ecd08fe341643670c4c01"),
    ("Darwin", "x86_64"): ("micromamba-osx-64", "8421462a1ef1df9f7acd114f9e2a24c1332ce4f4e29f44465b4acb44e2755172"),
}


class MicromambaError(NlvsError):
    code = "MICROMAMBA_ERROR"


class MicromambaManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "micromamba"
        self.envs = self.root / "envs"
        self.binary = self.root / ("micromamba.exe" if os.name == "nt" else "micromamba")
        self.state_file = self.root / "state.json"

    def _asset(self) -> tuple[str, str]:
        key = (platform.system(), platform.machine())
        if key not in _ASSETS:
            raise MicromambaError(f"Piattaforma non supportata: {key[0]} {key[1]}")
        return _ASSETS[key]

    def status(self) -> dict[str, Any]:
        installed = self.binary.is_file()
        envs = []
        if self.envs.is_dir():
            for path in sorted(self.envs.iterdir()):
                if path.is_dir():
                    envs.append({"name": path.name, "path": str(path), "size_bytes": dir_size(path)[0]})
        return {
            "version": VERSION,
            "installed": installed,
            "binary": str(self.binary),
            "root": str(self.root),
            "envs": envs,
            "size_bytes": dir_size(self.root)[0] if self.root.exists() else 0,
            "platform": f"{platform.system()} {platform.machine()}",
            "asset": self._asset()[0],
        }

    def install(self, progress=None) -> dict[str, Any]:
        name, expected = self._asset()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.binary
        if target.is_file():
            return {"installed": True, "version": VERSION, "path": str(target), "reused": True}
        url = _BASE + name
        tmp = target.with_suffix(".download")
        if progress:
            progress("download", 10, f"download {name}")
        urllib.request.urlretrieve(url, tmp)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != expected:
            tmp.unlink(missing_ok=True)
            raise MicromambaError(f"Checksum Micromamba non valido: {digest}")
        tmp.replace(target)
        if os.name != "nt":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        completed = subprocess.run([str(target), "--version"], capture_output=True, text=True, timeout=60)
        if completed.returncode != 0:
            raise MicromambaError("Micromamba non eseguibile: " + (completed.stderr or completed.stdout)[-500:])
        atomic_write_text(self.state_file, f'{{"version": "{VERSION}", "installed_at": "{utc_now_iso()}"}}')
        return {"installed": True, "version": VERSION, "path": str(target), "reused": False,
                "output": (completed.stdout or "").strip()}

    def verify(self) -> dict[str, Any]:
        if not self.binary.is_file():
            return {"ok": False, "reason": "Micromamba non installato"}
        expected = self._asset()[1]
        digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        completed = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=60)
        return {"ok": digest == expected and completed.returncode == 0,
                "sha256": digest, "expected": expected,
                "version": (completed.stdout or "").strip()}

    def uninstall(self) -> dict[str, Any]:
        freed = dir_size(self.root)[0] if self.root.exists() else 0
        if self.root.exists():
            import shutil
            shutil.rmtree(self.root, ignore_errors=True)
        return {"removed": not self.root.exists(), "freed_bytes": freed}

    def create_env(self, name: str, python: str = "3.10") -> dict[str, Any]:
        if not name or any(c in name for c in "\\/.. "):
            raise MicromambaError("Nome ambiente non valido")
        status = self.status()
        if not status["installed"]:
            raise MicromambaError("Installa prima Micromamba")
        target = self.envs / name
        if target.exists():
            return {"created": False, "name": name, "path": str(target), "reused": True}
        self.envs.mkdir(parents=True, exist_ok=True)
        command = [str(self.binary), "create", "-y", "-p", str(target), f"python={python}", "pip"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3600,
                                   env={**os.environ, "MAMBA_ROOT_PREFIX": str(self.root)})
        if completed.returncode != 0:
            raise MicromambaError("Creazione ambiente fallita: " + (completed.stderr or completed.stdout)[-1200:])
        return {"created": True, "name": name, "path": str(target),
                "python": str(target / ("python.exe" if os.name == "nt" else "bin/python3")),
                "size_bytes": dir_size(target)[0]}

    def remove_env(self, name: str) -> dict[str, Any]:
        if not name or any(c in name for c in "\\/.. "):
            raise MicromambaError("Nome ambiente non valido")
        target = self.envs / name
        if not target.exists():
            return {"removed": False, "name": name, "freed_bytes": 0}
        freed = dir_size(target)[0]
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        return {"removed": not target.exists(), "name": name, "freed_bytes": freed}
