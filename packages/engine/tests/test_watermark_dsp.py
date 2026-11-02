"""Genuine DSP spread-spectrum watermark: embed, detect, round-trip, honest misses."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nl_vo_shield.audio_io import load_audio, write_audio_atomic
from nl_vo_shield.errors import EngineExecutionError


def test_embed_and_detect_roundtrip(ctx, tone_wav: Path) -> None:
    engine = ctx.registry.get("watermark_dsp")
    buffer, _info = load_audio(tone_wav)
    payload = 0x1A2B
    ectx = ctx.engine_context(payload=payload)
    output, record = engine.execute("embed", buffer, {}, ectx)
    assert output.frames == buffer.frames
    assert record["params"]["payload_16bit"] == payload
    assert record["detail"]["measured_snr_db"] is not None
    verification = engine.verify_effect("embed", output, {}, ectx)
    assert verification["ok"] is True
    detection = verification["detail"]
    assert detection["detected"] is True
    assert detection["payload_16bit"] == payload
    assert detection["mean_z"] >= 3.0


def test_detection_survives_pcm16_file_roundtrip(ctx, tone_wav: Path, tmp_path: Path) -> None:
    engine = ctx.registry.get("watermark_dsp")
    buffer, info = load_audio(tone_wav)
    payload = 0xBEEF
    ectx = ctx.engine_context(payload=payload)
    output, _record = engine.execute("embed", buffer, {}, ectx)
    target = tmp_path / "wm.wav"
    write_audio_atomic(output, target, subtype="PCM_16")
    reloaded, _ = load_audio(target)
    detection = engine.detect(reloaded, ctx.engine_context(payload=payload))
    assert detection["detected"] is True
    assert detection["payload_16bit"] == payload


def test_clean_audio_is_not_detected(ctx, tone_wav: Path) -> None:
    engine = ctx.registry.get("watermark_dsp")
    buffer, _info = load_audio(tone_wav)
    detection = engine.detect(buffer, ctx.engine_context(payload=0x1234))
    assert detection["detected"] is False
    assert detection["payload_16bit"] is None


def test_short_audio_is_rejected(ctx) -> None:
    from nl_vo_shield.audio_io import AudioBuffer

    engine = ctx.registry.get("watermark_dsp")
    tiny = AudioBuffer(np.zeros((400, 1), dtype=np.float32), 24000)
    with pytest.raises(EngineExecutionError):
        engine.execute("embed", tiny, {}, ctx.engine_context(payload=1))


def test_payload_mismatch_fails_verification(ctx, tone_wav: Path) -> None:
    engine = ctx.registry.get("watermark_dsp")
    buffer, _info = load_audio(tone_wav)
    ectx = ctx.engine_context(payload=0x1111)
    output, _record = engine.execute("embed", buffer, {}, ectx)
    # Verification expects a different payload: must fail (no false success).
    wrong_ctx = ctx.engine_context(payload=0x2222)
    verification = engine.verify_effect("embed", output, {}, wrong_ctx)
    assert verification["ok"] is False
    assert verification["detail"]["payload_match"] is False


def test_silence_watermark_is_detected_without_clipping(ctx) -> None:
    from nl_vo_shield.audio_io import AudioBuffer

    engine = ctx.registry.get("watermark_dsp")
    silence = AudioBuffer(np.zeros((44100, 1), dtype=np.float32), 44100)
    payload = 0x0F0F
    ectx = ctx.engine_context(payload=payload)
    output, record = engine.execute("embed", silence, {}, ectx)
    assert float(np.max(np.abs(output.samples))) < 0.01  # inaudible on silence
    verification = engine.verify_effect("embed", output, {}, ectx)
    assert verification["ok"] is True
