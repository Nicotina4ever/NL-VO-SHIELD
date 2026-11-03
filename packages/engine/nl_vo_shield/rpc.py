"""JSON-RPC 2.0 engine server over stdio (NDJSON).

* one JSON request/response per line on stdout; logs only on stderr;
* ``job.start``/``batch.start``/``models.download`` answer immediately and run
  asynchronously, emitting notifications (``job.progress``/``job.finished``/
  ``job.failed`` and ``models.progress``/``models.finished``/``models.failed``);
* every error is structured; the server never exits on a bad request.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import PROTOCOL_VERSION, __version__
from .context import AppContext
from .doctor import run_doctor
from .errors import NlvsError, PipelineError
from .pipeline import failure_result, run_batch, run_job
from .quality import analyze as analyze_quality
from .util import full_id, json_safe, sha256_file, utc_now_iso
from .verify import verify_file

log = logging.getLogger("nl_vo_shield.rpc")

# JSON-RPC standard codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APPLICATION_ERROR = -32000


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class JobManager:
    """Runs long operations on worker threads and emits notifications."""

    def __init__(self, notify: Callable[[str, dict[str, Any]], None], max_workers: int = 2) -> None:
        self._notify = notify
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nlvs-job")

    def start(
        self,
        kind: str,
        runner: Callable[[Callable[..., None], threading.Event], dict[str, Any]],
        *,
        job_id: str | None = None,
        progress_method: str = "job.progress",
        finished_method: str = "job.finished",
        failed_method: str = "job.failed",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = job_id or full_id()
        record: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "state": "queued",
            "progress": {"pct": 0, "message": "queued"},
            "created_at": utc_now_iso(),
            "cancel": threading.Event(),
            "result": None,
            "error": None,
            "extra": extra or {},
        }
        with self._lock:
            self._jobs[job_id] = record
        self._executor.submit(
            self._run,
            job_id,
            runner,
            progress_method,
            finished_method,
            failed_method,
        )
        return {"job_id": job_id, "state": "queued", "kind": kind}

    def _run(
        self,
        job_id: str,
        runner: Callable[[Callable[..., None], threading.Event], dict[str, Any]],
        progress_method: str,
        finished_method: str,
        failed_method: str,
    ) -> None:
        record = self._jobs[job_id]
        record["state"] = "running"
        record["started_at"] = utc_now_iso()

        def emit(pct: int | None, message: str, **extra: Any) -> None:
            payload = {
                "job_id": job_id,
                "pct": pct,
                "message": message,
                "step": extra.get("step"),
                "step_index": extra.get("step_index"),
                **{key: value for key, value in extra.items() if key not in ("step", "step_index")},
            }
            record["progress"] = payload
            try:
                self._notify(progress_method, json_safe(payload))
            except Exception:
                log.exception("Failed to emit progress notification")

        try:
            result = runner(emit, record["cancel"])
            record["result"] = result
            status = str(result.get("status"))
            if status == "failed":
                # Fail-closed result: the job completed but did not succeed.
                record["state"] = "failed"
                self._notify(failed_method, {"job_id": job_id, **record["extra"], "result": result})
            elif status == "cancelled":
                record["state"] = "cancelled"
                self._notify(finished_method, {"job_id": job_id, **record["extra"], "result": result})
            else:
                record["state"] = "finished"
                self._notify(finished_method, {"job_id": job_id, **record["extra"], "result": result})
        except Exception as exc:
            log.error("Job %s failed: %s", job_id, exc, exc_info=True)
            failure = failure_result(
                job_id,
                str(exc),
                code=getattr(exc, "code", "INTERNAL_ERROR"),
                detail=getattr(exc, "detail", {}),
            )
            record["result"] = failure
            record["error"] = str(exc)
            record["state"] = "failed"
            try:
                self._notify(failed_method, {"job_id": job_id, **record["extra"], "result": failure})
            except Exception:
                log.exception("Failed to emit failure notification")

    def cancel(self, job_id: str) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        record["cancel"].set()
        return True

    def status(self, job_id: str) -> dict[str, Any] | None:
        record = self._jobs.get(job_id)
        if record is None:
            return None
        return {
            "job_id": job_id,
            "kind": record["kind"],
            "state": record["state"],
            "progress": record["progress"],
            "created_at": record["created_at"],
            "started_at": record.get("started_at"),
            "result": record["result"] if record["state"] in ("finished", "failed", "cancelled") else None,
            "error": record["error"],
        }

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "job_id": record["job_id"],
                "kind": record["kind"],
                "state": record["state"],
                "created_at": record["created_at"],
                "progress": record["progress"],
            }
            for record in sorted(
                self._jobs.values(), key=lambda item: str(item["created_at"]), reverse=True
            )
        ]

    def shutdown(self) -> None:
        with self._lock:
            for record in self._jobs.values():
                record["cancel"].set()
        self._executor.shutdown(wait=False, cancel_futures=True)


class RpcServer:
    def __init__(self, ctx: AppContext, stdin=None, stdout=None) -> None:
        self.ctx = ctx
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self.jobs = JobManager(self.notify)
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "health": self._health,
            "engines.list": self._engines_list,
            "engines.install_hint": self._engines_install_hint,
            "job.start": self._job_start,
            "job.cancel": self._job_cancel,
            "job.status": self._job_status,
            "job.list": self._job_list,
            "verify.run": self._verify_run,
            "quality.analyze": self._quality_analyze,
            "batch.start": self._batch_start,
            "doctor.run": self._doctor_run,
            "profiles.list": self._profiles_list,
            "profiles.save": self._profiles_save,
            "profiles.delete": self._profiles_delete,
            "presets.list": self._presets_list,
            "presets.save": self._presets_save,
            "presets.delete": self._presets_delete,
            "settings.get": self._settings_get,
            "settings.set": self._settings_set,
            "receipts.list": self._receipts_list,
            "receipts.get": self._receipts_get,
            "receipts.verify": self._receipts_verify,
            "models.list": self._models_list,
            "models.download": self._models_download,
            "models.delete": self._models_delete,
            "models.prune": self._models_prune,
            "ai.status": self._ai_status,
            "ai.install": self._ai_install,
            "ai.uninstall": self._ai_uninstall,
            "ai.verify": self._ai_verify,
            "lab.status": self._lab_status,
            "lab.install_engine": self._lab_install_engine,
            "lab.uninstall_engine": self._lab_uninstall_engine,
            "lab.install_model": self._lab_install_model,
            "lab.uninstall_model": self._lab_uninstall_model,
            "lab.install_whisper": self._lab_install_whisper,
            "lab.uninstall_whisper": self._lab_uninstall_whisper,
            "lab.transcribe": self._lab_transcribe,
            "lab.run": self._lab_run,
            "lab.verify": self._lab_verify,
            "components.list": self._components_list,
            "components.action": self._components_action,
            "cache.size": self._cache_size,
            "cache.clear": self._cache_clear,
            "open_path": self._open_path,
            "shutdown": self._shutdown,
        }

    # ------------------------------------------------------------------ transport
    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(json_safe(payload), ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self._stdout.write(line + "\n")
            self._stdout.flush()

    def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": json_safe(result)})

    def _error(self, request_id: Any, code: int, message: str,
               data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        if data:
            payload["data"] = json_safe(data)
        self._write({"jsonrpc": "2.0", "id": request_id, "error": payload})

    # ------------------------------------------------------------------ loop
    def serve(self) -> int:
        log.info("NL VO Shield engine RPC %s (protocol %s) ready", __version__, PROTOCOL_VERSION)
        for raw_line in self._stdin:
            if self._stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._error(None, PARSE_ERROR, f"Parse error: {exc}")
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                self._error(request.get("id") if isinstance(request, dict) else None,
                            INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
                continue
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                self._error(request_id, INVALID_PARAMS, "params must be an object")
                continue
            if request_id is None:
                # Host notification; the engine does not need any at the moment.
                continue
            handler = self._handlers.get(str(method))
            if handler is None:
                self._error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
                continue
            try:
                result = handler(params)
                self._respond(request_id, result)
            except RpcError as exc:
                self._error(request_id, exc.code, exc.message, exc.data)
            except NlvsError as exc:
                self._error(request_id, APPLICATION_ERROR, str(exc), exc.to_dict())
            except Exception as exc:
                log.error("Unhandled error in %s: %s", method, exc, exc_info=True)
                self._error(
                    request_id,
                    INTERNAL_ERROR,
                    f"Internal error: {type(exc).__name__}",
                    {"type": type(exc).__name__, "message": str(exc)},
                )
            if self._stop.is_set():
                break
        self.jobs.shutdown()
        return 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _require(params: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
        value = params.get(key)
        if value is None:
            raise RpcError(INVALID_PARAMS, f"Missing required parameter: {key}")
        if not isinstance(value, kind):
            raise RpcError(INVALID_PARAMS, f"Parameter '{key}' has the wrong type")
        return value

    def _chain_from_params(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        chain = params.get("chain")
        preset_id = params.get("preset")
        if chain is not None:
            if not isinstance(chain, list):
                raise RpcError(INVALID_PARAMS, "chain must be a list of steps")
            return chain, preset_id
        if preset_id is None:
            preset_id = self.ctx.settings_public().get("default_preset", "studio_master")
        chain, preset = self.ctx.preset_chain(str(preset_id))
        if preset is None:
            raise RpcError(INVALID_PARAMS, f"Unknown preset: {preset_id}")
        return chain, str(preset_id)

    # ------------------------------------------------------------------ methods
    def _health(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.ctx.health()

    def _engines_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"engines": self.ctx.engine_infos()}

    def _engines_install_hint(self, params: dict[str, Any]) -> dict[str, Any]:
        engine_id = self._require(params, "engine_id", str)
        return self.ctx.install_hint(engine_id)

    def _job_start(self, params: dict[str, Any]) -> dict[str, Any]:
        input_path = self._require(params, "input_path", str)
        output_dir = self._require(params, "output_dir", str)
        chain, preset = self._chain_from_params(params)
        if not chain:
            raise RpcError(INVALID_PARAMS, "The protection chain is empty")
        options = params.get("options") or {}
        if not isinstance(options, dict):
            raise RpcError(INVALID_PARAMS, "options must be an object")
        if not Path(input_path).is_file():
            raise RpcError(INVALID_PARAMS, f"Input file not found: {input_path}")
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(step: str, step_index: int, pct: int | None, message: str) -> None:
                emit(pct, message, step=step, step_index=step_index)

            result = run_job(
                self.ctx,
                job_id=job_id,
                input_path=input_path,
                output_dir=output_dir,
                chain=chain,
                options=options,
                preset=preset,
                progress=progress,
                cancel=cancel,
            )
            return result

        started = self.jobs.start("protect", runner, job_id=job_id,
                                  extra={"input_path": input_path, "output_dir": output_dir,
                                         "preset": preset})
        return started

    def _job_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require(params, "job_id", str)
        return {"cancelled": self.jobs.cancel(job_id)}

    def _job_status(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require(params, "job_id", str)
        status = self.jobs.status(job_id)
        if status is None:
            raise RpcError(INVALID_PARAMS, f"Unknown job: {job_id}")
        return status

    def _job_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"jobs": self.jobs.list()}

    def _verify_run(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._require(params, "path", str)
        receipts_dir = params.get("receipts_dir")
        if receipts_dir is not None and not isinstance(receipts_dir, str):
            raise RpcError(INVALID_PARAMS, "receipts_dir must be a string")
        return verify_file(self.ctx, path, receipts_dir=receipts_dir)

    def _quality_analyze(self, params: dict[str, Any]) -> dict[str, Any]:
        from .audio_io import load_audio

        path = self._require(params, "path", str)
        reference_path = params.get("reference_path")
        if reference_path is not None and not isinstance(reference_path, str):
            raise RpcError(INVALID_PARAMS, "reference_path must be a string")
        buffer, info = load_audio(path)
        reference = None
        if reference_path:
            reference, _ref_info = load_audio(reference_path)
        metrics = analyze_quality(buffer, reference)
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "format": info.to_dict(),
            "metrics": json_safe(metrics),
        }

    def _batch_start(self, params: dict[str, Any]) -> dict[str, Any]:
        inputs = params.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise RpcError(INVALID_PARAMS, "inputs must be a non-empty list of paths")
        if not all(isinstance(item, str) for item in inputs):
            raise RpcError(INVALID_PARAMS, "inputs must contain only strings")
        output_dir = self._require(params, "output_dir", str)
        chain, preset = self._chain_from_params(params)
        if not chain:
            raise RpcError(INVALID_PARAMS, "The protection chain is empty")
        options = params.get("options") or {}
        if not isinstance(options, dict):
            raise RpcError(INVALID_PARAMS, "options must be an object")
        missing = [item for item in inputs if not Path(item).is_file()]
        if missing:
            raise RpcError(INVALID_PARAMS, f"Input files not found: {missing}")
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(step: str, step_index: int, pct: int | None, message: str) -> None:
                emit(pct, message, step=step, step_index=step_index)

            return run_batch(
                self.ctx,
                job_id=job_id,
                inputs=inputs,
                output_dir=output_dir,
                chain=chain,
                options=options,
                preset=preset,
                progress=progress,
                cancel=cancel,
            )

        started = self.jobs.start("batch", runner, job_id=job_id,
                                  extra={"output_dir": output_dir, "count": len(inputs)})
        return started

    def _doctor_run(self, params: dict[str, Any]) -> dict[str, Any]:
        return run_doctor(self.ctx)

    def _profiles_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"profiles": self.ctx.profiles.list()}

    def _profiles_save(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._require(params, "profile", dict)
        return {"profile": self.ctx.profiles.save(profile)}

    def _profiles_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        profile_id = self._require(params, "id", str)
        return {"deleted": self.ctx.profiles.delete(profile_id)}

    def _presets_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"presets": self.ctx.resolved_presets()}

    def _presets_save(self, params: dict[str, Any]) -> dict[str, Any]:
        preset = self._require(params, "preset", dict)
        return {"preset": self.ctx.presets.save(preset)}

    def _presets_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        preset_id = self._require(params, "id", str)
        return {"deleted": self.ctx.presets.delete(preset_id)}

    def _settings_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"settings": self.ctx.settings_public()}

    def _settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._require(params, "settings", dict)
        return {"settings": self.ctx.save_settings(settings)}

    def _receipts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = params.get("limit", 200)
        if not isinstance(limit, int) or limit <= 0:
            raise RpcError(INVALID_PARAMS, "limit must be a positive integer")
        return {"receipts": self.ctx.receipts.list(limit=limit)}

    def _receipts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        receipt_id = self._require(params, "id", str)
        receipt = self.ctx.receipts.get(receipt_id)
        if receipt is None:
            raise RpcError(INVALID_PARAMS, f"Unknown receipt: {receipt_id}")
        return {"receipt": receipt}

    def _receipts_verify(self, params: dict[str, Any]) -> dict[str, Any]:
        receipt_id = self._require(params, "id", str)
        trust_mode = str(self.ctx.settings_public().get("receipt_trust_mode", "local"))
        verification = self.ctx.receipts.verify(receipt_id, trust_mode=trust_mode)
        return {
            "valid": bool(verification.get("valid")),
            "reason": verification.get("reason"),
            "receipt": verification.get("receipt"),
        }

    def _models_list(self, params: dict[str, Any]) -> dict[str, Any]:
        verify_hash = params.get("verify_hash", True)
        if not isinstance(verify_hash, bool):
            raise RpcError(INVALID_PARAMS, "verify_hash must be a boolean")
        models = self.ctx.models.list(verify_hash=verify_hash)
        return {
            "models": models,
            "models_dir": self.ctx.settings["models_dir_resolved"],
            "cache_dir": self.ctx.settings["cache_dir_resolved"],
            "total_bytes": sum(item.get("size_bytes") or 0 for item in models if item.get("present")),
        }

    def _models_download(self, params: dict[str, Any]) -> dict[str, Any]:
        model_id = self._require(params, "id", str)
        if not params.get("confirmed"):
            raise RpcError(
                INVALID_PARAMS,
                "models.download requires confirmed=true: model downloads are always explicit",
                {"model_id": model_id, "requires_confirmation": True},
            )
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, model_id=model_id, downloaded=downloaded, total=total)

            return self.ctx.models.download(model_id, progress=progress, cancel=cancel)

        started = self.jobs.start(
            "models.download",
            runner,
            job_id=job_id,
            progress_method="models.progress",
            finished_method="models.finished",
            failed_method="models.failed",
            extra={"model_id": model_id},
        )
        return {**started, "model_id": model_id}

    def _models_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        model_id = self._require(params, "id", str)
        return {"deleted": self.ctx.models.delete(model_id)}

    def _models_prune(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.ctx.models.prune()

    # ------------------------------------------------------------------ AI runtime
    def _ai_runtime(self):
        from .ai_runtime import AIRuntime

        return AIRuntime(self.ctx.data_dir, self.ctx.models)

    def _ai_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"runtime": self._ai_runtime().status()}

    def _ai_install(self, params: dict[str, Any]) -> dict[str, Any]:
        variant = str(params.get("variant") or "cpu").lower()
        if variant not in ("cpu", "gpu"):
            raise RpcError(INVALID_PARAMS, "variant must be 'cpu' or 'gpu'")
        if not params.get("confirmed"):
            raise RpcError(
                INVALID_PARAMS,
                "ai.install requires confirmed=true: AI runtime downloads are always explicit",
                {"variant": variant, "requires_confirmation": True},
            )
        runtime = self._ai_runtime()
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, downloaded=downloaded, total=total)

            return runtime.install(variant, progress=progress, cancel=cancel)

        started = self.jobs.start(
            "ai.install",
            runner,
            job_id=job_id,
            progress_method="ai.progress",
            finished_method="ai.finished",
            failed_method="ai.failed",
            extra={"variant": variant},
        )
        return {**started, "variant": variant}

    def _ai_uninstall(self, params: dict[str, Any]) -> dict[str, Any]:
        if not params.get("confirmed"):
            raise RpcError(
                INVALID_PARAMS,
                "ai.uninstall requires confirmed=true",
                {"requires_confirmation": True},
            )
        remove_models = bool(params.get("remove_models", False))
        return self._ai_runtime().uninstall(remove_models=remove_models)

    def _ai_verify(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._ai_runtime().verify()

    # ------------------------------------------------------------------ Qwen lab
    def _qwen_lab(self):
        from .qwen_lab import QwenLab

        return QwenLab(self.ctx.data_dir, self.ctx.models)

    def _lab_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"lab": self._qwen_lab().status()}

    def _lab_install_engine(self, params: dict[str, Any]) -> dict[str, Any]:
        device = str(params.get("device") or "cpu").lower()
        if device not in ("cpu", "cuda", "auto"):
            raise RpcError(INVALID_PARAMS, "device must be 'cpu', 'cuda' or 'auto'")
        lab = self._qwen_lab()
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, downloaded=downloaded, total=total)

            return lab.install_engine(device, progress=progress, cancel=cancel)

        started = self.jobs.start(
            "lab.install_engine",
            runner,
            job_id=job_id,
            progress_method="lab.progress",
            finished_method="lab.finished",
            failed_method="lab.failed",
            extra={"device": device},
        )
        return {**started, "device": device}

    def _lab_uninstall_engine(self, params: dict[str, Any]) -> dict[str, Any]:
        remove_models = bool(params.get("remove_models", False))
        return {"lab": self._qwen_lab().uninstall_engine(remove_models=remove_models)}

    def _lab_install_model(self, params: dict[str, Any]) -> dict[str, Any]:
        model_id = self._require(params, "id", str)
        local_path = params.get("local_path")
        source = params.get("source")
        if local_path is not None and not isinstance(local_path, str):
            raise RpcError(INVALID_PARAMS, "local_path must be a string")
        if source is not None and not isinstance(source, str):
            raise RpcError(INVALID_PARAMS, "source must be a string")
        lab = self._qwen_lab()
        if local_path:
            return {"lab": lab.install_model(model_id, source=source, local_path=local_path)}
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, downloaded=downloaded, total=total)

            return lab.install_model(model_id, source=source, progress=progress, cancel=cancel)

        started = self.jobs.start(
            "lab.install_model",
            runner,
            job_id=job_id,
            progress_method="lab.progress",
            finished_method="lab.finished",
            failed_method="lab.failed",
            extra={"model_id": model_id},
        )
        return {**started, "model_id": model_id}

    def _lab_uninstall_model(self, params: dict[str, Any]) -> dict[str, Any]:
        model_id = self._require(params, "id", str)
        return {"lab": self._qwen_lab().uninstall_model(model_id)}

    def _lab_install_whisper(self, params: dict[str, Any]) -> dict[str, Any]:
        size = str(params.get("size") or params.get("model") or "base")
        local_path = params.get("local_path")
        if local_path is not None and not isinstance(local_path, str):
            raise RpcError(INVALID_PARAMS, "local_path must be a string")
        lab = self._qwen_lab()
        if local_path:
            return {"lab": lab.install_whisper(size, local_path=local_path)}
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, downloaded=downloaded, total=total)

            return lab.install_whisper(size, progress=progress, cancel=cancel)

        started = self.jobs.start(
            "lab.install_whisper",
            runner,
            job_id=job_id,
            progress_method="lab.progress",
            finished_method="lab.finished",
            failed_method="lab.failed",
            extra={"size": size},
        )
        return {**started, "size": size}

    def _lab_uninstall_whisper(self, params: dict[str, Any]) -> dict[str, Any]:
        size = str(params.get("size") or params.get("model") or "base")
        return {"lab": self._qwen_lab().uninstall_whisper(size)}

    def _lab_transcribe(self, params: dict[str, Any]) -> dict[str, Any]:
        audio = self._require(params, "audio", str)
        model = str(params.get("model") or "base")
        language = params.get("language")
        if language is not None and not isinstance(language, str):
            raise RpcError(INVALID_PARAMS, "language must be a string")
        device = str(params.get("device") or "cpu")
        if device not in ("cpu", "cuda", "auto"):
            raise RpcError(INVALID_PARAMS, "device must be 'cpu', 'cuda' or 'auto'")
        return {"transcription": self._qwen_lab().transcribe(
            audio, model=model, language=language or None, device=device
        )}

    def _lab_run(self, params: dict[str, Any]) -> dict[str, Any]:
        original = self._require(params, "original", str)
        protected = self._require(params, "protected", str)
        text = self._require(params, "text", str)
        ref_text = params.get("ref_text")
        if ref_text is not None and not isinstance(ref_text, str):
            raise RpcError(INVALID_PARAMS, "ref_text must be a string")
        x_vector_only = bool(params.get("x_vector_only", False))
        if not x_vector_only and not str(ref_text or "").strip():
            raise RpcError(
                INVALID_PARAMS,
                "ref_text is required unless x_vector_only=true: transcribe the reference with "
                "lab.transcribe and pass the text explicitly",
                {"code": "LAB_REF_TEXT_REQUIRED", "requires": ["ref_text"]},
            )
        model_id = str(params.get("model_id") or "qwen3-tts-0.6b")
        language = str(params.get("language") or "Italian")
        try:
            seed = int(params.get("seed", 0))
        except (TypeError, ValueError) as exc:
            raise RpcError(INVALID_PARAMS, "seed must be an integer") from exc
        device = str(params.get("device") or "cpu")
        if device not in ("cpu", "cuda", "auto"):
            raise RpcError(INVALID_PARAMS, "device must be 'cpu', 'cuda' or 'auto'")
        output_dir = params.get("output_dir")
        if output_dir is not None and not isinstance(output_dir, str):
            raise RpcError(INVALID_PARAMS, "output_dir must be a string")
        lab = self._qwen_lab()
        job_id = full_id()

        def runner(emit: Callable[..., None], cancel: threading.Event) -> dict[str, Any]:
            def progress(pct: int | None, message: str, downloaded: int, total: int | None) -> None:
                emit(pct, message, downloaded=downloaded, total=total)

            return lab.run(
                original=original,
                protected=protected,
                text=text,
                ref_text=ref_text,
                model_id=model_id,
                language=language,
                seed=seed,
                device=device,
                x_vector_only=x_vector_only,
                output_dir=output_dir,
                progress=progress,
                cancel=cancel,
                job_id=job_id,
            )

        started = self.jobs.start(
            "lab.run",
            runner,
            job_id=job_id,
            progress_method="lab.progress",
            finished_method="lab.finished",
            failed_method="lab.failed",
            extra={"model_id": model_id, "device": device},
        )
        return {**started, "model_id": model_id}

    def _lab_verify(self, params: dict[str, Any]) -> dict[str, Any]:
        deep = bool(params.get("deep", False))
        return {"lab": self._qwen_lab().verify(deep=deep)}

    def _components(self):
        from .components import ComponentManager

        return ComponentManager(self.ctx.data_dir, self.ctx)

    def _components_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._components().list()

    def _components_action(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"component": self._components().action(
            str(params.get("id") or ""),
            str(params.get("action") or ""),
            local_path=params.get("local_path"),
            confirmed=bool(params.get("confirmed", False)),
        )}

    def _cache_size(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.ctx.cache.size()

    def _cache_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.ctx.cache.clear()

    def _open_path(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require(params, "path", str)
        return {
            "opened": False,
            "reason": "shell integration is implemented by the desktop host (Rust), not the engine",
        }

    def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self._stop.set()
        return {"ok": True}


def _warm_up_imports(ctx: AppContext) -> dict[str, Any]:
    """Import heavy native libraries in the main thread before serving.

    On Windows, importing scipy/torch (native DLL initialization) from a worker
    thread while the main thread is blocked reading stdin can deadlock. Warming
    the imports here removes that class of hangs and lowers first-job latency.
    """
    import importlib
    import importlib.util
    import time

    modules = [
        "numpy",
        "soundfile",
        "scipy",
        "scipy.signal",
        "scipy.ndimage",
        "scipy.linalg",
        "cryptography",
        "pyloudnorm",
    ]
    try:
        models_ready, _reason = ctx.models.is_ready("audioseal")
    except Exception:
        models_ready = False
    if models_ready and importlib.util.find_spec("torch") is not None:
        modules += ["torch", "audioseal"]
    started = time.time()
    warmed: list[str] = []
    failed: dict[str, str] = {}
    for name in modules:
        try:
            importlib.import_module(name)
            warmed.append(name)
        except Exception as exc:  # optional libraries may be absent
            failed[name] = f"{type(exc).__name__}: {exc}"
    return {
        "seconds": round(time.time() - started, 2),
        "warmed": warmed,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="nl_vo_shield.rpc", description="NL VO Shield engine JSON-RPC server")
    parser.add_argument("--data-dir", required=False, help="application data directory")
    parser.add_argument("--portable", action="store_true", help="portable data directory next to the executable")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        ctx = AppContext(args.data_dir, portable=True if args.portable else None)
    except NlvsError as exc:
        log.error("Cannot start the engine: %s", exc)
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": exc.to_dict()}), flush=True)
        return 1
    warmup = _warm_up_imports(ctx)
    log.info(
        "Engine warm-up: %.2fs (%d modules)%s",
        warmup["seconds"],
        len(warmup["warmed"]),
        f"; unavailable: {sorted(warmup['failed'])}" if warmup["failed"] else "",
    )
    server = RpcServer(ctx)
    try:
        return server.serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
