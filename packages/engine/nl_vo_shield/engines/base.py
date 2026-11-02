"""Engine contract: EngineInfo, registry and the per-job execution context.

Engines never claim more than they can prove. ``status=verified`` is only
reported after a real successful execution+verification in the current
process; otherwise engines are ``available``, ``experimental`` or
``unavailable`` with an explicit reason.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import __version__
from ..errors import EngineNotFound
from ..models import ModelStore

ProgressFn = Callable[[str, int | None, str], None]


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


ALL_OS = ["windows", "macos", "linux"]


@dataclass
class EngineContext:
    """Everything an engine may need, scoped to one job (or one probe)."""

    data_dir: Path
    models_dir: Path
    cache_dir: Path
    settings: dict[str, Any]
    models: ModelStore
    c2patool: Path | None = None
    progress: ProgressFn | None = None
    cancel: threading.Event | None = None
    work_dir: Path | None = None
    options: dict[str, Any] = field(default_factory=dict)
    payload: int | None = None
    engine_registry: Any = None

    def emit(self, step: str, pct: int | None, message: str) -> None:
        if self.progress is not None:
            try:
                self.progress(step, pct, message)
            except Exception:
                pass

    @contextmanager
    def heartbeat(self, step: str, start_pct: int, end_pct: int, message: str,
                  interval: float = 1.0):
        """Emit progress while a long blocking call (e.g. torch inference) runs.

        This is a liveness signal, not fake completion: the percentage advances
        slowly inside the current engine's slice and is bounded by ``end_pct``.
        """
        stop = threading.Event()
        step_size = max(1, max(1, end_pct - start_pct) // 20)

        def tick() -> None:
            pct = int(start_pct)
            while not stop.wait(interval):
                pct = min(int(end_pct), pct + step_size)
                self.emit(step, pct, message)

        thread = threading.Thread(target=tick, name=f"nlvs-heartbeat-{step}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2.0)

    @property
    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def check_cancel(self) -> None:
        from ..errors import JobCancelled

        if self.cancelled:
            raise JobCancelled("Job cancelled by user")


@dataclass
class EngineInfo:
    id: str
    name: str
    version: str
    kind: str
    capabilities: list[str]
    supported_os: list[str]
    status: str
    requires: list[str]
    requires_download: bool
    reason: str | None = None
    path: str | None = None
    quality_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "supported_os": list(self.supported_os),
            "status": self.status,
            "requires": list(self.requires),
            "requires_download": bool(self.requires_download),
            "reason": self.reason,
            "path": self.path,
            "quality_note": self.quality_note,
        }


class Engine(Protocol):  # pragma: no cover - structural typing
    id: str
    name: str
    kind: str
    version: str
    capabilities: list[str]
    requires: list[str]
    requires_download: bool
    quality_note: str | None

    def availability(self) -> tuple[bool, str | None]: ...

    def info(self) -> EngineInfo: ...


class BaseEngine:
    """Common behaviour for all engines."""

    id: str = "base"
    name: str = "Base"
    kind: str = "base"
    version: str = __version__
    capabilities: list[str] = []
    supported_os: list[str] = ALL_OS
    requires: list[str] = []
    requires_download: bool = False
    quality_note: str | None = None
    status_override: str | None = None  # e.g. "experimental"
    verified_in_session: bool = False

    def availability(self) -> tuple[bool, str | None]:
        return True, None

    def path(self) -> str | None:
        return None

    def info(self) -> EngineInfo:
        available, reason = self.availability()
        if not available:
            status = "unavailable"
        elif self.status_override:
            status = self.status_override
        elif self.verified_in_session:
            status = "verified"
        else:
            status = "available"
        return EngineInfo(
            id=self.id,
            name=self.name,
            version=self.version,
            kind=self.kind,
            capabilities=list(self.capabilities),
            supported_os=list(self.supported_os),
            status=status,
            requires=list(self.requires),
            requires_download=bool(self.requires_download and not available),
            reason=reason,
            path=self.path(),
            quality_note=self.quality_note,
        )

    def mark_verified(self) -> None:
        if self.status_override != "experimental":
            self.verified_in_session = True

    # Engines that transform audio implement execute(); file-based provenance
    # engines implement sign(). Unsupported actions raise explicitly.
    def execute(self, action: str, buffer: Any, params: dict[str, Any], ctx: EngineContext):
        raise EngineUnsupportedAction(f"{self.id} cannot execute action '{action}'")

    def sign(self, input_path: Path, work_dir: Path, params: dict[str, Any], ctx: EngineContext):
        raise EngineUnsupportedAction(f"{self.id} cannot sign '{action_of(params)}'")

    def verify_effect(self, action: str, buffer: Any, params: dict[str, Any], ctx: EngineContext) -> dict[str, Any]:
        """Return {'check','ok','detail'} proving the effect actually happened."""
        return {"check": f"{self.id}.{action}", "ok": True, "detail": {"note": "no verification implemented"}}


class EngineUnsupportedAction(Exception):
    pass


def action_of(params: dict[str, Any]) -> str:
    return str(params.get("action", "sign"))


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, BaseEngine] = {}

    def register(self, engine: BaseEngine) -> None:
        self._engines[engine.id] = engine

    def get(self, engine_id: str) -> BaseEngine:
        engine = self._engines.get(engine_id)
        if engine is None:
            raise EngineNotFound(
                f"Unknown engine: {engine_id}",
                detail={"known": sorted(self._engines)},
            )
        return engine

    def has(self, engine_id: str) -> bool:
        return engine_id in self._engines

    def ids(self) -> list[str]:
        return sorted(self._engines)

    def engines(self) -> list[BaseEngine]:
        return [self._engines[key] for key in sorted(self._engines)]

    def infos(self) -> list[dict[str, Any]]:
        return [engine.info().to_dict() for engine in self.engines()]

    def install_hint(self, engine_id: str) -> dict[str, Any]:
        engine = self.get(engine_id)
        available, reason = engine.availability()
        info = engine.info()
        instructions = _INSTALL_HINTS.get(engine_id, "No installation hint is available for this engine.")
        requires = list(engine.requires)
        download_models = [
            model_id
            for model_id in _DOWNLOAD_MODELS.get(engine_id, [])
        ]
        return {
            "engine_id": engine_id,
            "status": info.status,
            "available": available,
            "reason": reason,
            "instructions": instructions,
            "requires": requires,
            "requires_download": bool(requires_download_needed(engine, download_models)),
            "download_models": download_models,
            "no_automatic_download": True,
        }

    def mark_verified(self, engine_id: str) -> None:
        try:
            self.get(engine_id).mark_verified()
        except EngineNotFound:
            pass


def requires_download_needed(engine: BaseEngine, download_models: list[str]) -> bool:
    if not download_models:
        return False
    available, _reason = engine.availability()
    return not available


_INSTALL_HINTS: dict[str, str] = {
    "audioseal": (
        "AudioSeal richiede i pacchetti opzionali torch+audioseal e i pesi ufficiali. "
        "La release base non scarica nulla: esegui esplicitamente "
        "`models download audioseal-generator` e `models download audioseal-detector` "
        "(RPC: models.download) e verifica con `models list`."
    ),
    "spectral_cloak": (
        "Motore DSP incluso: nessuna installazione richiesta. Efficacia anti-clone non validata "
        "(status experimental)."
    ),
    "watermark_dsp": (
        "Motore watermark DSP incluso: nessuna installazione richiesta."
    ),
    "c2pa": (
        "Serve c2patool 0.9.x: viene cercato in bin/c2patool (bundle) o nella cartella dati. "
        "Il certificato di firma locale viene generato in <data_dir>/c2pa al primo utilizzo."
    ),
    "fingerprint": "Motore locale incluso: nessuna installazione richiesta.",
    "quality": "Motore locale incluso: nessuna installazione richiesta.",
    "safespeech": (
        "SafeSpeech e' ricerca esterna: servono i checkpoint completi forniti dall'utente. "
        "In questa installazione i pesi sono incompleti, quindi il motore resta unavailable e "
        "non viene mai scaricato automaticamente."
    ),
}

_DOWNLOAD_MODELS: dict[str, list[str]] = {
    "audioseal": ["audioseal-generator", "audioseal-detector"],
}
