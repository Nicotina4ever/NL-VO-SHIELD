"""Local profiles and presets (never contains private keys)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .errors import PresetError
from .util import atomic_write_text, full_id, read_json, utc_now_iso

PROFILES_FILENAME = "profiles.json"
PRESETS_FILENAME = "presets.json"

BUILTIN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "studio_master",
        "name": "Studio Master",
        "description": (
            "Massima qualita' possibile con anti-clone + watermark AudioSeal + provenienza C2PA. "
            "Richiede i pesi AudioSeal e c2patool."
        ),
        "builtin": True,
        "required_engines": ["spectral_cloak", "audioseal", "c2pa"],
        "chain": [
            {"engine": "spectral_cloak", "action": "perturb", "required": True,
             "params": {"strength": "medium", "seed": 1234}},
            {"engine": "audioseal", "action": "embed", "required": True, "params": {}},
            {"engine": "c2pa", "action": "sign", "required": True, "params": {}},
        ],
    },
    {
        "id": "professional_demo",
        "name": "Professional Demo",
        "description": (
            "Bilanciamento tra qualita' e protezione per demo professionali: perturbazione media, "
            "watermark AudioSeal e firma C2PA."
        ),
        "builtin": True,
        "required_engines": ["spectral_cloak", "audioseal", "c2pa"],
        "chain": [
            {"engine": "spectral_cloak", "action": "perturb", "required": True,
             "params": {"strength": "medium", "seed": 1234}},
            {"engine": "audioseal", "action": "embed", "required": True, "params": {}},
            {"engine": "c2pa", "action": "sign", "required": True, "params": {}},
        ],
    },
    {
        "id": "public_distribution",
        "name": "Public Distribution",
        "description": (
            "Perturbazione piu' forte per pubblicazione: qualita' leggermente inferiore, "
            "watermark e firma C2PA obbligatori."
        ),
        "builtin": True,
        "required_engines": ["spectral_cloak", "audioseal", "c2pa"],
        "chain": [
            {"engine": "spectral_cloak", "action": "perturb", "required": True,
             "params": {"strength": "high", "seed": 1234}},
            {"engine": "audioseal", "action": "embed", "required": True, "params": {}},
            {"engine": "c2pa", "action": "sign", "required": True, "params": {}},
        ],
    },
    {
        "id": "base_watermark",
        "name": "Base Watermark (DSP)",
        "description": (
            "Fallback per installazioni base senza torch/AudioSeal: watermark DSP spread-spectrum "
            "reale + provenienza C2PA. Non e' anti-cloning."
        ),
        "builtin": True,
        "required_engines": ["watermark_dsp", "c2pa"],
        "chain": [
            {"engine": "watermark_dsp", "action": "embed", "required": True, "params": {}},
            {"engine": "c2pa", "action": "sign", "required": True, "params": {}},
        ],
    },
    {
        "id": "custom",
        "name": "Custom",
        "description": "Catena scelta manualmente dall'utente.",
        "builtin": True,
        "required_engines": [],
        "chain": [],
    },
)

_PROFILE_FIELDS = ("name", "author", "email", "usage_terms", "copyright", "organization")


class JsonStore:
    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, Any]]:
        data = read_json(self.path, default={}) or {}
        items = data.get(self.key, []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _write(self, items: list[dict[str, Any]]) -> None:
        payload = {"schema": f"nlvs.{self.key}/1", self.key: items}
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))


class PresetStore(JsonStore):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(Path(data_dir) / PRESETS_FILENAME, "presets")

    def list(self) -> list[dict[str, Any]]:
        custom = {item.get("id"): item for item in self._read() if item.get("id")}
        merged: list[dict[str, Any]] = []
        for builtin in BUILTIN_PRESETS:
            item = dict(builtin)
            override = custom.pop(builtin["id"], None)
            if override and not override.get("builtin"):
                # A user override may adjust params but cannot shadow builtin ids
                # with an empty chain.
                if override.get("chain"):
                    item["chain"] = override["chain"]
                    item["description"] = override.get("description", item["description"])
                    item["overridden"] = True
            merged.append(item)
        for item in custom.values():
            merged.append(item)
        return merged

    def get(self, preset_id: str) -> dict[str, Any] | None:
        for item in self.list():
            if item.get("id") == preset_id:
                return item
        return None

    def save(self, preset: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(preset, dict):
            raise PresetError("preset must be an object")
        preset_id = preset.get("id")
        if not preset_id or not isinstance(preset_id, str):
            raise PresetError("preset.id is required")
        if any(item["id"] == preset_id and item.get("builtin") for item in BUILTIN_PRESETS):
            raise PresetError(
                f"'{preset_id}' is a builtin preset id; choose a different id for custom presets",
                code="PRESET_BUILTIN_LOCKED",
            )
        chain = preset.get("chain")
        if not isinstance(chain, list) or not chain:
            raise PresetError("preset.chain must be a non-empty list of steps")
        for step in chain:
            if not isinstance(step, dict) or not step.get("engine"):
                raise PresetError("each preset step needs an 'engine'")
            if step.get("action") not in ("embed", "perturb", "sign", "detect", "verify"):
                raise PresetError(f"unsupported preset action: {step.get('action')!r}")
        record = {
            "id": preset_id,
            "name": str(preset.get("name") or preset_id),
            "description": str(preset.get("description") or ""),
            "builtin": False,
            "chain": chain,
            "created_at": preset.get("created_at") or utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        with self._lock:
            items = [item for item in self._read() if item.get("id") != preset_id]
            items.append(record)
            self._write(items)
        return record

    def delete(self, preset_id: str) -> bool:
        if any(item["id"] == preset_id and item.get("builtin") for item in BUILTIN_PRESETS):
            raise PresetError(
                f"Builtin preset '{preset_id}' cannot be deleted",
                code="PRESET_BUILTIN_LOCKED",
            )
        with self._lock:
            items = self._read()
            kept = [item for item in items if item.get("id") != preset_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
        return True


class ProfileStore(JsonStore):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(Path(data_dir) / PROFILES_FILENAME, "profiles")

    def list(self) -> list[dict[str, Any]]:
        return self._read()

    def get(self, profile_id: str) -> dict[str, Any] | None:
        for item in self._read():
            if item.get("id") == profile_id:
                return item
        return None

    def save(self, profile: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(profile, dict):
            raise PresetError("profile must be an object")
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            profile_id = full_id()
        if len(profile_id) > 120 or "/" in profile_id or "\\" in profile_id or "\x00" in profile_id:
            raise PresetError("profile.id contains invalid characters")
        record = {"id": profile_id, "updated_at": utc_now_iso()}
        for key in _PROFILE_FIELDS:
            value = profile.get(key)
            if value is None:
                record[key] = ""
            elif isinstance(value, str) and len(value) <= 4000:
                record[key] = value
            else:
                raise PresetError(f"profile.{key} must be a string of at most 4000 characters")
        with self._lock:
            items = self._read()
            existing = next((item for item in items if item.get("id") == profile_id), None)
            record["created_at"] = (existing or {}).get("created_at") or utc_now_iso()
            items = [item for item in items if item.get("id") != profile_id]
            items.append(record)
            self._write(items)
        return record

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            items = self._read()
            kept = [item for item in items if item.get("id") != profile_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
        return True


def chain_from_preset(preset: dict[str, Any]) -> list[dict[str, Any]]:
    chain = preset.get("chain")
    if not isinstance(chain, list):
        return []
    return [dict(step) for step in chain]
