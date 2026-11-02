"""OCRITICAL: fail-closed semantics.

A required step that fails or cannot run must never produce a file at the
"protected" output path. The legacy launcher copied ``*_PROTETTO.wav`` even
after a signature failure; these tests make that behaviour impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield import pipeline
from nl_vo_shield.engines.base import BaseEngine
from nl_vo_shield.errors import EngineExecutionError, EngineUnavailable
from nl_vo_shield.util import full_id

from .conftest import c2pa_available


class FailingEngine(BaseEngine):
    id = "failing_test"
    name = "Failing test engine"
    kind = "anti_clone"
    version = "test"
    capabilities = ["perturb"]
    requires: list[str] = []

    def __init__(self, context=None) -> None:
        self.context = context

    def execute(self, action, buffer, params, ctx):
        raise EngineExecutionError("simulated required engine failure")

    def verify_effect(self, action, buffer, params, ctx):
        return {"check": "failing_test.verify", "ok": False, "detail": {"simulated": True}}


def _run(ctx, tone_wav: Path, chain, output_dir: Path):
    return pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path=tone_wav,
        output_dir=output_dir,
        chain=chain,
    )


def _protected_files(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.is_file() and "_protected" in path.name]


def test_required_engine_failure_never_declares_protected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    ctx.registry.register(FailingEngine(ctx))
    output_dir = tmp_path / "out"
    chain = [
        {"engine": "watermark_dsp", "action": "embed", "required": True},
        {"engine": "failing_test", "action": "perturb", "required": True},
        {"engine": "c2pa", "action": "sign", "required": True},
    ]
    result = _run(ctx, tone_wav, chain, output_dir)
    assert result["status"] in ("partial", "failed")
    assert result["status"] != "protected"
    assert result["output_path"] is None
    assert _protected_files(output_dir) == []
    # The failure is explicit and attributed to the required engine.
    failed_engines = [item["engine"] for item in result["failed"]]
    assert "failing_test" in failed_engines
    assert any(item.get("required") for item in result["failed"])
    # A partial output exists for recovery and is clearly marked.
    assert result["output_partial_path"] is not None
    partial = Path(result["output_partial_path"])
    assert partial.is_file() and "_PARTIAL_" in partial.name
    assert any("PARTIAL" in warning for warning in result["warnings"])
    # C2PA was never executed after the required failure.
    executed = [item["engine"] for item in result["executed"]]
    assert "c2pa" not in executed


def test_missing_required_engine_fails_closed(ctx, tone_wav: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    chain = [{"engine": "engine_that_does_not_exist", "action": "embed", "required": True}]
    result = _run(ctx, tone_wav, chain, output_dir)
    assert result["status"] == "failed"
    assert result["output_path"] is None
    assert result["output_partial_path"] is None
    assert list(output_dir.rglob("*.wav")) == []
    assert result["failed"][0]["code"] == "ENGINE_NOT_FOUND"


def test_unavailable_required_engine_fails_closed(ctx, tone_wav: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    chain = [{"engine": "safespeech", "action": "perturb", "required": True}]
    result = _run(ctx, tone_wav, chain, output_dir)
    assert result["status"] == "failed"
    assert result["output_path"] is None
    assert _protected_files(output_dir) == []
    assert result["failed"][0]["code"] == "ENGINE_UNAVAILABLE"
    assert "SafeSpeech" in result["failed"][0]["error"]


def test_optional_engine_failure_is_skipped_not_protected_claim(
    ctx, tone_wav: Path, tmp_path: Path
) -> None:
    ctx.registry.register(FailingEngine(ctx))
    output_dir = tmp_path / "out"
    payload = [
        {"engine": "watermark_dsp", "action": "embed", "required": True},
        {"engine": "failing_test", "action": "perturb", "required": False},
    ]
    if c2pa_available(ctx):
        payload.append({"engine": "c2pa", "action": "sign", "required": True})
    result = _run(ctx, tone_wav, payload, output_dir)
    # An optional failure does not invalidate a valid required chain...
    assert result["status"] == "protected"
    assert any(item["engine"] == "failing_test" for item in result["skipped"])
    assert result["output_path"] is not None
    assert _protected_files(output_dir)


def test_c2pa_failure_after_watermark_yields_partial(
    ctx, tone_wav: Path, tmp_path: Path, monkeypatch
) -> None:
    if not c2pa_available(ctx):
        pytest.skip("c2patool not available")
    engine = ctx.registry.get("c2pa")

    def raise_signing(*_args, **_kwargs):
        raise EngineExecutionError("simulated C2PA signing failure")

    monkeypatch.setattr(engine, "sign", raise_signing)
    output_dir = tmp_path / "out"
    chain = [
        {"engine": "watermark_dsp", "action": "embed", "required": True},
        {"engine": "c2pa", "action": "sign", "required": True},
    ]
    result = _run(ctx, tone_wav, chain, output_dir)
    assert result["status"] == "partial"
    assert result["output_path"] is None
    assert _protected_files(output_dir) == []
    assert result["output_partial_path"] is not None
    assert Path(result["output_partial_path"]).is_file()
    # The watermark was applied and verified, but the job is not "protected".
    checks = [item["check"] for item in result["verified"]]
    assert "watermark_dsp.detect" in checks
    assert result["receipt_id"] is not None
    receipt = ctx.receipts.get(result["receipt_id"])
    assert receipt is not None
    assert receipt["status"] == "partial"
    assert receipt["partial_output"] is True


def test_cancelled_job_writes_no_protected_output(ctx, tone_wav: Path, tmp_path: Path) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()  # cancelled before it starts processing
    output_dir = tmp_path / "out"
    result = pipeline.run_job(
        ctx,
        job_id=full_id(),
        input_path=tone_wav,
        output_dir=output_dir,
        chain=[{"engine": "watermark_dsp", "action": "embed", "required": True}],
        cancel=cancel,
    )
    assert result["status"] == "cancelled"
    assert result["output_path"] is None
    assert _protected_files(output_dir) == []


def test_chain_without_steps_rejected(ctx, tone_wav: Path, tmp_path: Path) -> None:
    with pytest.raises(Exception):
        _run(ctx, tone_wav, [], tmp_path / "out")
