"""Application context: settings, data dirs, model/cache stores and engines.

One ``AppContext`` is shared by the CLI and the RPC server. Constructing it
configures the model/cache environment *before* any torch/audioseal import.
"""

from __future__ import annotations

import platform
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import __version__, paths
from .cache import CacheManager, validate_cache_location
from .env import apply_model_env
from .models import ModelStore
from .receipts import ReceiptStore
from .settings import SettingsStore
from .store import PresetStore, ProfileStore
from .engines import build_registry
from .engines.base import EngineContext


class AppContext:
    def __init__(self, data_dir: str | Path | None = None, *, portable: bool | None = None) -> None:
        self.data_dir = paths.resolve_data_dir(data_dir, portable=portable)
        self.settings_store = SettingsStore(self.data_dir)
        self.settings = self.settings_store.load()
        # Cache ownership must be established before any cache subdirectory is
        # created, otherwise an existing user-selected directory could never be
        # proven foreign and clear() would be unsafe.
        validate_cache_location(Path(self.settings["cache_dir_resolved"]), self.data_dir)
        self.cache = CacheManager(self.settings["cache_dir_resolved"])
        self.cache.ensure()
        self._apply_environment(self.settings)
        self.safespeech_dir = paths.find_safespeech_dir()
        self.models = ModelStore(self.settings["models_dir"], safespeech_dir=self.safespeech_dir)
        self.receipts = ReceiptStore(self.data_dir)
        self.profiles = ProfileStore(self.data_dir)
        self.presets = PresetStore(self.data_dir)
        self.c2patool = paths.find_c2patool(self.data_dir, self.settings.get("c2patool_path"))
        self.registry = build_registry(self)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    def _apply_environment(self, settings: dict[str, Any]) -> None:
        apply_model_env(
            Path(settings["models_dir_resolved"]),
            Path(settings["cache_dir_resolved"]),
            allow_download=False,
        )

    def engine_context(
        self,
        *,
        progress: Callable[[str, int | None, str], None] | None = None,
        cancel: threading.Event | None = None,
        work_dir: Path | None = None,
        options: dict[str, Any] | None = None,
        payload: int | None = None,
    ) -> EngineContext:
        settings = self.settings_public()
        return EngineContext(
            data_dir=self.data_dir,
            models_dir=Path(settings["models_dir"]),
            cache_dir=Path(settings["cache_dir"]),
            settings=settings,
            models=self.models,
            c2patool=self.c2patool,
            progress=progress,
            cancel=cancel,
            work_dir=work_dir,
            options=options or {},
            payload=payload,
            engine_registry=self.registry,
        )

    # ------------------------------------------------------------------ settings
    def settings_public(self) -> dict[str, Any]:
        return self.settings_store.public()

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = self.settings_store.save(patch)
            old_models = self.settings.get("models_dir_resolved")
            old_cache = self.settings.get("cache_dir_resolved")
            self.settings = updated
            if updated["models_dir_resolved"] != old_models or updated["cache_dir_resolved"] != old_cache:
                validate_cache_location(
                    Path(updated["cache_dir_resolved"]), self.data_dir
                )
                self.cache = CacheManager(updated["cache_dir_resolved"])
                self.cache.ensure()
                self._apply_environment(updated)
                self.models = ModelStore(
                    updated["models_dir_resolved"], safespeech_dir=self.safespeech_dir
                )
            if "c2patool_path" in patch:
                self.c2patool = paths.find_c2patool(self.data_dir, updated.get("c2patool_path"))
                engine = self.registry._engines.get("c2pa")  # type: ignore[attr-defined]
                if engine is not None:
                    self.registry.register(type(engine)(self))
            return self.settings_public()

    # ------------------------------------------------------------------ info
    def health(self) -> dict[str, Any]:
        engines = [
            {"id": info["id"], "version": info["version"], "status": info["status"]}
            for info in self.registry.infos()
        ]
        return {
            "version": __version__,
            "os": platform.system().lower(),
            "arch": platform.machine(),
            "python": sys.version.split()[0],
            "python_implementation": platform.python_implementation(),
            "engines": engines,
            "data_dir": str(self.data_dir),
            "models_dir": self.settings["models_dir_resolved"],
            "cache_dir": self.settings["cache_dir_resolved"],
            "protocol_version": __import__("nl_vo_shield").PROTOCOL_VERSION,
        }

    def engine_infos(self) -> list[dict[str, Any]]:
        return self.registry.infos()

    def install_hint(self, engine_id: str) -> dict[str, Any]:
        return self.registry.install_hint(engine_id)

    def resolved_paths(self) -> dict[str, str]:
        return {
            "data_dir": str(self.data_dir),
            "models_dir": self.settings["models_dir_resolved"],
            "cache_dir": self.settings["cache_dir_resolved"],
        }
