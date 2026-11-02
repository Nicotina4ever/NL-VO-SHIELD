"""User settings stored in the data directory (atomic JSON)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .cache import validate_cache_location
from .errors import SettingsError
from .util import atomic_write_text, read_json

SETTINGS_FILENAME = "settings.json"

DEFAULTS: dict[str, Any] = {
    "language": "it",
    "theme": "dark",
    "portable": False,
    "models_dir": None,      # default: <data_dir>/models
    "cache_dir": None,       # default: <data_dir>/cache
    "output_dir": None,      # default: None -> chosen per job
    "default_preset": "studio_master",
    "c2patool_path": None,
    "watermark_threshold": 0.5,
    "receipt_trust_mode": "local",
    "keep_partial_outputs": True,
}

_ALLOWED = set(DEFAULTS)

_SCHEMA = "nlvs.settings/1"


class SettingsStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / SETTINGS_FILENAME
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ load
    def load(self) -> dict[str, Any]:
        stored = read_json(self.path, default={}) or {}
        if not isinstance(stored, dict):
            stored = {}
        unknown = sorted(set(stored) - _ALLOWED - {"schema"})
        values = dict(DEFAULTS)
        values.update({key: stored[key] for key in stored if key in _ALLOWED})
        values = self._resolve(values)
        values["_unknown_keys"] = unknown  # informational, never persisted
        return values

    def _resolve(self, values: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.data_dir
        models_dir = values.get("models_dir")
        models_path = (
            Path(models_dir).expanduser() if models_dir else data_dir / "models"
        )
        if not models_path.is_absolute():
            models_path = data_dir / models_path
        cache_dir = values.get("cache_dir")
        cache_path = Path(cache_dir).expanduser() if cache_dir else data_dir / "cache"
        if not cache_path.is_absolute():
            cache_path = data_dir / cache_path
        output_dir = values.get("output_dir")
        output_path = None
        if output_dir:
            candidate = Path(output_dir).expanduser()
            output_path = candidate if candidate.is_absolute() else data_dir / candidate
        return {
            **values,
            "data_dir": str(data_dir),
            "models_dir": str(models_path),
            "cache_dir": str(cache_path),
            "output_dir": str(output_path) if output_path else None,
            "models_dir_resolved": str(models_path),
            "cache_dir_resolved": str(cache_path),
        }

    # ------------------------------------------------------------------ save
    def validate_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise SettingsError("settings must be an object")
        unknown = sorted(set(patch) - _ALLOWED)
        if unknown:
            raise SettingsError(
                f"Unknown setting keys: {', '.join(unknown)}",
                code="SETTINGS_UNKNOWN_KEY",
                detail={"unknown": unknown, "allowed": sorted(_ALLOWED)},
            )
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key in ("models_dir", "cache_dir", "output_dir", "c2patool_path"):
                if value in (None, ""):
                    clean[key] = None
                    continue
                if not isinstance(value, str):
                    raise SettingsError(f"Setting '{key}' must be a string or null")
                if "\x00" in value:
                    raise SettingsError(f"Setting '{key}' contains a NUL byte")
                clean[key] = value
            elif key == "portable":
                if not isinstance(value, bool):
                    raise SettingsError("Setting 'portable' must be a boolean")
                clean[key] = value
            elif key == "language":
                if value not in ("it", "en"):
                    raise SettingsError("Setting 'language' must be 'it' or 'en'")
                clean[key] = value
            elif key == "theme":
                if value not in ("dark", "light", "system"):
                    raise SettingsError("Setting 'theme' must be dark, light or system")
                clean[key] = value
            elif key == "default_preset":
                if not isinstance(value, str) or not value:
                    raise SettingsError("Setting 'default_preset' must be a non-empty string")
                clean[key] = value
            elif key == "watermark_threshold":
                if not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
                    raise SettingsError("Setting 'watermark_threshold' must be a number in [0, 1]")
                clean[key] = float(value)
            elif key == "receipt_trust_mode":
                if value not in ("local", "any"):
                    raise SettingsError("Setting 'receipt_trust_mode' must be 'local' or 'any'")
                clean[key] = value
            elif key == "keep_partial_outputs":
                if not isinstance(value, bool):
                    raise SettingsError("Setting 'keep_partial_outputs' must be a boolean")
                clean[key] = value
        # Cross-field validation on the merged view.
        merged = self.load()
        merged.update(clean)
        merged = self._resolve({key: merged[key] for key in _ALLOWED})
        model_path = Path(merged["models_dir_resolved"])
        cache_path = Path(merged["cache_dir_resolved"])
        if model_path == cache_path:
            raise SettingsError("models_dir and cache_dir must be different directories")
        if str(model_path).startswith(str(cache_path) + "\\") or str(model_path).startswith(str(cache_path) + "/"):
            raise SettingsError("models_dir cannot be inside cache_dir")
        if "cache_dir" in clean:
            validate_cache_location(cache_path, self.data_dir)
        return clean

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        clean = self.validate_patch(patch)
        with self._lock:
            stored = read_json(self.path, default={}) or {}
            if not isinstance(stored, dict):
                stored = {}
            stored.pop("schema", None)
            stored.pop("_unknown_keys", None)
            stored.update(clean)
            payload = {"schema": _SCHEMA, **stored}
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        return self.load()

    def public(self) -> dict[str, Any]:
        """Settings exposed to the UI (paths resolved, internal keys removed)."""
        values = self.load()
        values.pop("_unknown_keys", None)
        return values
