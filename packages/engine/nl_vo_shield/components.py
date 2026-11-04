"""Universal component catalogue and lifecycle manager.

One catalogue drives the GUI, the installer and the engine registry. Built-in
engines can be disabled; external engines are imported by *copying* the selected
source into the application data directory. No junction, symlink or other
permanent reference to the import location is created: after the import the
source directory can be moved or deleted without breaking the component.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import NlvsError
from .util import atomic_write_text, dir_size, utc_now_iso


class ComponentError(NlvsError):
    code = "COMPONENT_ERROR"


_JOBS: dict[str, dict[str, Any]] = {}
_INSTALL_LOCK = threading.Lock()
_JOBS_LOCK = threading.Lock()


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
        "runtime": "micromamba/envs/e2e",
        "description": "Protezione E2E-VGuard (componente esterno).",
    },
}

_BOOTSTRAP_IDS = {"safespeech", "mimic_blocker", "enkidu", "e2e_vguard"}
_MANUAL = {"e2e_vguard": ["checkpoints/VITS/pretrained_ljs.pth"]}

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
        if not python.is_file():
            python = runtime / ("python.exe" if os.name == "nt" else "bin/python3")
        return {
            "path": str(path),
            "present": present,
            "runtime_present": python.is_file(),
            "runtime_path": str(runtime),
            "links": self._count_links(path) if path.exists() else 0,
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
                "description": spec["description"], "bootstrap": cid in _BOOTSTRAP_IDS,
                "manual_missing": [rel for rel in _MANUAL.get(cid, []) if installed and not (Path(st["path"]) / rel).is_file()],
                "actions": (["verify", "disable", "uninstall"] if installed else ["install"]) +
                           (["import_checkpoint"] if installed and any(not (Path(st["path"]) / rel).is_file() for rel in _MANUAL.get(cid, [])) else []) +
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
                    job_id = uuid.uuid4().hex
                    with _JOBS_LOCK:
                        _JOBS[job_id] = {"job_id": job_id, "component": cid, "status": "running",
                                         "stage": "queued", "percent": 0, "message": "In coda",
                                         "logs": [], "started_at": utc_now_iso()}
                    def _worker(job_id: str = job_id, cid: str = cid) -> None:
                        try:
                            from .bootstrap import install as bootstrap_install
                            log_path = self.data_dir / "logs" / f"component-{cid}.log"
                            log_path.parent.mkdir(parents=True, exist_ok=True)
                            def _log(line: str) -> None:
                                with open(log_path, "a", encoding="utf-8") as fh:
                                    fh.write(f"{utc_now_iso()} {line}\n")
                            _log(f"job {job_id} start")
                            with _INSTALL_LOCK:
                                def progress(stage, pct, msg):
                                    _log(f"{pct}% {stage}: {msg}")
                                    with _JOBS_LOCK:
                                        job = _JOBS[job_id]
                                        job.update(stage=stage, percent=int(pct), message=msg)
                                        job["logs"].append(f"{pct}% {msg}")
                                        job["logs"] = job["logs"][-80:]
                                result = bootstrap_install(self.data_dir, cid, progress)
                            _log("bootstrap done")
                            st = self._state(); en = st.setdefault("enabled", {}); en[cid] = True; self._write(st)
                            with _JOBS_LOCK:
                                _JOBS[job_id].update(status="done", percent=100, stage="done",
                                                     message="Installato", result={"path": result.get("component"),
                                                     "runtime": result.get("runtime"),
                                                     "manual_missing": result.get("manual_missing", [])})
                        except Exception as exc:
                            try:
                                with open(self.data_dir / "logs" / f"component-{cid}.log", "a", encoding="utf-8") as fh:
                                    fh.write(f"{utc_now_iso()} ERROR {exc}\n")
                            except Exception:
                                pass
                            with _JOBS_LOCK:
                                _JOBS[job_id].update(status="error", message=str(exc), stage="error")
                    threading.Thread(target=_worker, daemon=True).start()
                    return {"id": cid, "action": "install", "bootstrap": True, "job_id": job_id, "status": "running"}
                source = Path(local_path).expanduser().resolve()
                missing = [rel for rel in spec["required"] if not (source / rel).is_file()]
                if missing:
                    raise ComponentError(f"Percorso non valido; mancano {', '.join(missing)}")
                if target.exists() and (target / ".nlvs-bootstrap.json").is_file():
                    raise ComponentError("Componente già presente; rimuoverlo prima")
                self._copy_component(source, target)
                enabled[cid] = True
                self._write(state)
                return {"id": cid, "action": "install", "path": str(target),
                        "copied": True, "linked": False, "size_bytes": self._size(target),
                        "source": str(source)}
            if not target.exists() or target.is_symlink() or self._is_reparse(target):
                raise ComponentError("Componente non installato")
            if action == "repair":
                from .bootstrap import install as bootstrap_install
                job_id = uuid.uuid4().hex
                with _JOBS_LOCK:
                    _JOBS[job_id] = {"job_id": job_id, "component": cid, "status": "running",
                                     "stage": "queued", "percent": 0, "message": "Riparazione in coda", "logs": []}
                def _repair(job_id: str = job_id, cid: str = cid) -> None:
                    try:
                        with _INSTALL_LOCK:
                            result = bootstrap_install(self.data_dir, cid,
                                lambda st, pc, msg: _JOBS[job_id].update(stage=st, percent=int(pc), message=msg),
                                force=True)
                        st = self._state(); en = st.setdefault("enabled", {}); en[cid] = True; self._write(st)
                        with _JOBS_LOCK:
                            _JOBS[job_id].update(status="done", percent=100, stage="done", message="Riparato",
                                                 result={"runtime": result.get("runtime")})
                    except Exception as exc:
                        with _JOBS_LOCK:
                            _JOBS[job_id].update(status="error", message=str(exc), stage="error")
                threading.Thread(target=_repair, daemon=True).start()
                return {"id": cid, "action": "repair", "job_id": job_id, "status": "running"}
            if action == "verify":
                st = self._external_status(cid, spec)
                st["independent"] = st["present"] and st["links"] == 0
                st["operational"] = bool(st["independent"] and st["runtime_present"])
                return {"id": cid, "action": "verify", "ok": st["operational"], **st}
            if action == "import_checkpoint":
                if not local_path:
                    raise ComponentError("local_path richiesto per il checkpoint")
                source = Path(local_path).expanduser().resolve()
                target_file = target / "checkpoints" / "pretrained_ljs.pth"
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if source.is_file():
                    shutil.copy2(source, target_file)
                elif (source / "pretrained_ljs.pth").is_file():
                    shutil.copy2(source / "pretrained_ljs.pth", target_file)
                else:
                    raise ComponentError("pretrained_ljs.pth non trovato nel percorso")
                return {"id": cid, "action": "import_checkpoint", "copied": True, "path": str(target_file)}
            if action == "uninstall":
                if not self._is_managed(target):
                    raise ComponentError("Rifiuto di rimuovere un percorso non gestito")
                freed = self._size(target)
                removed = self._remove(target)
                enabled.pop(cid, None)
                self._write(state)
                return {"id": cid, "action": "uninstall", "removed": not target.exists(),
                        "freed_bytes": freed, "path": str(target)}
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

    def _copy_component(self, source: Path, target: Path) -> None:
        """Copy the imported component; never link back to the source."""
        if not source.is_dir():
            raise ComponentError("Il percorso di importazione deve essere una directory")
        try:
            if target.resolve() == source.resolve():
                raise ComponentError("La sorgente coincide con la destinazione gestita")
        except OSError:
            pass
        try:
            free = shutil.disk_usage(target.parent).free
            needed = self._size(source)
            if needed and free < needed + 64 * 1024 * 1024:
                raise ComponentError(
                    f"Spazio insufficiente: servono {needed // (1024 * 1024)} MB, liberi {free // (1024 * 1024)} MB")
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, target, symlinks=False, dirs_exist_ok=False)
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise ComponentError("Copia del componente fallita: " + str(exc)) from exc
        # Imported components must not retain links to the original tree.
        links = self._count_links(target)
        if links:
            shutil.rmtree(target, ignore_errors=True)
            raise ComponentError(f"La sorgente contiene {links} link simbolici; esportarla come copia reale")

    def _is_managed(self, target: Path) -> bool:
        try:
            target.resolve().relative_to(self.components_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            return bool(path.lstat().st_file_attributes & 0x400)
        except (AttributeError, OSError):
            return path.is_symlink()

    def _count_links(self, source: Path) -> int:
        count = 0
        for root, _dirs, files in os.walk(source, followlinks=False):
            if self._is_reparse(Path(root)):
                count += 1
            for name in files:
                if self._is_reparse(Path(root) / name):
                    count += 1
        return count

    @staticmethod
    def _remove(target: Path) -> bool:
        if not target.exists() and not target.is_symlink():
            return False
        if target.is_symlink():
            target.unlink()
            return True
        if os.name == "nt" and target.lstat().st_file_attributes & 0x400:
            completed = subprocess.run(["cmd", "/c", "rmdir", str(target)], capture_output=True, text=True)
            return completed.returncode == 0

        def _on_error(func, path, _exc):
            try:
                os.chmod(path, 0o700)
                func(path)
            except Exception:
                pass

        shutil.rmtree(target, onerror=_on_error)
        return not target.exists()

    def job(self, job_id: str) -> dict[str, Any]:
        with _JOBS_LOCK:
            return dict(_JOBS.get(job_id) or {"job_id": job_id, "status": "unknown"})

    def jobs(self) -> list[dict[str, Any]]:
        with _JOBS_LOCK:
            return [dict(job) for job in _JOBS.values()]

    def is_enabled(self, cid: str) -> bool:
        return bool(self._state().get("enabled", {}).get(cid, True))
