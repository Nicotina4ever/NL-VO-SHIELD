"""Audio I/O: formats, sample-rate preservation, atomic writes, resampling."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nl_vo_shield.audio_io import (
    AudioBuffer,
    choose_subtype,
    fit_length,
    load_audio,
    resample_buffer,
    write_audio_atomic,
)
from nl_vo_shield.errors import AudioError, AudioFormatError


def test_load_preserves_rate_and_channels(tone_wav: Path) -> None:
    buffer, info = load_audio(tone_wav)
    assert buffer.sample_rate == 24000
    assert buffer.channels == 1
    assert info.frames > 0
    assert buffer.samples.dtype == np.float32


def test_stereo_roundtrip_preserves_layout(tmp_path: Path) -> None:
    from tests.conftest import write_tone

    source = write_tone(tmp_path / "stereo.wav", sample_rate=48000, channels=2, subtype="PCM_24")
    buffer, info = load_audio(source)
    assert buffer.channels == 2 and buffer.sample_rate == 48000
    target = tmp_path / "copy.wav"
    manifest = write_audio_atomic(buffer, target, subtype=choose_subtype(info, target))
    assert manifest["sample_rate"] == 48000
    assert manifest["channels"] == 2
    assert manifest["subtype"] == "PCM_24"
    assert target.is_file()
    reloaded, info2 = load_audio(target)
    assert info2.sample_rate == 48000 and info2.channels == 2
    assert reloaded.frames == buffer.frames
    assert np.max(np.abs(reloaded.samples - buffer.samples)) < 1e-6


def test_no_temporary_files_left_behind(tmp_path: Path, tone_wav: Path) -> None:
    buffer, info = load_audio(tone_wav)
    target = tmp_path / "out" / "x.wav"
    write_audio_atomic(buffer, target)
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


def test_write_never_overwrites_input(tmp_path: Path, tone_wav: Path) -> None:
    original = tone_wav.read_bytes()
    buffer, info = load_audio(tone_wav)
    write_audio_atomic(buffer, tone_wav)  # explicit same-path write is still atomic
    assert tone_wav.read_bytes() == original


def test_resample_changes_rate_but_not_channels(tone_wav: Path) -> None:
    buffer, _ = load_audio(tone_wav)
    resampled = resample_buffer(buffer, 16000)
    assert resampled.sample_rate == 16000
    assert resampled.channels == buffer.channels
    assert abs(resampled.duration - buffer.duration) < 0.01


def test_fit_length_trims_and_pads(tone_wav: Path) -> None:
    buffer, _ = load_audio(tone_wav)
    shorter = fit_length(buffer, 1000)
    assert shorter.frames == 1000
    longer = fit_length(buffer, buffer.frames + 500)
    assert longer.frames == buffer.frames + 500
    assert np.allclose(longer.samples[buffer.frames:, :], 0.0)


def test_invalid_extension_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "file.xyz"
    bad.write_bytes(b"not audio")
    with pytest.raises(AudioFormatError):
        load_audio(bad)


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(AudioError):
        load_audio(tmp_path / "missing.wav")


def test_empty_buffer_rejected() -> None:
    with pytest.raises(AudioError):
        AudioBuffer(np.zeros((0, 1), dtype=np.float32), 24000)


def test_mp3_roundtrip_when_ffmpeg_available(tmp_path: Path, tone_wav: Path) -> None:
    from nl_vo_shield import paths

    if paths.find_ffmpeg() is None:
        pytest.skip("ffmpeg not available")
    buffer, info = load_audio(tone_wav)
    target = tmp_path / "tone.mp3"
    write_audio_atomic(buffer, target)
    assert target.is_file() and target.stat().st_size > 0
    reloaded, mp3_info = load_audio(target)
    assert reloaded.frames > 0
    assert reloaded.sample_rate == buffer.sample_rate
    assert abs(reloaded.duration - buffer.duration) < 0.2
