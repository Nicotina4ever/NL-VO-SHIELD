"""Engine registry: honest EngineInfo statuses, hints, fingerprint matching."""

from __future__ import annotations

import numpy as np

from nl_vo_shield.audio_io import AudioBuffer, load_audio

ENGINE_INFO_KEYS = {
    "id", "name", "version", "kind", "capabilities", "supported_os",
    "status", "requires", "requires_download", "reason", "path", "quality_note",
}
ENGINE_INFO_EXTRA_KEYS = {
    "description", "impact", "limitations", "recommended_levels", "incompatible_with",
}


def test_engine_infos_contract(ctx) -> None:
    infos = ctx.engine_infos()
    ids = {info["id"] for info in infos}
    assert {"audioseal", "watermark_dsp", "spectral_cloak", "c2pa",
            "fingerprint", "quality", "safespeech"} <= ids
    for info in infos:
        assert ENGINE_INFO_KEYS <= set(info), info["id"]
        assert ENGINE_INFO_EXTRA_KEYS <= set(info), info["id"]
        assert info["status"] in ("available", "unavailable", "experimental", "verified")
        assert isinstance(info["capabilities"], list)
        assert isinstance(info["limitations"], list)
        assert isinstance(info["recommended_levels"], list)
        assert info["description"]
        if info["status"] == "unavailable":
            assert info["reason"], info["id"]


def test_status_semantics(ctx) -> None:
    by_id = {info["id"]: info for info in ctx.engine_infos()}
    assert by_id["spectral_cloak"]["status"] == "experimental"
    assert by_id["safespeech"]["status"] == "unavailable"
    assert "incomplete" in (by_id["safespeech"]["reason"] or "").lower() or \
           "not found" in (by_id["safespeech"]["reason"] or "").lower()
    assert by_id["watermark_dsp"]["status"] in ("available", "verified")
    assert by_id["fingerprint"]["status"] in ("available", "verified")


def test_audioseal_reports_download_requirement_honestly(ctx) -> None:
    info = {item["id"]: item for item in ctx.engine_infos()}["audioseal"]
    if info["status"] == "unavailable" and info["requires_download"]:
        assert "download" in (info["reason"] or "").lower() or "weights" in (info["reason"] or "").lower()
    hint = ctx.install_hint("audioseal")
    assert hint["no_automatic_download"] is True
    assert hint["instructions"]
    assert any(model.startswith("audioseal") for model in hint["download_models"])


def test_install_hints_for_each_engine(ctx) -> None:
    for info in ctx.engine_infos():
        hint = ctx.install_hint(info["id"])
        assert hint["engine_id"] == info["id"]
        assert isinstance(hint["instructions"], str) and hint["instructions"]
        assert hint["no_automatic_download"] is True


def test_fingerprint_similarity(ctx, tone_wav) -> None:
    engine = ctx.registry.get("fingerprint")
    buffer, _info = load_audio(tone_wav)
    first = engine.compute(buffer)
    second = engine.compute(buffer)
    assert first["algorithm"] == "nlvs-spectral-fp/1"
    assert len(first["vector"]) == first["dimensions"] == 64
    assert engine.similarity(first["vector"], second["vector"]) > 0.9999

    # A different spectral shape must be clearly less similar.
    frames = buffer.frames
    time_axis = np.arange(frames) / buffer.sample_rate
    other = AudioBuffer(
        (0.3 * np.sin(2 * np.pi * 1800 * time_axis)).astype(np.float32)[:, None],
        buffer.sample_rate,
    )
    second_profile = engine.compute(other)
    similarity = engine.similarity(first["vector"], second_profile["vector"])
    assert similarity < 0.99


def test_mark_verified_after_real_execution(ctx, tone_wav) -> None:
    engine = ctx.registry.get("watermark_dsp")
    assert engine.info().status in ("available", "verified")
    engine.mark_verified()
    assert engine.info().status == "verified"


def test_unknown_engine_raises(ctx) -> None:
    import pytest

    from nl_vo_shield.errors import EngineNotFound

    with pytest.raises(EngineNotFound):
        ctx.registry.get("nope")
