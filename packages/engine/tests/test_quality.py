"""Objective quality metrics (measurements only, no protection claims)."""

from __future__ import annotations

import numpy as np

from nl_vo_shield.audio_io import AudioBuffer, load_audio
from nl_vo_shield.quality import analyze


def _tone_buffer(sample_rate: int = 24000, seconds: float = 1.2, frequency: float = 220.0,
                 amplitude: float = 0.3) -> AudioBuffer:
    frames = int(sample_rate * seconds)
    time_axis = np.arange(frames) / sample_rate
    samples = (amplitude * np.sin(2 * np.pi * frequency * time_axis)).astype(np.float32)
    return AudioBuffer(samples[:, None], sample_rate)


def test_tone_metrics() -> None:
    metrics = analyze(_tone_buffer())
    assert metrics["sample_rate"] == 24000
    assert metrics["channels"] == 1
    assert metrics["peak_dbfs"] is not None
    assert abs(metrics["peak_dbfs"] - round(20 * np.log10(0.3), 4)) < 0.01
    assert metrics["clipping_samples"] == 0
    assert metrics["clipping_ratio"] == 0.0
    assert metrics["crest_factor_db"] is not None
    assert metrics["lufs_integrated"] is not None
    assert metrics["measurement_note"].startswith("Objective measurements only")


def test_silence_is_reported_honestly() -> None:
    silence = AudioBuffer(np.zeros((24000, 1), dtype=np.float32), 24000)
    metrics = analyze(silence)
    assert metrics["peak_dbfs"] is None
    assert metrics["rms_dbfs"] is None
    assert metrics["lufs_integrated"] is None
    assert any("silence" in note.lower() or "LUFS" in note for note in metrics["notes"])


def test_identical_comparison() -> None:
    buffer = _tone_buffer()
    metrics = analyze(buffer, buffer.copy())
    reference = metrics["reference"]
    assert reference["comparable"] is True
    assert reference["identical"] is True
    assert reference["snr_db"] is None
    assert reference["max_abs_delta"] == 0.0


def test_added_noise_comparison() -> None:
    original = _tone_buffer()
    samples = original.samples.copy()
    rng = np.random.default_rng(7)
    samples += rng.normal(0, 0.001, size=samples.shape).astype(np.float32)
    degraded = AudioBuffer(samples, original.sample_rate)
    reference = analyze(degraded, original)["reference"]
    assert reference["comparable"] is True
    assert reference["identical"] is False
    assert reference["snr_db"] is not None and reference["snr_db"] > 20
    assert reference["max_abs_delta"] > 0


def test_clipping_detection() -> None:
    frames = 2400
    samples = np.full((frames, 1), 1.0, dtype=np.float32)
    metrics = analyze(AudioBuffer(samples, 24000))
    assert metrics["clipping_samples"] == frames
    assert metrics["clipping_ratio"] == 1.0


def test_loaded_tone_file(tone_wav) -> None:
    buffer, _info = load_audio(tone_wav)
    metrics = analyze(buffer)
    assert metrics["duration_seconds"] == round(buffer.frames / buffer.sample_rate, 4)
    assert metrics["true_peak_dbtp"] is not None
    assert metrics.get("centroid_hz") is not None
