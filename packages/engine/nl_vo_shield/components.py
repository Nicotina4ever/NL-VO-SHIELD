"""Universal component catalogue and lifecycle manager.

One catalogue drives the GUI, the installer and the engine registry.  Built-in
engines can be disabled; external engines are installed from a local copy (a
directory junction/symlink is used so nothing is duplicated) and can be removed
without touching the user's source/checkpoint directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .errors import NlvsError
from .util import atomic_write_text, dir_size, utc_now_iso


class ComponentError(NlvsError):
    code = "COMPONENT_ERROR"


_BUILTINS = [
    ("spectral_cloak", "Spectral Cloak", "protection", ["TTS", "VC"], "DSP sperimentale"),
    ("watermark_dsp", "DSP Watermark", "protection", ["provenienza"], "watermark locale"),
    ("audioseal", "AudioSeal", "protection", ["TTS", "VC"], "watermark neurale"),
    ("c2pa", "C2PA", "provenance", ["provenienza"], "firma/credential"),
    ("fingerprint", "Fingerprint", "analysis", ["analisi"], "impronta audio"),
    ("quality", "Quality analyzer", "analysis", ["qualità"], "misure oggettive"),
]

_EXTERNAL = {
    "safespeech": {
        "label": "SafeSpeech (official)",
        "kind": "protection",
        "threats": ["TTS", "VC"],
        "required": ["protect.py", "checkpoints/base_models/G_0.pth"],
        "runtime": "runtime/safespeech/venv",
        "description": "Perturbazione ufficiale SafeSpeech (CPU/CUDA), single WAV e batch.",
    },
    "mimic_blocker": {
        "label": "Mimic Blocker",
        "kind": "protection",
        "threats": ["TTS", "VC"],
        "required": ["model/model.py", "model/checkpoints/generator_wavlm.pth"],
        "runtime": "runtime/ai/venv",
        "description": "Generatore preaddestrato Mimic Blocker, CPU/GPU, quality guard.",
    },
    "enkidu": {
        "label": "Enkidu (UFP)",
        "kind": "protection",
        "threats": ["TTS", "VC"],
        "required": ["core/enkidu.py"],
        "runtime": "runtime/ai/venv",
        "description": "Perturbazione frequenziale universale; profili UFP.",
    },
    "e2e_vguard": {
        "label": "E2E-VGuard",
        "kind": "protection",
        "threats": ["TTS", "VC"],
        "required": ["protect.py"],
        "runtime": "runtime/ai/venv",
        "description": "Protezione E2E-VGuard (componente esterno).",
    },
}

_LAB_MODELS = [
    ("qwen3-tts-0.6b", "Qwen3-TTS 0.6B", "lab", ["TTS"], "Qwen3-TTS-12Hz-0.6B-Base"),
    ("qwen3-tts-1.7b", "Qwen3-TTS 1.7B", "lab", ["TTS"], "Qwen3-TTS-12Hz-1.7B-Base"),
    ("qwen3-tts-tokenizer", "Qwen3-TTS Tokenizer", "lab", ["TTS"], "Qwen3-TTS-Tokenizer-12Hz"),
]

_WHISPER = [("whisper-tiny", "Whisper tiny", 75_000_000), ("whisper-base", "Whisper base", 145_000_000),
            ("whisper-small", "Whisper small", 484_000_000), ("whisper-medium", "Whisper medium", 1_530_000_000),
            ("whisper-large-v3", "Whisper large-v3", 3_100_000_000)]


class ComponentManager:
    def __init__(self, data_dir: Path, context: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.context = context
        self.components_dir = self.data_dir / "components"
        self.state_file = self.components_dir / "state.json"
        self.components_dir.mkdir(parents=True, exist_ok=True)

    def _state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now_iso()
        atomic_write_text(self.state_file, json.dumps(state, ensure_ascii=False, indent=2))

    def _enabled(self, cid: str) -> bool:
        return bool(self._state().get("enabled", {}).get(cid, True))

    def _size(self, path: Path | None) -> int:
        if path is None or not path.exists():
            return 0
        try:
            return int(dir_size(path)[0])
        except Exception:
            return 0

    def _external_path(self, cid: str) -> Path:
        return self.components_dir / cid

    def _external_status(self, cid: str, spec: dict[str, Any]) -> dict[str, Any]:
        path = self._external_path(cid)
        present = all((path / rel).is_file() for rel in spec["required"])
        runtime = self.data_dir / spec["runtime"]
        python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
        return {
            "path": str(path),
            "present": present,
            "runtime_present": python.is_file(),
            "size_bytes": self._size(path),
        }

    def list(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        state = self._state()
        enabled_map = state.get("enabled", {})
        for cid, label, kind, threats, note in _BUILTINS:
            items.append({
                "id": cid, "label": label, "kind": kind, "threats": threats,
                "builtin": True, "removable": False, "installed": True,
                "enabled": bool(enabled_map.get(cid, True)), "path": None,
                "size_bytes": 0, "requirements": "incluse nel nucleo",
                "description": note, "actions": ["verify", "disable" if enabled_map.get(cid, True) else "enable"],
            })
        for cid, spec in _EXTERNAL.items():
            st = self._external_status(cid, spec)
            installed = st["present"]
            items.append({
                "id": cid, "label": spec["label"], "kind": spec["kind"], "threats": spec["threats"],
                "builtin": False, "removable": True, "installed": installed,
                "enabled": bool(enabled_map.get(cid, True)), "path": st["path"],
                "size_bytes": st["size_bytes"],
                "requirements": ", ".join(spec["required"]) + " + runtime",
                "runtime_present": st["runtime_present"],
                "description": spec["description"],
                "actions": (["verify", "disable", "uninstall"] if installed else ["install"]) +
                           (["enable"] if installed and not enabled_map.get(cid, True) else []),
            })
        for cid, label, kind, threats, dirname in _LAB_MODELS:
            path = self.data_dir / "models" / "qwen3-tts" / dirname
            installed = (path / "config.json").is_file()
            items.append({
                "id": cid, "label": label, "kind": kind, "threats": threats,
                "builtin": False, "removable": True, "installed": installed,
                "enabled": bool(enabled_map.get(cid, True)), "path": str(path),
                "size_bytes": self._size(path), "requirements": "runtime AI + modello",
                "description": "Modello di clonazione del Laboratorio di prova.",
                "actions": ["uninstall"] if installed else ["install_via_lab"],
            })
        for cid, label, approx in _WHISPER:
            path = self.data_dir / "models" / "whisper" / label.split()[-1].replace("large-v3", "large-v3")
            installed = (path / "model.bin").is_file()
            items.append({
                "id": cid, "label": label, "kind": "speech", "threats": ["trascrizione"],
                "builtin": False, "removable": True, "installed": installed,
                "enabled": bool(enabled_map.get(cid, True)), "path": str(path),
                "size_bytes": self._size(path), "requirements": "faster-whisper",
                "description": "Trascrizione del riferimento per il Laboratorio.",
                "actions": ["uninstall"] if installed else ["install_via_lab"],
            })
        return {"schema": "nlvs.components/1", "items": items,
                "total_bytes": sum(int(i.get("size_bytes") or 0) for i in items)}

    def action(self, cid: str, action: str, *, local_path: str | None = None,
               confirmed: bool = False) -> dict[str, Any]:
        cid = str(cid)
        action = str(action).lower()
        if action in ("uninstall", "install") and not confirmed:
            raise ComponentError("Conferma esplicita richiesta per install/uninstall",
                                 detail={"requires_confirmation": True})
        state = self._state()
        enabled = state.setdefault("enabled", {})
        if cid in _EXTERNAL:
            spec = _EXTERNAL[cid]
            target = self._external_path(cid)
            if action == "install":
                if not local_path:
                    raise ComponentError("local_path richiesto per il componente esterno")
                source = Path(local_path).expanduser().resolve()
                missing = [rel for rel in spec["required"] if not (source / rel).is_file()]
                if missing:
                    raise ComponentError(f"Percorso non valido; mancano {', '.join(missing)}")
                if target.exists():
                    raise ComponentError("Componente già presente; rimuoverlo prima")
                self._link(source, target)
                enabled[cid] = True
                self._write(state)
                return {"id": cid, "action": "install", "path": str(target), "linked": True}
            if not target.exists():
                raise ComponentError("Componente non installato")
            if action == "verify":
                st = self._external_status(cid, spec)
                return {"id": cid, "action": "verify", "ok": st["present"], **st}
            if action == "uninstall":
                removed = self._unlink(target)
                enabled.pop(cid, None)
                self._write(state)
                return {"id": cid, "action": "uninstall", "removed": removed, "path": str(target)}
            if action in ("enable", "disable"):
                enabled[cid] = action == "enable"
                self._write(state)
                return {"id": cid, "action": action, "enabled": enabled[cid]}
            raise ComponentError(f"Azione non supportata: {action}")

        if cid in {item[0] for item in _BUILTINS}:
            if action in ("enable", "disable"):
                enabled[cid] = action == "enable"
                self._write(state)
                return {"id": cid, "action": action, "enabled": enabled[cid]}
            if action == "verify":
                return {"id": cid, "action": "verify", "ok": True, "note": "componente del nucleo"}
            raise ComponentError("I componenti del nucleo possono essere solo abilitati/disabilitati qui")

        if cid.startswith("qwen3-tts") or cid.startswith("whisper-"):
            if action == "uninstall":
                item = next((i for i in self.list()["items"] if i["id"] == cid), None)
                path = Path(item["path"]) if item else None
                if path is None or not path.exists():
                    return {"id": cid, "action": "uninstall", "removed": False, "freed_bytes": 0}
                roots = [(self.data_dir / "models").resolve()]
                if not any(str(path.resolve()).lower().startswith(str(r).lower()) for r in roots):
                    raise ComponentError("Rifiuto di eliminare un percorso esterno ai modelli")
                freed = self._size(path)
                shutil.rmtree(path, ignore_errors=True)
                return {"id": cid, "action": "uninstall", "removed": not path.exists(), "freed_bytes": freed}
            raise ComponentError("Usa lab.install_model / lab.install_whisper per questa installazione")
        raise ComponentError(f"Componente sconosciuto: {cid}")

    @staticmethod
    def _link(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                                       capture_output=True, text=True)
            if completed.returncode != 0:
                raise ComponentError("Creazione del collegamento fallita: " + (completed.stderr or completed.stdout))
        else:
            target.symlink_to(source, target_is_directory=True)

    @staticmethod
    def _unlink(target: Path) -> bool:
        if not target.exists():
            return False
        if os.name == "nt" and not target.is_symlink():
            completed = subprocess.run(["cmd", "/c", "rmdir", str(target)], capture_output=True, text=True)
            if completed.returncode == 0:
                return True
        if target.is_symlink():
            target.unlink()
            return True
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()

    def is_enabled(self, cid: str) -> bool:
        return bool(self._state().get("enabled", {}).get(cid, True))
