"""File verification: watermark, C2PA, fingerprint and local receipt.

Outcome semantics:
* ``verified``   — at least one independent positive check plus a valid receipt or
                   valid provenance; integrity claims hold;
* ``partial``    — a positive signal exists but attribution is incomplete
                   (e.g. watermark found, receipt missing) or C2PA integrity is
                   valid but the signer is not trusted;
* ``not_detected`` — every check ran and nothing was found;
* ``indeterminate`` — C2PA manifest present but invalid/tampered, or the audio
                   could not be read well enough to conclude.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .audio_io import AudioBuffer, load_audio
from .errors import NlvsError, PipelineError
from .util import json_safe, sha256_file, utc_now_iso


def _load_receipts(ctx: Any, receipts_dir: str | Path | None) -> list[dict[str, Any]]:
    if receipts_dir is None:
        return ctx.receipts.list(limit=2000, include_body=True)
    directory = Path(receipts_dir).expanduser()
    if not directory.is_dir():
        raise PipelineError(f"Receipts directory not found: {directory}")
    receipts: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            receipts.append(data)
    return receipts


def verify_file(
    ctx: Any,
    path: str | Path,
    *,
    receipts_dir: str | Path | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise PipelineError(
            f"File not found: {path}", code="INPUT_NOT_FOUND", detail={"path": str(path)}
        )
    warnings: list[str] = []
    digest = sha256_file(path)
    buffer: AudioBuffer | None = None
    audio_info: dict[str, Any] | None = None
    load_error: str | None = None
    try:
        buffer, file_info = load_audio(path)
        audio_info = file_info.to_dict()
    except NlvsError as exc:
        load_error = str(exc)
        warnings.append(f"Audio could not be decoded for watermark/quality checks: {exc}")

    ectx = ctx.engine_context(cancel=cancel or threading.Event())
    engines_status: dict[str, Any] = {}

    # ------------------------------------------------------------------ watermark
    watermark_checks: list[dict[str, Any]] = []
    for engine_id in ("audioseal", "watermark_dsp"):
        engine = ctx.registry.get(engine_id)
        available, reason = engine.availability()
        engines_status[engine_id] = {"available": available, "reason": reason}
        if not available:
            watermark_checks.append(
                {"engine": engine_id, "available": False, "detected": False, "reason": reason}
            )
            continue
        if buffer is None:
            watermark_checks.append(
                {"engine": engine_id, "available": True, "detected": False,
                 "reason": "audio not decodable"}
            )
            continue
        try:
            if hasattr(engine, "detect"):
                detection = engine.detect(buffer, ectx)
            else:  # pragma: no cover - both watermark engines implement detect
                detection = {"detected": False, "reason": "detector not implemented"}
        except Exception as exc:
            detection = {"detected": False, "reason": f"{type(exc).__name__}: {exc}"}
        detection = json_safe(detection)
        detection["engine"] = engine_id
        watermark_checks.append(detection)

    detected_checks = [item for item in watermark_checks if item.get("detected")]
    best = max(detected_checks, key=lambda item: float(item.get("confidence") or 0.0), default=None)
    watermark = {
        "detected": bool(best),
        "confidence": float(best.get("confidence") or 0.0) if best else 0.0,
        "payload_hex": best.get("payload_hex") if best else None,
        "engine": best.get("engine") if best else None,
        "checks": watermark_checks,
    }

    # ------------------------------------------------------------------ C2PA
    c2pa_engine = ctx.registry.get("c2pa")
    c2pa_available, c2pa_reason = c2pa_engine.availability()
    engines_status["c2pa"] = {"available": c2pa_available, "reason": c2pa_reason}
    if c2pa_available:
        try:
            c2pa = c2pa_engine.verify(path)
        except NlvsError as exc:
            c2pa = {"present": False, "valid": False, "reason": str(exc)}
        except Exception as exc:
            c2pa = {"present": False, "valid": False, "reason": f"{type(exc).__name__}: {exc}"}
    else:
        c2pa = {"present": False, "valid": False, "reason": c2pa_reason}
    c2pa = json_safe(c2pa)

    # ------------------------------------------------------------------ fingerprint / receipts
    receipts = _load_receipts(ctx, receipts_dir)
    fingerprint_result: dict[str, Any] = {
        "matched": False,
        "receipt_id": None,
        "similarity": None,
        "threshold": None,
    }
    exact_receipt = next(
        (
            item
            for item in receipts
            if ((item.get("output") or {}).get("sha256")) == digest
        ),
        None,
    )
    if buffer is not None and receipts:
        try:
            fingerprint_engine = ctx.registry.get("fingerprint")
            fingerprint_result = fingerprint_engine.find_best(buffer, receipts)
        except Exception as exc:
            warnings.append(f"Fingerprint matching failed: {type(exc).__name__}: {exc}")

    receipt_entry: dict[str, Any] = {"found": False, "id": None, "valid": False}
    candidate = exact_receipt
    match_source = "output_sha256" if exact_receipt is not None else None
    if candidate is None and fingerprint_result.get("receipt_id"):
        candidate = next(
            (item for item in receipts if item.get("id") == fingerprint_result["receipt_id"]),
            None,
        )
        match_source = "fingerprint" if candidate is not None else None
    if candidate is not None:
        verification = ctx.receipts.verify(candidate)
        receipt_entry = {
            "found": True,
            "id": candidate.get("id"),
            "valid": bool(verification.get("valid")),
            "signature_valid": bool(verification.get("signature_valid")),
            "key_trusted": bool(verification.get("key_trusted")),
            "reason": verification.get("reason"),
            "match_source": match_source,
            "path": (candidate.get("output") or {}).get("path"),
            "watermark_payload_hex": (candidate.get("watermark") or {}).get("payload_hex"),
            "created_at": candidate.get("created_at"),
        }

    # watermark payload vs receipt payload (consistency, not cryptography)
    payload_consistency = None
    if watermark.get("detected") and receipt_entry.get("watermark_payload_hex") and watermark.get("payload_hex"):
        payload_consistency = (
            str(watermark["payload_hex"]).lower()
            == str(receipt_entry["watermark_payload_hex"]).lower()
        )

    # ------------------------------------------------------------------ outcome
    positives = {
        "watermark": bool(watermark.get("detected")),
        "c2pa_valid": bool(c2pa.get("present") and c2pa.get("valid")),
        "c2pa_trusted": bool(c2pa.get("trusted")),
        "receipt_valid": bool(receipt_entry.get("valid")),
        "fingerprint_matched": bool(fingerprint_result.get("matched")),
    }
    c2pa_tampered = bool(c2pa.get("present") and not c2pa.get("valid"))
    exact_hash_match = exact_receipt is not None
    fingerprint_only_match = bool(
        positives["receipt_valid"] and positives["fingerprint_matched"] and not exact_hash_match
        and not positives["watermark"]
    )
    reason = ""
    if c2pa_tampered:
        outcome = "indeterminate"
        reason = (
            "A C2PA manifest is present but its signature/asset hash did not verify "
            "(tampered or invalid manifest)."
        )
    elif buffer is None and not positives["c2pa_valid"]:
        outcome = "indeterminate"
        reason = f"Audio could not be decoded and no valid provenance was found: {load_error}"
    elif positives["receipt_valid"] and positives["watermark"] and payload_consistency is not False:
        outcome = "verified"
        reason = (
            "Valid local receipt matched the file, a watermark was detected and its payload is "
            "consistent with the receipt."
        )
    elif positives["receipt_valid"] and exact_hash_match:
        outcome = "verified"
        reason = "The file's SHA-256 matches a locally signed receipt exactly."
    elif positives["c2pa_valid"] and positives["c2pa_trusted"]:
        outcome = "verified"
        reason = "C2PA signature and asset hash are valid and the certificate chain is trusted."
    elif positives["c2pa_valid"] and (positives["watermark"] or positives["receipt_valid"]):
        outcome = "verified"
        reason = "C2PA integrity is valid and an independent watermark/receipt confirms attribution."
    elif fingerprint_only_match:
        outcome = "partial"
        reason = (
            "The audio resembles a locally protected file, but no watermark was detected and the "
            "bytes do not match the receipt: this is likely the un-watermarked source or a "
            "re-encoded derivative, not a confirmed protected output."
        )
    elif positives["c2pa_valid"]:
        outcome = "partial"
        reason = (
            "C2PA signature integrity is valid, but the signing certificate is not trusted by a "
            "configured anchor and no local watermark/receipt confirmed attribution."
        )
    elif positives["watermark"] or positives["fingerprint_matched"]:
        outcome = "partial"
        reason = (
            "A watermark/fingerprint signal was found but no valid signed receipt confirms it."
        )
    else:
        outcome = "not_detected"
        reason = "No watermark, valid C2PA manifest, fingerprint match or local receipt was found."

    details = {
        "engines": engines_status,
        "watermark_checks": watermark_checks,
        "c2pa_available": c2pa_available,
        "payload_consistency": payload_consistency,
        "receipt_match_source": match_source,
        "audio": audio_info,
        "audio_load_error": load_error,
        "fingerprint": fingerprint_result,
        "explanations": reason,
        "checked_at": utc_now_iso(),
    }
    return json_safe(
        {
            "path": str(path),
            "sha256": digest,
            "watermark": {
                "detected": watermark["detected"],
                "confidence": watermark["confidence"],
                "payload_hex": watermark["payload_hex"],
                "engine": watermark["engine"],
                "checks": watermark["checks"],
            },
            "c2pa": {
                "present": bool(c2pa.get("present")),
                "valid": bool(c2pa.get("valid")),
                "signer": c2pa.get("signer"),
                "trusted": bool(c2pa.get("trusted")),
                "claims": c2pa.get("claims") or {},
                "manifest_id": c2pa.get("manifest_id"),
                "signature_valid": c2pa.get("signature_valid"),
                "data_hash_valid": c2pa.get("data_hash_valid"),
                "tampered": c2pa.get("tampered"),
                "reason": c2pa.get("reason") if not c2pa.get("present") else None,
                "trust": c2pa.get("trust"),
            },
            "fingerprint": fingerprint_result,
            "receipt": receipt_entry,
            "outcome": outcome,
            "details": details,
            "warnings": warnings,
        }
    )
