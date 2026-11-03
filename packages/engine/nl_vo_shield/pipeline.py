"""Fail-closed protection pipeline.

Core rule (from MISSIONE_MASTER §2 and the legacy bug being fixed): a required
step that fails invalidates the result. ``status=protected`` is returned only
when every required step was executed and every necessary verification passed.
In every other case no file is ever written to the "protected" output path; a
clearly marked ``_PARTIAL_`` file is written for recovery only.

The legacy launcher copied ``*_PROTETTO.wav`` even after the C2PA signature
failed; this module cannot do that by construction: the final path is only
materialised in the ``protected`` branch after all verifications succeed.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
import numpy as np

from .audio_io import AudioBuffer, choose_subtype, load_audio, write_audio_atomic
from .quality import _true_peak
from .engines.base import EngineContext, EngineRegistry
from .errors import (
    EngineUnavailable,
    JobCancelled,
    NlvsError,
    PipelineError,
)
from .quality import analyze as analyze_quality
from .util import (
    dir_size,
    json_safe,
    safe_rmtree,
    sha256_file,
    short_id,
    utc_now_iso,
)

ProgressCallback = Callable[[str, int, int | None, str], None]  # step, step_index, pct, message

_PROVENANCE_ENGINES = {"c2pa"}
_TRANSFORM_ACTIONS = {"embed", "perturb"}
_VERIFY_ACTIONS = {"detect", "verify"}
_SIGN_ACTIONS = {"sign"}
_ALLOWED_ACTIONS = _TRANSFORM_ACTIONS | _VERIFY_ACTIONS | _SIGN_ACTIONS


# Per-engine headroom needed by the perturbation.  It is intentionally small
# and derived from the requested intensity instead of a blanket -1 dBTP.
_HEADROOM_BY_STRENGTH = {
    "spectral_cloak": {"low": 0.03, "medium": 0.05, "high": 0.10, "extreme": 0.15},
    "safe_speech": {"low": 0.02, "medium": 0.04, "high": 0.08},
    "safespeech": {"low": 0.02, "medium": 0.04, "high": 0.08},
    "mimic_blocker": {"low": 0.02, "medium": 0.04, "high": 0.08},
    "enkidu": {"low": 0.02, "medium": 0.04, "high": 0.08},
}
_HEADROOM_DEFAULT = 0.02
_TRUE_PEAK_CEILING = 0.999


def _headroom_for_step(step: "Step") -> float:
    explicit = step.params.get("headroom")
    if isinstance(explicit, (int, float)) and 0.0 < float(explicit) < 0.5:
        return float(explicit)
    table = _HEADROOM_BY_STRENGTH.get(step.engine, {})
    strength = str(step.params.get("strength", "medium")).lower()
    return float(table.get(strength, _HEADROOM_DEFAULT))


def _prepare_headroom(buffer: AudioBuffer, step: "Step") -> tuple[AudioBuffer, dict[str, float] | None]:
    """Attenuate a *temporary working copy* enough for the perturbation.

    Originals are never touched: the returned buffer is a new object.  The
    caller keeps the original input for quality/provenance reporting.
    """
    if step.action != "perturb":
        return buffer, None
    headroom = _headroom_for_step(step)
    ceiling = 1.0 - headroom
    peak = buffer.peak
    if peak <= ceiling or peak <= 0.0:
        return buffer, None
    gain = ceiling / peak
    out = buffer.copy()
    out.samples = np.ascontiguousarray(out.samples * np.float32(gain), dtype=np.float32)
    gain_db = 20.0 * float(np.log10(gain))
    out.notes.append(
        f"auto headroom: attenuated input by {gain_db:.2f} dB to leave {headroom:.3f} for {step.engine}"
    )
    return out, {"gain_db": round(gain_db, 3), "ceiling": round(ceiling, 4), "required_headroom": headroom}


@dataclass
class Step:
    engine: str
    action: str
    required: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        if not isinstance(data, dict):
            raise PipelineError("Each chain step must be an object")
        engine = data.get("engine")
        action = data.get("action")
        if not isinstance(engine, str) or not engine:
            raise PipelineError("Each chain step needs a non-empty 'engine' string")
        if not isinstance(action, str) or not action:
            raise PipelineError(f"Chain step '{engine}' needs a non-empty 'action' string")
        if action not in _ALLOWED_ACTIONS:
            raise PipelineError(
                f"Unsupported action '{action}' for engine '{engine}'",
                detail={"allowed": sorted(_ALLOWED_ACTIONS)},
            )
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise PipelineError(f"params for step '{engine}' must be an object")
        required = data.get("required", True)
        if not isinstance(required, bool):
            raise PipelineError(f"'required' for step '{engine}' must be a boolean")
        return cls(engine=engine, action=action, required=required, params=dict(params))

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "action": self.action,
            "required": self.required,
            "params": self.params,
        }


def prepare_chain(
    chain: list[dict[str, Any]], registry: EngineRegistry
) -> tuple[list[Step], list[str]]:
    steps = [Step.from_dict(item) for item in chain]
    if not steps:
        raise PipelineError("The protection chain is empty: nothing would be applied")
    warnings: list[str] = []
    # Duplicate steps would silently apply the same transformation twice.
    seen_steps: dict[tuple[str, str], int] = {}
    for step in steps:
        key = (step.engine, step.action)
        seen_steps[key] = seen_steps.get(key, 0) + 1
    duplicates = [f"{engine}.{action}" for (engine, action), count in seen_steps.items() if count > 1]
    if duplicates:
        raise PipelineError(
            "Duplicate chain steps are not allowed: " + ", ".join(duplicates),
            code="CHAIN_DUPLICATE_STEP",
            detail={"duplicates": duplicates},
        )
    # Only one anti-clone perturbation can be applied meaningfully.
    anti_clone = [
        step.engine
        for step in steps
        if _engine_kind(step.engine, registry) == "anti_clone" and step.action in _TRANSFORM_ACTIONS
    ]
    if len(anti_clone) > 1:
        raise PipelineError(
            "Only one anti-clone perturbation can be applied per chain (incompatible engines: "
            + ", ".join(anti_clone)
            + ")",
            code="CHAIN_INCOMPATIBLE_ANTICLONE",
            detail={"engines": anti_clone},
        )
    # C2PA must be the final transformation, otherwise the signature would cover
    # audio that is modified afterwards.
    provenance = [step for step in steps if _is_provenance(step, registry)]
    others = [step for step in steps if not _is_provenance(step, registry)]
    if provenance and steps[-1] not in provenance:
        warnings.append(
            "C2PA signing steps were moved to the end of the chain so the signature covers "
            "the final audio (recommended order: anti-clone -> watermark -> C2PA)."
        )
    ordered = others + provenance
    for step in ordered:
        engine_kind = _engine_kind(step.engine, registry)
        if engine_kind in ("quality", "fingerprint"):
            raise PipelineError(
                f"Engine '{step.engine}' is a measurement engine and cannot be used as a "
                "protection step"
            )
        if step.action in _SIGN_ACTIONS and engine_kind != "provenance":
            raise PipelineError(
                f"Action 'sign' is only valid for provenance engines (got '{step.engine}')"
            )
        if step.action in _TRANSFORM_ACTIONS and engine_kind == "provenance":
            raise PipelineError(
                f"Engine '{step.engine}' cannot transform audio (action '{step.action}')"
            )
    return ordered, warnings


def _engine_kind(engine_id: str, registry: EngineRegistry) -> str:
    if not registry.has(engine_id):
        return "unknown"
    return registry.get(engine_id).kind


def _is_provenance(step: Step, registry: EngineRegistry) -> bool:
    if step.engine in _PROVENANCE_ENGINES:
        return True
    return _engine_kind(step.engine, registry) == "provenance"


def _requested_kinds(steps: list[Step], registry: EngineRegistry) -> list[str]:
    kinds: list[str] = []
    for step in steps:
        kind = _engine_kind(step.engine, registry)
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _quality_summary(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metrics:
        return None
    keys = (
        "sample_rate",
        "channels",
        "frames",
        "duration_seconds",
        "peak_dbfs",
        "true_peak_dbtp",
        "rms_dbfs",
        "lufs_integrated",
        "clipping_samples",
        "clipping_ratio",
    )
    summary = {key: metrics.get(key) for key in keys}
    summary["notes"] = metrics.get("notes") or []
    reference = metrics.get("reference") or {}
    if reference:
        summary["reference"] = {
            key: reference.get(key)
            for key in ("snr_db", "max_abs_delta", "rms_delta_db", "log_spectral_distance_db", "identical")
        }
    return json_safe(summary)


def failure_result(
    job_id: str,
    message: str,
    *,
    code: str = "PIPELINE_ERROR",
    detail: dict[str, Any] | None = None,
    input_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a contract-shaped JobResult for an exception that escaped run_job."""
    result = _result_skeleton(job_id, utc_now_iso())
    result["status"] = "failed"
    result["failed"] = [
        {
            "engine": "pipeline",
            "action": "run",
            "error": str(message),
            "required": True,
            "code": code,
            "detail": detail or {},
        }
    ]
    if input_path is not None:
        result["input_path"] = str(input_path)
    result["finished_at"] = utc_now_iso()
    return result


def _result_skeleton(job_id: str, started_at: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "failed",
        "requested": [],
        "executed": [],
        "verified": [],
        "skipped": [],
        "failed": [],
        "quality": {"input": None, "output": None},
        "hashes": {"input_sha256": None, "output_sha256": None, "output_partial_sha256": None},
        "output_path": None,
        "output_partial_path": None,
        "receipt_id": None,
        "warnings": [],
        "started_at": started_at,
        "finished_at": None,
        "software": {"name": "NL VO Shield", "version": __version__},
    }


def _parse_payload(options: dict[str, Any]) -> int | None:
    if "payload_hex" in options and options["payload_hex"] not in (None, ""):
        try:
            value = int(str(options["payload_hex"]), 16)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"options.payload_hex is not valid hex: {options['payload_hex']!r}") from exc
        if not 0 <= value <= 0xFFFF:
            raise PipelineError("options.payload_hex must fit in 16 bits (0x0000-0xffff)")
        return value
    if "payload_16bit" in options and options["payload_16bit"] is not None:
        try:
            value = int(options["payload_16bit"])
        except (TypeError, ValueError) as exc:
            raise PipelineError("options.payload_16bit must be an integer") from exc
        if not 0 <= value <= 0xFFFF:
            raise PipelineError("options.payload_16bit must be in [0, 65535]")
        return value
    return None


class _ProgressMapper:
    def __init__(self, callback: ProgressCallback | None, total_steps: int) -> None:
        self.callback = callback
        self.total_steps = max(1, total_steps)

    def emit(self, step: str, step_index: int, pct: int | None, message: str) -> None:
        if self.callback is None:
            return
        fraction = 100.0 / self.total_steps
        overall = int(step_index * fraction + (pct if pct is not None else 0) * fraction / 100.0)
        overall = max(0, min(99, overall))
        try:
            self.callback(step, step_index, overall, message)
        except Exception:
            pass


def run_job(
    ctx: Any,
    *,
    job_id: str,
    input_path: str | Path,
    output_dir: str | Path,
    chain: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    preset: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one fail-closed protection job and return a JobResult dict."""
    started_at = utc_now_iso()
    # Normalise every path to an absolute form immediately: external tools
    # (c2patool, ffmpeg) and the job temp directory resolution depend on it.
    input_path = Path(input_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    options = dict(options or {})
    cancel = cancel or threading.Event()

    if not input_path.is_file():
        raise PipelineError(
            f"Input file not found: {input_path}",
            code="INPUT_NOT_FOUND",
            detail={"input_path": str(input_path)},
        )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(
            f"Cannot create the output directory: {output_dir}: {exc}",
            code="OUTPUT_DIR_UNWRITABLE",
        ) from exc
    if input_path.resolve() == output_dir.resolve():
        raise PipelineError("The output directory cannot be the input file", code="OUTPUT_EQUALS_INPUT")

    steps, chain_warnings = prepare_chain(ctx.resolve_auto_engines(chain), ctx.registry)
    result = _result_skeleton(job_id, started_at)
    result["requested"] = _requested_kinds(steps, ctx.registry)
    result["requested_engines"] = [step.engine for step in steps]
    result["warnings"].extend(chain_warnings)
    result["preset"] = preset
    result["input_path"] = str(input_path)
    if options.get("profile_id"):
        result["profile_id"] = options["profile_id"]

    work_dir = output_dir / f".nlvs_job_{short_id(job_id)}"
    try:
        work_dir.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise PipelineError(f"Cannot create the job temporary directory: {work_dir}") from exc

    mapper = _ProgressMapper(progress, len(steps))
    executed: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    warnings = result["warnings"]
    cancelled = False
    required_failed = False
    transforms_executed = False
    payload = _parse_payload(options)
    source_info = None
    input_sha = None
    input_buffer: AudioBuffer | None = None
    input_quality = None
    current_buffer: AudioBuffer | None = None
    current_file: Path | None = None
    ext = input_path.suffix.lower() or ".wav"

    try:
        try:
            input_buffer, source_info = load_audio(input_path, temp_dir=work_dir, compute_sha256=True)
            input_sha = source_info.sha256
            current_buffer = input_buffer
        except NlvsError as exc:
            failed.append(
                {"engine": "audio_io", "action": "load", "error": str(exc),
                 "required": True, "code": getattr(exc, "code", "AUDIO_ERROR")}
            )
            required_failed = True
            result["failed"] = failed
            result["status"] = "failed"
            result["finished_at"] = utc_now_iso()
            return result

        try:
            input_quality = analyze_quality(input_buffer)
        except Exception as exc:  # measurement must never break protection
            warnings.append(f"Input quality analysis failed: {type(exc).__name__}: {exc}")

        progress_index = {"value": 0}
        ectx = ctx.engine_context(
            progress=lambda step, pct, message: mapper.emit(
                step, progress_index["value"], pct, message
            ),
            cancel=cancel,
            work_dir=work_dir,
            options=options,
            payload=payload,
        )

        for index, step in enumerate(steps):
            progress_index["value"] = index
            if cancel.is_set():
                cancelled = True
                break
            mapper.emit(step.engine, index, 0, f"{step.action}: start")
            engine_kind = _engine_kind(step.engine, ctx.registry)
            if not ctx.registry.has(step.engine):
                message = f"Unknown engine '{step.engine}'"
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": message, "required": True,
                                   "code": "ENGINE_NOT_FOUND"})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action, "reason": message})
                warnings.append(f"Optional engine '{step.engine}' unknown; skipped")
                continue
            engine = ctx.registry.get(step.engine)
            from .components import ComponentManager
            if not ComponentManager(ctx.data_dir).is_enabled(step.engine):
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": "component disabled in the universal component manager",
                                   "required": True, "code": "COMPONENT_DISABLED"})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action,
                                "reason": "component disabled"})
                continue
            available, reason = engine.availability()
            if not available:
                message = reason or f"Engine '{step.engine}' is unavailable"
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": message, "required": True,
                                   "code": "ENGINE_UNAVAILABLE"})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action, "reason": message})
                warnings.append(f"Optional engine '{step.engine}' unavailable; skipped")
                continue
            if step.action == "embed" and ectx.payload is None:
                ectx.payload = secrets.randbits(16)
                payload = ectx.payload
            try:
                if step.action in _TRANSFORM_ACTIONS:
                    if current_file is not None:
                        current_buffer, _info = load_audio(current_file, temp_dir=work_dir)
                        current_file = None
                    assert current_buffer is not None
                    before = current_buffer
                    working, headroom_info = _prepare_headroom(current_buffer, step)
                    if headroom_info is not None:
                        warnings.append(
                            f"{step.engine}: input attenuated by {headroom_info['gain_db']:.2f} dB "
                            f"(auto headroom for perturbation)"
                        )
                    output_buffer, record = engine.execute(
                        step.action, working, dict(step.params), ectx
                    )
                    if not isinstance(output_buffer, AudioBuffer):
                        raise PipelineError(
                            f"Engine '{step.engine}' returned no audio buffer for '{step.action}'"
                        )
                    try:
                        true_peak = _true_peak(output_buffer.samples)
                    except Exception:
                        true_peak = None
                    if true_peak is not None and true_peak > _TRUE_PEAK_CEILING:
                        scale = _TRUE_PEAK_CEILING / true_peak
                        output_buffer = output_buffer.copy()
                        output_buffer.samples = np.ascontiguousarray(
                            output_buffer.samples * np.float32(scale), dtype=np.float32
                        )
                        output_buffer.notes.append(
                            f"true-peak safety: output attenuated by {20.0 * float(np.log10(scale)):.2f} dB"
                        )
                        warnings.append(
                            f"{step.engine}: final true peak exceeded 0 dBTP; output attenuated "
                            f"by {20.0 * float(np.log10(scale)):.2f} dB"
                        )
                    current_buffer = output_buffer
                    transforms_executed = True
                    executed.append(record)
                    verify_params = dict(step.params)
                    verify_params["_input_buffer"] = before
                    verification = engine.verify_effect(
                        step.action, current_buffer, verify_params, ectx
                    )
                    verified.append(verification)
                    if verification.get("ok"):
                        ctx.registry.mark_verified(step.engine)
                    elif step.required:
                        failed.append(
                            {
                                "engine": step.engine,
                                "action": step.action,
                                "error": "post-step verification failed: "
                                + str(verification.get("check")),
                                "required": True,
                                "code": "VERIFICATION_FAILED",
                            }
                        )
                        required_failed = True
                        break
                    else:
                        warnings.append(
                            f"Optional step '{step.engine}' ran but its verification failed "
                            f"({verification.get('check')}); the result is not declared protected "
                            "because of that step."
                        )
                elif step.action in _SIGN_ACTIONS:
                    if current_file is None:
                        assert current_buffer is not None
                        stage = work_dir / f"stage_{index}{ext}"
                        write_audio_atomic(
                            current_buffer,
                            stage,
                            subtype=choose_subtype(source_info, stage),
                            temp_dir=work_dir,
                        )
                        current_file = stage
                    output_file, record = engine.sign(
                        current_file, work_dir, dict(step.params), ectx
                    )
                    current_file = Path(output_file)
                    executed.append(record)
                    verify_params = dict(step.params)
                    verify_params["_signed_path"] = str(current_file)
                    verification = engine.verify_effect("sign", None, verify_params, ectx)
                    verified.append(verification)
                    if verification.get("ok"):
                        ctx.registry.mark_verified(step.engine)
                    elif step.required:
                        failed.append(
                            {
                                "engine": step.engine,
                                "action": step.action,
                                "error": "C2PA verification failed after signing",
                                "required": True,
                                "code": "VERIFICATION_FAILED",
                            }
                        )
                        required_failed = True
                        break
                elif step.action in _VERIFY_ACTIONS:
                    # Explicit verification step: does not transform the audio.
                    if current_file is not None and step.action == "verify" and engine_kind == "provenance":
                        verification = engine.verify_effect(
                            "sign", None, {"_signed_path": str(current_file)}, ectx
                        )
                    elif hasattr(engine, "detect") and current_buffer is not None:
                        detection = engine.detect(current_buffer, ectx)
                        verification = {
                            "check": f"{step.engine}.detect",
                            "ok": bool(detection.get("detected")),
                            "detail": detection,
                        }
                    else:
                        raise PipelineError(
                            f"Engine '{step.engine}' cannot verify in its current state"
                        )
                    verified.append(verification)
                    if not verification.get("ok") and step.required:
                        failed.append(
                            {
                                "engine": step.engine,
                                "action": step.action,
                                "error": "explicit verification failed",
                                "required": True,
                                "code": "VERIFICATION_FAILED",
                            }
                        )
                        required_failed = True
                        break
                else:  # pragma: no cover - prepare_chain rejects unknown actions
                    raise PipelineError(f"Unsupported action: {step.action}")
            except JobCancelled:
                cancelled = True
                break
            except EngineUnavailable as exc:
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": str(exc), "required": True,
                                   "code": "ENGINE_UNAVAILABLE"})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action, "reason": str(exc)})
                warnings.append(f"Optional step '{step.engine}' unavailable: {exc}")
            except NlvsError as exc:
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": str(exc), "required": True,
                                   "code": getattr(exc, "code", "ENGINE_EXECUTION_FAILED"),
                                   "detail": exc.detail})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action, "reason": str(exc)})
                warnings.append(f"Optional step '{step.engine}' failed: {exc}")
            except Exception as exc:  # defensive: never mask an engine crash
                message = f"{type(exc).__name__}: {exc}"
                if step.required:
                    failed.append({"engine": step.engine, "action": step.action,
                                   "error": message, "required": True,
                                   "code": "ENGINE_EXCEPTION"})
                    required_failed = True
                    break
                skipped.append({"engine": step.engine, "action": step.action, "reason": message})
                warnings.append(f"Optional step '{step.engine}' crashed: {message}")
            mapper.emit(step.engine, index, 100, f"{step.action}: done")

        # ------------------------------------------------------------ status
        if cancelled:
            status = "cancelled"
        elif required_failed:
            status = "partial" if transforms_executed else "failed"
        elif not executed:
            status = "failed"
            warnings.append("No step was executed: nothing was applied to the audio")
        else:
            status = "protected"
            # Every required step must have an executed record.
            executed_engines = [item.get("engine") for item in executed]
            for step in steps:
                if step.required and step.engine not in executed_engines:
                    status = "indeterminate"
                    warnings.append(
                        f"Required engine '{step.engine}' has no executed record; "
                        "the job cannot be declared protected."
                    )
                    break
        result["status"] = status

        # ------------------------------------------------------------ output
        output_path: Path | None = None
        output_partial_path: Path | None = None
        output_quality = None
        output_sha = None
        partial_sha = None
        keep_partial = bool(ctx.settings_public().get("keep_partial_outputs", True))

        def _materialize(target: Path) -> None:
            nonlocal current_file
            if current_file is not None and current_file.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                current_file.replace(target)
                current_file = None
                return
            if current_buffer is not None:
                write_audio_atomic(
                    current_buffer,
                    target,
                    subtype=choose_subtype(source_info, target),
                    temp_dir=work_dir,
                )
                return
            raise PipelineError("No audio state available to write an output file")

        if status == "protected":
            final = output_dir / f"{input_path.stem}_protected{ext}"
            if final.resolve() == input_path.resolve():
                raise PipelineError("Refusing to overwrite the input file with the protected output")
            _materialize(final)
            output_path = final
            output_sha = sha256_file(final)
        elif transforms_executed and (status in ("partial", "indeterminate", "failed", "cancelled")):
            if keep_partial:
                partial = output_dir / f"{input_path.stem}_PARTIAL_{short_id(job_id)}{ext}"
                _materialize(partial)
                output_partial_path = partial
                partial_sha = sha256_file(partial)
                warnings.append(
                    "PARTIAL output written for recovery only: it is NOT protected. "
                    "A required step failed or was not verified."
                )
            else:
                warnings.append(
                    "Partial output was not kept (settings.keep_partial_outputs=false)."
                )

        # ------------------------------------------------------------ quality
        try:
            if output_path is not None and current_buffer is not None:
                output_quality = analyze_quality(current_buffer, input_buffer)
            elif output_path is not None:
                loaded, _info = load_audio(output_path, temp_dir=work_dir)
                output_quality = analyze_quality(loaded, input_buffer)
            elif output_partial_path is not None and current_buffer is not None:
                output_quality = analyze_quality(current_buffer, input_buffer)
        except Exception as exc:
            warnings.append(f"Output quality analysis failed: {type(exc).__name__}: {exc}")

        # ------------------------------------------------------------ receipt
        receipt_id = None
        receipt = None
        if status in ("protected", "partial", "indeterminate") and executed:
            receipt = _build_receipt(
                ctx,
                job_id=job_id,
                status=status,
                input_path=input_path,
                source_info=source_info,
                input_sha=input_sha,
                output_path=output_path or output_partial_path,
                output_sha=output_sha or partial_sha,
                partial=output_path is None,
                payload=payload,
                executed=executed,
                verified=verified,
                options=options,
                warnings=warnings,
                input_buffer=input_buffer,
                current_buffer=current_buffer,
                work_dir=work_dir,
            )
            if receipt is not None:
                receipt_id = receipt.get("id")

        result.update(
            {
                "executed": executed,
                "verified": verified,
                "skipped": skipped,
                "failed": failed,
                "quality": {
                    "input": _quality_summary(input_quality),
                    "output": _quality_summary(output_quality),
                },
                "hashes": {
                    "input_sha256": input_sha,
                    "output_sha256": output_sha,
                    "output_partial_sha256": partial_sha,
                },
                "output_path": str(output_path) if output_path else None,
                "output_partial_path": str(output_partial_path) if output_partial_path else None,
                "receipt_id": receipt_id,
                "partial_output_marked": bool(output_partial_path),
            }
        )
        if payload is not None:
            result["watermark_payload_hex"] = f"{payload:04x}"
        result["finished_at"] = utc_now_iso()
        return result
    finally:
        safe_rmtree(work_dir, allowed_root=output_dir)


def _build_receipt(
    ctx: Any,
    *,
    job_id: str,
    status: str,
    input_path: Path,
    source_info: Any,
    input_sha: str | None,
    output_path: Path | None,
    output_sha: str | None,
    partial: bool,
    payload: int | None,
    executed: list[dict[str, Any]],
    verified: list[dict[str, Any]],
    options: dict[str, Any],
    warnings: list[str],
    input_buffer: AudioBuffer | None,
    current_buffer: AudioBuffer | None,
    work_dir: Path,
) -> dict[str, Any] | None:
    """Create and persist the signed receipt; failure here never fakes success."""
    watermark_record = next(
        (item for item in executed if item.get("action") == "embed"), None
    )
    c2pa_verification = next(
        (item for item in verified if item.get("check") == "c2pa.verify"), None
    )
    c2pa_record = next((item for item in executed if item.get("action") == "sign"), None)
    c2pa_info = None
    if c2pa_record is not None or c2pa_verification is not None:
        detail = (c2pa_verification or {}).get("detail") or {}
        record_detail = (c2pa_record or {}).get("detail") or {}
        c2pa_info = {
            "manifest_id": record_detail.get("manifest_id")
            or (c2pa_record or {}).get("params", {}).get("manifest_id"),
            "signer": detail.get("signer") or record_detail.get("signer"),
            "signature_valid": bool(detail.get("valid")),
            "trusted": bool(detail.get("trusted")),
            "tool_version": (c2pa_record or {}).get("version"),
            "note": (
                "Signature integrity and certificate trust are separate facts; a local self-signed "
                "certificate is not a public identity."
            ),
        }
    fingerprint = None
    fingerprint_engine = None
    try:
        fingerprint_engine = ctx.registry.get("fingerprint")
        source_buffer = current_buffer or input_buffer
        if source_buffer is not None:
            fingerprint = fingerprint_engine.compute(source_buffer)
    except Exception as exc:
        warnings.append(f"Fingerprint not stored in receipt: {type(exc).__name__}: {exc}")
    engine_chain = [
        {
            "engine": item.get("engine"),
            "action": item.get("action"),
            "version": item.get("version"),
        }
        for item in executed
    ]
    body: dict[str, Any] = {
        "status": status,
        "job_id": job_id,
        "input": {
            "path": str(input_path),
            "sha256": input_sha,
            "format": getattr(source_info, "format", None),
            "subtype": getattr(source_info, "subtype", None),
            "sample_rate": getattr(source_info, "sample_rate", None),
            "channels": getattr(source_info, "channels", None),
            "frames": getattr(source_info, "frames", None),
            "duration_seconds": getattr(source_info, "duration_seconds", None),
        },
        "output": {
            "path": str(output_path) if output_path else None,
            "sha256": output_sha,
            "partial": bool(partial),
        },
        "chain": engine_chain,
        "watermark": (
            {
                "engine": watermark_record.get("engine"),
                "payload_16bit": payload,
                "payload_hex": f"{payload:04x}" if payload is not None else None,
                "bits": 16,
                "note": (
                    "16-bit watermark payload; the receipt ID is the full identifier. "
                    "The payload alone is not a globally unique identity."
                ),
            }
            if watermark_record is not None and payload is not None
            else None
        ),
        "c2pa": c2pa_info,
        "fingerprint": fingerprint,
        "claims": {
            "author": options.get("author") or "",
            "usage_terms": options.get("usage_terms") or "",
            "copyright": options.get("copyright") or "",
        },
        "profile_id": options.get("profile_id"),
        "verified_checks": [
            {"check": item.get("check"), "ok": bool(item.get("ok"))} for item in verified
        ],
        "warnings": list(warnings),
        "partial_output": bool(partial),
    }
    try:
        receipt = ctx.receipts.create(body)
        return receipt
    except Exception as exc:
        warnings.append(f"Receipt could not be created: {type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------- batch
def run_batch(
    ctx: Any,
    *,
    job_id: str,
    inputs: list[str | Path],
    output_dir: str | Path,
    chain: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    preset: str | None = None,
    progress: Callable[[str, int, int | None, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the same fail-closed chain over several files."""
    started_at = utc_now_iso()
    cancel = cancel or threading.Event()
    output_dir = Path(output_dir).expanduser().resolve()
    if not inputs:
        raise PipelineError("Batch has no input files", code="BATCH_EMPTY")
    total = len(inputs)
    items: list[dict[str, Any]] = []
    for index, item in enumerate(inputs):
        if cancel.is_set():
            break

        def item_progress(step: str, step_index: int, pct: int | None, message: str,
                          _index=index) -> None:
            if progress is None:
                return
            fraction = 100.0 / total
            overall = int(_index * fraction + (pct if pct is not None else 0) * fraction / 100.0)
            try:
                progress(step, _index, max(0, min(99, overall)),
                         f"[{_index + 1}/{total}] {message}")
            except Exception:
                pass

        try:
            result = run_job(
                ctx,
                job_id=f"{job_id}-{index + 1}",
                input_path=item,
                output_dir=output_dir,
                chain=chain,
                options=options,
                preset=preset,
                progress=item_progress,
                cancel=cancel,
            )
        except NlvsError as exc:
            result = _result_skeleton(f"{job_id}-{index + 1}", utc_now_iso())
            result.update(
                {
                    "status": "failed",
                    "input_path": str(item),
                    "failed": [{"engine": "pipeline", "error": str(exc), "required": True,
                                "code": getattr(exc, "code", "PIPELINE_ERROR")}],
                    "finished_at": utc_now_iso(),
                }
            )
        items.append(result)
        if cancel.is_set():
            break

    statuses = [item.get("status") for item in items]
    if cancel.is_set():
        aggregate = "cancelled"
    elif statuses and all(status == "protected" for status in statuses) and len(items) == total:
        aggregate = "protected"
    elif any(status == "protected" for status in statuses):
        aggregate = "partial"
    elif statuses and all(status in ("failed",) for status in statuses):
        aggregate = "failed"
    else:
        aggregate = "partial" if items else "failed"
    counts: dict[str, int] = {}
    for status in statuses:
        counts[str(status)] = counts.get(str(status), 0) + 1
    return {
        "job_id": job_id,
        "kind": "batch",
        "status": aggregate,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "output_dir": str(output_dir),
        "items": items,
        "counts": {**counts, "total": total, "processed": len(items)},
        "requested": items[0].get("requested") if items else [],
        "software": {"name": "NL VO Shield", "version": __version__},
    }
