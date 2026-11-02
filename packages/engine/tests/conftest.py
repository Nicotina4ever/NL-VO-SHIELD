"""Shared pytest fixtures and helpers.

All fixtures generate synthetic audio in temporary directories: tests never
depend on personal recordings. The only optional real-file test uses the
authorized workspace ``input/test.wav`` if it exists and skips otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from nl_vo_shield.context import AppContext  # noqa: E402


def write_tone(
    path: Path,
    *,
    sample_rate: int = 24000,
    seconds: float = 1.5,
    frequency: float = 220.0,
    amplitude: float = 0.3,
    channels: int = 1,
    subtype: str = "PCM_16",
) -> Path:
    """Create a deterministic synthetic tone file (no personal data)."""
    frames = int(sample_rate * seconds)
    time_axis = np.arange(frames, dtype=np.float64) / sample_rate
    mono = amplitude * np.sin(2.0 * np.pi * frequency * time_axis)
    if channels > 1:
        data = np.stack([mono] * channels, axis=1).copy()
        data[:, 1] *= 0.8
    else:
        data = mono
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sample_rate, subtype=subtype)
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    return AppContext(tmp_path / "appdata")


@pytest.fixture
def tone_wav(tmp_path: Path) -> Path:
    return write_tone(tmp_path / "tone.wav")


def c2pa_available(context: AppContext) -> bool:
    return bool(context.registry.get("c2pa").availability()[0])


def audioseal_available(context: AppContext) -> bool:
    return bool(context.registry.get("audioseal").availability()[0])


def workspace_test_audio() -> Path | None:
    """The authorized test WAV from the development workspace, if present."""
    from nl_vo_shield import paths

    try:
        candidate = paths.workspace_root() / "input" / "test.wav"
    except Exception:
        return None
    return candidate if candidate.is_file() else None
