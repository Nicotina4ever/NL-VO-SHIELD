"""Real AudioSeal adapter: resample, embed, detect, payload, sample-rate fidelity."""

from __future__ import annotations

from pathlib import Path

import pytest

from nl_vo_shield.audio_io import load_audio, write_audio_atomic

from .conftest import audioseal_available, write_tone


def _engine(ctx):
    engine = ctx.registry.get("audioseal")
    if not audioseal_available(ctx):
        pytest.skip("AudioSeal weights/torch not available in this data dir")
    return engine


def test_audio_seal_requires_explicit_download_when_missing(ctx, tmp_path: Path) -> None:
    # A fresh data dir has no weights: the engine must be unavailable with a
    # download instruction, never fetch silently.
    from nl_vo_shield.context import AppContext

    fresh = AppContext(tmp_path / "fresh-data")
    engine = fresh.registry.get("audioseal")
    available, reason = engine.availability()
    if available:
        pytest.skip("weights are shared/cached in this environment")
    assert "download" in reason.lower() or "weights" in reason.lower()
    info = engine.info()
    assert info.status == "unavailable"
    assert info.requires_download is True


def test_embed_detect_payload_roundtrip(ctx, tone_wav: Path) -> None:
    engine = _engine(ctx)
    buffer, _info = load_audio(tone_wav)  # 24 kHz mono synthetic tone
    payload = 0x1234
    ectx = ctx.engine_context(payload=payload)
    output, record = engine.execute("embed", buffer, {}, ectx)
    assert output.sample_rate == buffer.sample_rate
    assert output.channels == buffer.channels
    assert output.frames == buffer.frames
    assert record["detail"]["resampled_for_processing"] is True
    assert record["params"]["payload_16bit"] == payload
    verification = engine.verify_effect("embed", output, {}, ectx)
    assert verification["ok"] is True
    detail = verification["detail"]
    assert detail["detected"] is True
    assert detail["payload_16bit"] == payload
    assert detail["payload_match"] is True
    assert detail["confidence"] >= 0.5


def test_audio_seal_survives_pcm16_roundtrip(ctx, tone_wav: Path, tmp_path: Path) -> None:
    engine = _engine(ctx)
    buffer, _info = load_audio(tone_wav)
    payload = 0x0F0F
    ectx = ctx.engine_context(payload=payload)
    output, _record = engine.execute("embed", buffer, {}, ectx)
    target = tmp_path / "sealed.wav"
    write_audio_atomic(output, target, subtype="PCM_16")
    reloaded, reloaded_info = load_audio(target)
    assert reloaded_info.sample_rate == buffer.sample_rate
    detection = engine.detect(reloaded, ctx.engine_context(payload=payload))
    assert detection["detected"] is True
    assert detection["payload_16bit"] == payload


def test_audio_seal_stereo_keeps_layout(ctx, tmp_path: Path) -> None:
    engine = _engine(ctx)
    source = write_tone(tmp_path / "stereo.wav", sample_rate=44100, channels=2,
                        seconds=1.0, subtype="PCM_16")
    buffer, _info = load_audio(source)
    payload = 0x00AA
    ectx = ctx.engine_context(payload=payload)
    output, record = engine.execute("embed", buffer, {}, ectx)
    assert output.channels == 2
    assert output.sample_rate == 44100
    assert output.frames == buffer.frames
    assert record["detail"]["channels"] == 2
    detection = engine.detect(output, ctx.engine_context(payload=payload))
    assert detection["detected"] is True
    assert detection["payload_16bit"] == payload


def test_audio_seal_rejects_no_headroom(ctx) -> None:
    engine = _engine(ctx)
    from nl_vo_shield.audio_io import AudioBuffer

    import numpy as np

    # Full-scale square-ish signal: adding a watermark without clipping is
    # impossible, so the engine must refuse instead of silently clipping.
    samples = np.ones((24000, 1), dtype=np.float32) * 0.9999
    buffer = AudioBuffer(samples, 24000)
    with pytest.raises(Exception):
        engine.execute("embed", buffer, {}, ctx.engine_context(payload=1))
