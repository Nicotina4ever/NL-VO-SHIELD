"""Spectral Cloak anti-clone DSP perturbation: real, seeded, bounded, honest."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nl_vo_shield.audio_io import load_audio
from nl_vo_shield.errors import EngineExecutionError
from nl_vo_shield.quality import analyze


def _run(ctx, buffer, *, strength: str = "medium", seed: int = 1234):
    engine = ctx.registry.get("spectral_cloak")
    ectx = ctx.engine_context()
    output, record = engine.execute(
        "perturb", buffer, {"strength": strength, "seed": seed}, ectx
    )
    verification = engine.verify_effect(
        "perturb", output, {"strength": strength, "seed": seed, "_input_buffer": buffer}, ectx
    )
    return output, record, verification


def test_perturbation_is_real_and_verified(ctx, tone_wav: Path) -> None:
    buffer, _info = load_audio(tone_wav)
    output, record, verification = _run(ctx, buffer)
    assert verification["ok"] is True
    assert verification["detail"]["changed"] is True
    assert 12.0 <= verification["detail"]["snr_db"] <= 60.0
    assert record["detail"]["efficacy_validated"] is False
    assert float(np.max(np.abs(output.samples))) <= 1.0
    assert not np.array_equal(output.samples, buffer.samples)


def test_seed_is_deterministic(ctx, tone_wav: Path) -> None:
    buffer, _info = load_audio(tone_wav)
    first, _, _ = _run(ctx, buffer, seed=42)
    second, _, _ = _run(ctx, buffer, seed=42)
    assert np.array_equal(first.samples, second.samples)


def test_different_seeds_produce_different_audio(ctx, tone_wav: Path) -> None:
    buffer, _info = load_audio(tone_wav)
    first, _, _ = _run(ctx, buffer, seed=1)
    second, _, _ = _run(ctx, buffer, seed=2)
    assert not np.array_equal(first.samples, second.samples)


def test_strength_controls_distortion(ctx, tone_wav: Path) -> None:
    buffer, _info = load_audio(tone_wav)
    low_output, low_record, _ = _run(ctx, buffer, strength="low")
    high_output, high_record, _ = _run(ctx, buffer, strength="high")
    low_snr = analyze(low_output, buffer)["reference"]["snr_db"]
    high_snr = analyze(high_output, buffer)["reference"]["snr_db"]
    assert low_snr is not None and high_snr is not None
    assert low_snr > high_snr
    assert low_record["params"]["strength"] == "low"
    assert high_record["params"]["strength"] == "high"


def test_unknown_strength_rejected(ctx, tone_wav: Path) -> None:
    engine = ctx.registry.get("spectral_cloak")
    buffer, _info = load_audio(tone_wav)
    with pytest.raises(EngineExecutionError):
        engine.execute("perturb", buffer, {"strength": "impossible"}, ctx.engine_context())


def test_digital_silence_cannot_be_perturbed(ctx) -> None:
    from nl_vo_shield.audio_io import AudioBuffer

    engine = ctx.registry.get("spectral_cloak")
    silence = AudioBuffer(np.zeros((24000, 1), dtype=np.float32), 24000)
    output, _record = engine.execute(
        "perturb", silence, {"strength": "medium", "seed": 1}, ctx.engine_context()
    )
    verification = engine.verify_effect(
        "perturb", output, {"strength": "medium", "seed": 1, "_input_buffer": silence},
        ctx.engine_context(),
    )
    # Nothing to mask in digital silence: the step must fail closed, not pass.
    assert verification["ok"] is False
    assert verification["detail"]["changed"] is False
