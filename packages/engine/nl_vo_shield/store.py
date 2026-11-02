"""Local profiles and presets (never contains private keys).

Presets are real chains executed by the pipeline, not labels. The built-in
presets map to three impact levels plus a custom chain:

* ``public_distribution`` ("Leggero" / light): watermark + C2PA, no perturbation.
* ``professional_demo``  ("Bilanciato" / balanced): light spectral perturbation
  + watermark + C2PA.
* ``studio_master``      ("Massimo" / strong): medium spectral perturbation
  + watermark + C2PA (with an explicit quality warning).
* ``custom``: chain supplied by the UI, validated by the pipeline.

A step whose engine is ``watermark_auto`` is resolved at job time to AudioSeal
when its weights are installed, otherwise to the DSP spread-spectrum watermark.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .errors import PresetError
from .util import atomic_write_text, full_id, read_json, utc_now_iso

PROFILES_FILENAME = "profiles.json"
PRESETS_FILENAME = "presets.json"

WATERMARK_AUTO = "watermark_auto"
VALID_LEVELS = ("light", "balanced", "strong", "custom")

_COMMON_C2PA = {"engine": "c2pa", "action": "sign", "required": True, "params": {}}
_WATERMARK_AUTO_STEP = {
    "engine": WATERMARK_AUTO,
    "action": "embed",
    "required": True,
    "params": {},
}

BUILTIN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "public_distribution",
        "name": "Public Distribution",
        "label": "Leggero",
        "level": "light",
        "description": (
            "Watermark (AudioSeal when installed, otherwise the DSP spread-spectrum watermark) "
            "plus C2PA provenance. No anti-clone perturbation: only the audio samples are "
            "watermarked."
        ),
        "impact": (
            "Minimo: watermark inaudibile e manifest di provenienza; nessuna perturbazione "
            "anti-clone. Qualita' audio praticamente invariata."
        ),
        "builtin": True,
        "required_engines": ["watermark", "c2pa"],
        "quality_warning": False,
        "chain": [dict(_WATERMARK_AUTO_STEP), dict(_COMMON_C2PA)],
    },
    {
        "id": "professional_demo",
        "name": "Professional Demo",
        "label": "Bilanciato",
        "level": "balanced",
        "description": (
            "Light STFT anti-clone perturbation (spectral_cloak strength=low) + watermark "
            "(AudioSeal/DSP) + C2PA provenance."
        ),
        "impact": (
            "Moderato: perturbazione spettrale leggera (SNR ~30 dB) piu' watermark e provenienza. "
            "Qualita' alta, distorsione misurata nel job."
        ),
        "builtin": True,
        "required_engines": ["anti_clone", "watermark", "c2pa"],
        "quality_warning": False,
        "chain": [
            {"engine": "spectral_cloak", "action": "perturb", "required": True,
             "params": {"strength": "low", "seed": 1234}},
            dict(_WATERMARK_AUTO_STEP),
            dict(_COMMON_C2PA),
        ],
    },
    {
        "id": "studio_master",
        "name": "Studio Master",
        "label": "Massimo",
        "level": "strong",
        "description": (
            "Medium STFT anti-clone perturbation (spectral_cloak strength=medium) + watermark "
            "(AudioSeal/DSP) + C2PA provenance. Anti-clone efficacy remains experimental."
        ),
        "impact": (
            "Elevato: perturbazione spettrale media (SNR ~24 dB), watermark e provenienza. "
            "Possibile degrado percepibile su voci delicate: ascolto A/B consigliato."
        ),
        "builtin": True,
        "required_engines": ["anti_clone", "watermark", "c2pa"],
        "quality_warning": True,
        "chain": [
            {"engine": "spectral_cloak", "action": "perturb", "required": True,
             "params": {"strength": "medium", "seed": 1234}},
            dict(_WATERMARK_AUTO_STEP),
            dict(_COMMON_C2PA),
        ],
    },
    {
        "id": "base_watermark",
        "name": "Base Watermark (DSP)",
        "label": "Solo watermark (DSP)",
        "level": "light",
        "alias_of": "public_distribution",
        "description": (
            "Fallback for base installs without torch/AudioSeal: DSP spread-spectrum watermark "
            "+ C2PA provenance. Not anti-cloning."
        ),
        "impact": "Minimo: watermark DSP reale e manifest C2PA; nessun effetto anti-clone.",
        "builtin": True,
        "required_engines": ["watermark", "c2pa"],
        "quality_warning": False,
        "chain": [
            {"engine": "watermark_dsp", "action": "embed", "required": True, "params": {}},
            dict(_COMMON_C2PA),
        ],
    },
    {
        "id": "custom",
        "name": "Custom",
        "label": "Personalizzato",
        "level": "custom",
        "description": "Catena scelta manualmente dall'utente; validata dal motore prima dell'esecuzione.",
        "impact": "Dipende dalla catena scelta.",
        "builtin": True,
        "required_engines": [],
        "quality_warning": False,
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
                if override.get("chain"):
                    item["chain"] = override["chain"]
                    item["description"] = override.get("description", item["description"])
                    item["overridden"] = True
            merged.append(item)
        for item in custom.values():
            normalized = dict(item)
            normalized.setdefault("level", "custom")
            normalized.setdefault("label", normalized.get("name", "Custom"))
            normalized.setdefault("impact", "Dipende dalla catena personalizzata.")
            normalized.setdefault("quality_warning", False)
            merged.append(normalized)
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
        level = preset.get("level", "custom")
        if level not in VALID_LEVELS:
            raise PresetError(f"preset.level must be one of {VALID_LEVELS}")
        record = {
            "id": preset_id,
            "name": str(preset.get("name") or preset_id),
            "label": str(preset.get("label") or preset.get("name") or preset_id),
            "level": level,
            "description": str(preset.get("description") or ""),
            "impact": str(preset.get("impact") or "Dipende dalla catena personalizzata."),
            "builtin": False,
            "quality_warning": bool(preset.get("quality_warning", False)),
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
