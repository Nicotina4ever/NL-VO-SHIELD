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
from .store import WATERMARK_AUTO, PresetStore, ProfileStore, chain_from_preset
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
        if self.safespeech_dir is None:
            component = self.data_dir / "components" / "safespeech"
            if (component / "protect.py").is_file():
                self.safespeech_dir = component
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

    # ------------------------------------------------------------------ presets
    def watermark_engine_id(self) -> str:
        """AudioSeal when its weights are installed, otherwise the DSP watermark."""
        try:
            available, _reason = self.registry.get("audioseal").availability()
        except Exception:
            available = False
        return "audioseal" if available else "watermark_dsp"

    def resolve_auto_engines(self, chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace the ``watermark_auto`` placeholder with a concrete engine id."""
        resolved: list[dict[str, Any]] = []
        for step in chain or []:
            if not isinstance(step, dict):
                resolved.append(step)
                continue
            item = dict(step)
            item["params"] = dict(item.get("params") or {})
            if item.get("engine") == WATERMARK_AUTO:
                item["engine"] = self.watermark_engine_id()
            resolved.append(item)
        return resolved

    def preset_chain(self, preset_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        preset = self.presets.get(str(preset_id))
        if preset is None:
            return [], None
        return self.resolve_auto_engines(chain_from_preset(preset)), preset

    def resolved_presets(self) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for preset in self.presets.list():
            item = dict(preset)
            chain = self.resolve_auto_engines(chain_from_preset(preset))
            item["chain"] = chain
            item["watermark_engine"] = next(
                (step.get("engine") for step in chain if step.get("action") == "embed"), None
            )
            resolved.append(item)
        return resolved

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
        infos = self.registry.infos()
        try:
            from .components import ComponentManager
            manager = ComponentManager(self.data_dir, self)
            for info in infos:
                if not manager.is_enabled(str(info.get("id") or "")):
                    info["status"] = "unavailable"
                    info["reason"] = "component disabled in the universal component manager"
        except Exception:
            pass
        return infos

    def install_hint(self, engine_id: str) -> dict[str, Any]:
        return self.registry.install_hint(engine_id)

    def resolved_paths(self) -> dict[str, str]:
        return {
            "data_dir": str(self.data_dir),
            "models_dir": self.settings["models_dir_resolved"],
            "cache_dir": self.settings["cache_dir_resolved"],
        }
