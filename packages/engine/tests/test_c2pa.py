"""Real C2PA wrapping of c2patool 0.9.12: sign, verify, tamper detection, trust."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import c2pa_available


def _engine(ctx):
    engine = ctx.registry.get("c2pa")
    if not c2pa_available(ctx):
        pytest.skip("c2patool not available in this environment")
    return engine


def test_identity_is_generated_inside_data_dir(ctx) -> None:
    engine = _engine(ctx)
    identity = engine.ensure_identity()
    assert identity["alg"] in ("es256", "ps256")
    c2pa_dir = Path(ctx.data_dir) / "c2pa"
    for name in ("local-root-cert.pem", "signing-key.pem", "signing-cert.pem", "identity.json"):
        assert (c2pa_dir / name).is_file(), name
        assert (c2pa_dir / name).is_relative_to(ctx.data_dir)
    chain = (c2pa_dir / "signing-cert.pem").read_text(encoding="utf-8")
    assert chain.count("BEGIN CERTIFICATE") == 2  # leaf + local root


def test_c2pa_available_on_fresh_data_dir(tmp_path: Path) -> None:
    """C2PA must be selectable on a new install: availability depends on the tool,
    not on a previously generated signing identity."""
    from nl_vo_shield.context import AppContext

    fresh = AppContext(tmp_path / "fresh-data")
    engine = fresh.registry.get("c2pa")
    if fresh.c2patool is None:
        pytest.skip("c2patool not installed in this environment")
    available, reason = engine.availability()
    assert available is True, reason
    info = engine.info()
    assert info.status == "available"
    assert info.version not in (None, "", "unknown")
    assert info.path and Path(info.path).is_file()
    # No identity exists yet, and that must not make the engine unavailable.
    assert engine.identity_info() is None


def test_identity_generated_lazily_on_first_sign(tmp_path: Path, tone_wav: Path) -> None:
    from nl_vo_shield import pipeline
    from nl_vo_shield.context import AppContext
    from nl_vo_shield.util import full_id

    fresh = AppContext(tmp_path / "fresh-data")
    if not c2pa_available(fresh):
        pytest.skip("c2patool not available")
    assert fresh.registry.get("c2pa").identity_info() is None
    result = pipeline.run_job(
        fresh,
        job_id=full_id(),
        input_path=tone_wav,
        output_dir=tmp_path / "fresh-out",
        chain=[{"engine": "c2pa", "action": "sign", "required": True}],
    )
    assert result["status"] == "protected", result.get("failed")
    assert result["output_path"]
    assert result["finished_at"]
    assert fresh.registry.get("c2pa").identity_info() is not None
    verification = fresh.registry.get("c2pa").verify(Path(result["output_path"]))
    assert verification["valid"] is True


def test_relative_input_and_output_paths_work(ctx, tmp_path: Path, monkeypatch) -> None:
    """External tools must receive absolute paths even when the CLI gets relative ones."""
    from nl_vo_shield import pipeline
    from nl_vo_shield.util import full_id

    from .conftest import write_tone

    if not c2pa_available(ctx):
        pytest.skip("c2patool not available")
    write_tone(tmp_path / "relative-tone.wav")
    monkeypatch.chdir(tmp_path)
    result = pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path="relative-tone.wav",
        output_dir="relative-out",
        chain=[
            {"engine": "watermark_dsp", "action": "embed", "required": True},
            {"engine": "c2pa", "action": "sign", "required": True},
        ],
    )
    assert result["status"] == "protected", result.get("failed")
    output = Path(result["output_path"])
    assert output.is_file() and output.is_absolute()
    verification = ctx.registry.get("c2pa").verify(output)
    assert verification["present"] is True
    assert verification["valid"] is True


def test_sign_and_verify_wav(ctx, tone_wav: Path, tmp_path: Path) -> None:
    engine = _engine(ctx)
    work = tmp_path / "work"
    work.mkdir()
    ectx = ctx.engine_context(work_dir=work)
    signed, record = engine.sign(tone_wav, work, {}, ectx)
    assert signed.is_file()
    assert record["params"]["manifest_id"].startswith("urn:uuid:")
    assert record["detail"]["cert_trusted"] is False  # honest: local self-signed
    result = engine.verify(signed)
    assert result["present"] is True
    assert result["signature_valid"] is True
    assert result["data_hash_valid"] is True
    assert result["valid"] is True
    assert result["tampered"] is False
    assert result["trusted"] is True  # local self-signed root is the local anchor
    assert result["trust"]["root_matched_local_anchor"] is True
    assert result["signer"]
    assert result["format"] == "audio/wav"
    assert result["claims"]["creative_work"]["@type"] == "CreativeWork"
    assert result["claims"]["protection"]["schema"] == "nlvs.protection/1"


def test_tampered_file_is_detected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    engine = _engine(ctx)
    work = tmp_path / "work"
    work.mkdir()
    ectx = ctx.engine_context(work_dir=work)
    signed, _record = engine.sign(tone_wav, work, {}, ectx)
    tampered = tmp_path / "tampered.wav"
    data = bytearray(signed.read_bytes())
    for offset in range(200, 260):
        data[offset] ^= 0xFF
    tampered.write_bytes(bytes(data))
    result = engine.verify(tampered)
    assert result["present"] is True
    assert result["valid"] is False
    assert result["tampered"] is True
    assert result["data_hash_valid"] is False


def test_unsigned_audio_has_no_manifest(ctx, tone_wav: Path) -> None:
    engine = _engine(ctx)
    result = engine.verify(tone_wav)
    assert result["present"] is False
    assert result["valid"] is False


def test_manifest_claims_are_user_controlled(ctx, tone_wav: Path, tmp_path: Path) -> None:
    engine = _engine(ctx)
    work = tmp_path / "work"
    work.mkdir()
    options = {
        "author": "Test Voice Actor",
        "usage_terms": "Internal review only",
        "copyright": "Test copyright",
    }
    ectx = ctx.engine_context(work_dir=work, options=options)
    signed, _record = engine.sign(tone_wav, work, {}, ectx)
    result = engine.verify(signed)
    creative = result["claims"]["creative_work"]
    assert creative["author"][0]["name"] == "Test Voice Actor"
    assert creative["usageTerms"] == "Internal review only"
    assert creative["copyrightNotice"] == "Test copyright"


def test_c2pa_inside_pipeline_marks_verified(ctx, tone_wav: Path, tmp_path: Path) -> None:
    from nl_vo_shield import pipeline
    from nl_vo_shield.util import full_id

    if not c2pa_available(ctx):
        pytest.skip("c2patool not available")
    result = pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path=tone_wav,
        output_dir=tmp_path / "out",
        chain=[
            {"engine": "watermark_dsp", "action": "embed", "required": True},
            {"engine": "c2pa", "action": "sign", "required": True},
        ],
    )
    assert result["status"] == "protected"
    c2pa_info = next(item for item in result["verified"] if item["check"] == "c2pa.verify")
    assert c2pa_info["ok"] is True
    assert c2pa_info["detail"]["valid"] is True
    # "verified" engine status only after a real successful run in this session.
    assert ctx.registry.get("c2pa").info().status == "verified"
    receipt = ctx.receipts.get(result["receipt_id"])
    assert receipt["c2pa"]["signature_valid"] is True
    assert receipt["c2pa"]["manifest_id"].startswith("urn:uuid:")
