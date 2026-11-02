"""Pipeline + verification round-trip with real engines (no mocks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield import pipeline
from nl_vo_shield.util import full_id
from nl_vo_shield.verify import verify_file

from .conftest import audioseal_available, c2pa_available, write_tone


def _chain(ctx, *, with_c2pa: bool = True) -> list[dict]:
    if c2pa_available(ctx):
        return [
            {"engine": "watermark_dsp", "action": "embed", "required": True},
            {"engine": "c2pa", "action": "sign", "required": True},
        ]
    return [{"engine": "watermark_dsp", "action": "embed", "required": True}]


def _protect(ctx, source: Path, output_dir: Path, chain: list[dict]) -> dict:
    return pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path=source,
        output_dir=output_dir,
        chain=chain,
    )


def test_protect_and_verify_roundtrip(ctx, tone_wav: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    result = _protect(ctx, tone_wav, output_dir, _chain(ctx))
    assert result["status"] == "protected"
    assert result["output_path"] is not None
    output = Path(result["output_path"])
    assert output.is_file() and output.name.endswith("_protected.wav")
    assert result["hashes"]["input_sha256"] and result["hashes"]["output_sha256"]
    assert result["hashes"]["input_sha256"] != result["hashes"]["output_sha256"]
    assert result["receipt_id"] is not None
    assert result["executed"] and result["verified"]
    assert all(item["ok"] for item in result["verified"])
    assert result["quality"]["input"] is not None
    assert result["quality"]["output"] is not None

    verification = verify_file(ctx, output)
    assert verification["outcome"] == "verified"
    assert verification["watermark"]["detected"] is True
    assert verification["receipt"]["found"] is True
    assert verification["receipt"]["valid"] is True
    assert verification["path"] == str(output)


def test_verify_source_is_not_reported_protected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    _protect(ctx, tone_wav, output_dir, _chain(ctx))
    verification = verify_file(ctx, tone_wav)
    assert verification["outcome"] != "verified"
    assert verification["watermark"]["detected"] is False


def test_verify_unknown_file_is_not_detected(ctx, tmp_path: Path) -> None:
    other = write_tone(tmp_path / "other.wav", frequency=880.0)
    verification = verify_file(ctx, other)
    assert verification["outcome"] == "not_detected"
    assert verification["receipt"]["found"] is False


def test_recording_receipt_is_signed(ctx, tone_wav: Path, tmp_path: Path) -> None:
    result = _protect(ctx, tone_wav, tmp_path / "out", _chain(ctx))
    receipt = ctx.receipts.get(result["receipt_id"])
    assert receipt is not None
    verification = ctx.receipts.verify(receipt)
    assert verification["valid"] is True
    assert receipt["input"]["sha256"] == result["hashes"]["input_sha256"]
    assert receipt["output"]["sha256"] == result["hashes"]["output_sha256"]
    assert receipt["watermark"]["payload_16bit"] is not None
    assert len(receipt["watermark"]["payload_hex"]) == 4
    assert receipt["status"] == "protected"


def test_chain_order_forces_c2pa_last(ctx, tone_wav: Path, tmp_path: Path) -> None:
    if not c2pa_available(ctx):
        pytest.skip("c2patool not available")
    chain = [
        {"engine": "c2pa", "action": "sign", "required": True},
        {"engine": "watermark_dsp", "action": "embed", "required": True},
    ]
    result = _protect(ctx, tone_wav, tmp_path / "out", chain)
    assert result["status"] == "protected"
    executed = [item["action"] for item in result["executed"]]
    assert executed[-1] == "sign"
    assert any("moved to the end" in warning for warning in result["warnings"])


def test_stereo_and_48k_preserved(ctx, tmp_path: Path) -> None:
    source = write_tone(tmp_path / "stereo48k.wav", sample_rate=48000, channels=2,
                        subtype="PCM_24", seconds=1.0)
    result = _protect(ctx, source, tmp_path / "out", _chain(ctx))
    assert result["status"] == "protected"
    output = Path(result["output_path"])
    from nl_vo_shield.audio_io import load_audio

    buffer, info = load_audio(output)
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert buffer.frames == 48000


def test_batch_runs_each_file(ctx, tmp_path: Path) -> None:
    inputs = [
        write_tone(tmp_path / "a.wav", frequency=220.0),
        write_tone(tmp_path / "b.wav", frequency=440.0),
    ]
    batch = pipeline.run_batch(
        ctx,
        job_id=full_id(),
        inputs=inputs,
        output_dir=tmp_path / "out",
        chain=_chain(ctx),
    )
    assert batch["status"] == "protected"
    assert len(batch["items"]) == 2
    assert batch["counts"]["total"] == 2
    assert all(item["status"] == "protected" for item in batch["items"])
    assert len(list((tmp_path / "out").glob("*_protected.wav"))) == 2


def test_quality_engine_metrics(ctx, tone_wav: Path) -> None:
    from nl_vo_shield.audio_io import load_audio

    buffer, _info = load_audio(tone_wav)
    metrics = ctx.registry.get("quality").analyze(buffer)
    assert metrics["sample_rate"] == 24000
    assert metrics["duration_seconds"] > 1.0


def test_doctor_report_shape(ctx) -> None:
    from nl_vo_shield.doctor import run_doctor

    report = run_doctor(ctx)
    assert report["schema"] == "nlvs.doctor/1"
    ids = [check["id"] for check in report["checks"]]
    assert "python.version" in ids
    assert "models.inventory" in ids
    assert "tool.c2patool" in ids
    assert report["summary"]["status"] in ("ok", "warn", "fail")
    assert report["paths"]["models_dir"]
    assert report["paths"]["cache_dir"]


def test_acceptance_audio_roundtrip_if_available(ctx, tmp_path: Path) -> None:
    """Real-file smoke test on the authorized workspace WAV, when present."""
    from .conftest import workspace_test_audio

    source = workspace_test_audio()
    if source is None:
        pytest.skip("authorized workspace test audio not available")
    chain = _chain(ctx)
    if audioseal_available(ctx):
        chain = [{"engine": "audioseal", "action": "embed", "required": True}] + [
            step for step in chain if step["engine"] != "watermark_dsp"
        ]
    result = _protect(ctx, source, tmp_path / "acceptance", chain)
    assert result["status"] == "protected", result.get("failed")
    assert result["output_path"] is not None
    verification = verify_file(ctx, result["output_path"])
    assert verification["outcome"] == "verified"
    assert verification["watermark"]["detected"] is True
