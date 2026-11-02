"""Audio I/O: decode WAV/FLAC/MP3-family, atomic writes, safe resampling.

Design constraints:
* input files are never modified;
* output is written to a temporary file in the destination directory and moved
  with ``os.replace`` (atomic on the same volume);
* sample rate is preserved unless a caller explicitly asks otherwise;
* when the source is lossless PCM the bit depth is preserved where possible;
* no silent downmix: channels are kept unless an engine explicitly needs mono.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import paths
from .errors import AudioError, AudioFormatError, AudioWriteError
from .util import sha256_file

try:  # numpy is a hard dependency, soundfile is the primary decoder
    import soundfile as sf
except ImportError:  # pragma: no cover - reported by doctor
    sf = None  # type: ignore[assignment]

LOSSLESS_SUBTYPES = {"PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"}
PCM_SUBTYPES = {"PCM_16", "PCM_24", "PCM_32"}
DECODE_SUFFIXES = {".wav", ".flac", ".ogg", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".opus", ".wma"}
SF_WRITE_SUFFIXES = {".wav", ".flac", ".ogg"}
FFMPEG_WRITE_SUFFIXES = {".mp3", ".m4a", ".aac", ".opus", ".wma"}
MAX_REASONABLE_CHANNELS = 32


@dataclass
class AudioBuffer:
    """In-memory float32 audio with explicit sample rate and channel layout."""

    samples: np.ndarray  # shape (frames, channels), float32 in [-1, 1] nominally
    sample_rate: int
    source_path: str | None = None
    source_format: str | None = None
    source_subtype: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.samples = np.ascontiguousarray(self.samples, dtype=np.float32)
        if self.samples.ndim == 1:
            self.samples = self.samples[:, None]
        if self.samples.size == 0:
            raise AudioError("Empty audio buffer", code="AUDIO_EMPTY")

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration(self) -> float:
        return self.frames / float(self.sample_rate)

    @property
    def peak(self) -> float:
        return float(np.max(np.abs(self.samples))) if self.frames else 0.0

    @property
    def rms(self) -> float:
        if not self.frames:
            return 0.0
        return float(np.sqrt(np.mean(np.square(self.samples, dtype=np.float64))))

    def copy(self) -> "AudioBuffer":
        return AudioBuffer(
            self.samples.copy(),
            self.sample_rate,
            self.source_path,
            self.source_format,
            self.source_subtype,
            list(self.notes),
        )

    def mono(self) -> np.ndarray:
        if self.channels == 1:
            return self.samples[:, 0].astype(np.float64)
        # Mean downmix is used only for measurements/verification, never as a
        # silent transformation of the delivered audio.
        return self.samples.mean(axis=1).astype(np.float64)

    def info(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
            "duration_seconds": round(self.duration, 4),
            "peak_dbfs": round(20 * math.log10(self.peak), 4) if self.peak > 0 else None,
            "source_format": self.source_format,
            "source_subtype": self.source_subtype,
            "notes": list(self.notes),
        }


@dataclass
class AudioFileInfo:
    path: str
    format: str | None
    subtype: str | None
    sample_rate: int
    channels: int
    frames: int
    duration_seconds: float
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "subtype": self.subtype,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
            "duration_seconds": round(self.duration_seconds, 4),
            "sha256": self.sha256,
        }


def _decode_via_ffmpeg(source: Path, temp_dir: Path) -> Path:
    ffmpeg = paths.find_ffmpeg()
    if ffmpeg is None:
        raise AudioFormatError(
            f"Cannot decode {source.suffix} audio: ffmpeg is not available",
            detail={"path": str(source), "suffix": source.suffix},
        )
    decoded = temp_dir / f"decoded_{os.getpid()}_{abs(hash(str(source))) % 100000}.wav"
    command = [
        str(ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-c:a",
        "pcm_s24le",
        str(decoded),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioFormatError(
            f"ffmpeg failed to decode {source.name}: {exc}", detail={"path": str(source)}
        ) from exc
    if result.returncode != 0 or not decoded.is_file():
        raise AudioFormatError(
            f"ffmpeg could not decode {source.name}: {result.stderr.strip()[:500]}",
            detail={"path": str(source), "ffmpeg_returncode": result.returncode},
        )
    return decoded


def load_audio(
    path: str | Path,
    *,
    temp_dir: str | Path | None = None,
    compute_sha256: bool = False,
) -> tuple[AudioBuffer, AudioFileInfo]:
    """Decode an audio file into float32, preserving rate and channels."""
    path = Path(path)
    if not path.is_file():
        raise AudioError(f"Audio file not found: {path}", code="AUDIO_NOT_FOUND")
    if path.suffix.lower() not in DECODE_SUFFIXES:
        raise AudioFormatError(
            f"Unsupported audio extension: {path.suffix or '(none)'}",
            detail={"path": str(path), "supported": sorted(DECODE_SUFFIXES)},
        )
    if sf is None:
        raise AudioFormatError(
            "soundfile is not installed; the engine base requirements are incomplete"
        )

    samples: np.ndarray
    sample_rate: int
    subtype: str | None = None
    fmt: str | None = None
    used_ffmpeg = False
    try:
        info = sf.info(str(path))
        if info.frames <= 0:
            raise AudioFormatError(f"Audio file contains no frames: {path}", detail={"path": str(path)})
        if info.channels <= 0 or info.channels > MAX_REASONABLE_CHANNELS:
            raise AudioFormatError(
                f"Unsupported channel count {info.channels}", detail={"path": str(path)}
            )
        sample_rate = int(info.samplerate)
        fmt = info.format
        subtype = info.subtype
        samples, sample_rate = sf.read(
            str(path), dtype="float32", always_2d=True
        )
    except (AudioFormatError,):
        raise
    except Exception as exc:  # libsndfile refuses some compressed formats
        use_temp = Path(temp_dir) if temp_dir is not None else Path(tempfile.mkdtemp(prefix="nlvs_decode_"))
        owns_temp = temp_dir is None
        try:
            decoded = _decode_via_ffmpeg(path, use_temp)
            info = sf.info(str(decoded))
            samples, sample_rate = sf.read(str(decoded), dtype="float32", always_2d=True)
            sample_rate = int(info.samplerate)
            subtype = "DECODED_PCM_24"  # original bit depth unknown for lossy sources
            fmt = path.suffix.lstrip(".").upper()
            used_ffmpeg = True
            if owns_temp:
                try:
                    decoded.unlink()
                except OSError:
                    pass
        except AudioError:
            raise
        except Exception as fallback_exc:
            raise AudioFormatError(
                f"Unable to decode {path.name}: {exc}; ffmpeg fallback: {fallback_exc}",
                detail={"path": str(path)},
            ) from fallback_exc
        finally:
            if owns_temp:
                shutil.rmtree(use_temp, ignore_errors=True)

    if samples.size == 0:
        raise AudioError(f"Decoded audio is empty: {path}", detail={"path": str(path)})
    if not np.all(np.isfinite(samples)):
        raise AudioFormatError(
            f"Audio contains non-finite samples (corrupt file?): {path}",
            detail={"path": str(path)},
        )
    notes: list[str] = []
    if used_ffmpeg:
        notes.append(
            "Decoded through ffmpeg because libsndfile could not read the source "
            "format; original bit depth is not preserved for lossy sources."
        )
    buffer = AudioBuffer(
        samples=samples,
        sample_rate=int(sample_rate),
        source_path=str(path),
        source_format=fmt,
        source_subtype=subtype,
        notes=notes,
    )
    file_info = AudioFileInfo(
        path=str(path),
        format=fmt,
        subtype=subtype,
        sample_rate=buffer.sample_rate,
        channels=buffer.channels,
        frames=buffer.frames,
        duration_seconds=buffer.duration,
        sha256=sha256_file(path) if compute_sha256 else None,
    )
    return buffer, file_info


def resample_buffer(buffer: AudioBuffer, target_rate: int) -> AudioBuffer:
    """Band-limited polyphase resampling (no silent clipping or normalization)."""
    target_rate = int(target_rate)
    if target_rate <= 0:
        raise AudioError(f"Invalid target sample rate: {target_rate}")
    if target_rate == buffer.sample_rate:
        return buffer.copy()
    from scipy.signal import resample_poly

    gcd = math.gcd(buffer.sample_rate, target_rate)
    up = target_rate // gcd
    down = buffer.sample_rate // gcd
    channels = []
    for channel in range(buffer.channels):
        resampled = resample_poly(buffer.samples[:, channel].astype(np.float64), up, down)
        channels.append(resampled.astype(np.float32))
    length = min(len(c) for c in channels)
    samples = np.stack([c[:length] for c in channels], axis=1)
    out = AudioBuffer(
        samples=samples,
        sample_rate=target_rate,
        source_path=buffer.source_path,
        source_format=buffer.source_format,
        source_subtype=buffer.source_subtype,
        notes=list(buffer.notes),
    )
    out.notes.append(
        f"Resampled {buffer.sample_rate} Hz -> {target_rate} Hz for processing; "
        "the delivered file keeps the original rate."
    )
    return out


def fit_length(buffer: AudioBuffer, frames: int) -> AudioBuffer:
    """Trim or zero-pad to an exact frame count (used around resampling)."""
    frames = int(frames)
    if frames <= 0:
        raise AudioError("Invalid target frame count")
    if buffer.frames == frames:
        return buffer
    if buffer.frames > frames:
        samples = buffer.samples[:frames, :].copy()
    else:
        pad = np.zeros((frames - buffer.frames, buffer.channels), dtype=np.float32)
        samples = np.concatenate([buffer.samples, pad], axis=0)
    return AudioBuffer(
        samples=samples,
        sample_rate=buffer.sample_rate,
        source_path=buffer.source_path,
        source_format=buffer.source_format,
        source_subtype=buffer.source_subtype,
        notes=list(buffer.notes),
    )


def choose_subtype(source: AudioFileInfo | AudioBuffer | None, destination: Path) -> str | None:
    """Pick a lossless subtype close to the source when the container allows it."""
    suffix = destination.suffix.lower()
    source_subtype = None
    if isinstance(source, AudioFileInfo):
        source_subtype = source.subtype
    elif isinstance(source, AudioBuffer):
        source_subtype = source.source_subtype
    if suffix == ".wav":
        if source_subtype in LOSSLESS_SUBTYPES:
            return str(source_subtype)
        return "PCM_24"
    if suffix == ".flac":
        return "PCM_24"
    if suffix == ".ogg":
        return "VORBIS"
    return None


def write_audio_atomic(
    buffer: AudioBuffer,
    destination: str | Path,
    *,
    subtype: str | None = None,
    temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically write ``buffer`` to ``destination``; returns a small manifest."""
    destination = Path(destination)
    suffix = destination.suffix.lower()
    if suffix not in SF_WRITE_SUFFIXES | FFMPEG_WRITE_SUFFIXES:
        raise AudioFormatError(
            f"Unsupported output extension: {suffix or '(none)'}",
            detail={"destination": str(destination), "supported": sorted(SF_WRITE_SUFFIXES | FFMPEG_WRITE_SUFFIXES)},
        )
    if buffer.frames <= 0:
        raise AudioWriteError("Refusing to write an empty audio buffer")
    if sf is None:
        raise AudioWriteError("soundfile is not installed; cannot write audio")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_in_dest = destination.with_name(
        f".{destination.stem}.{os.getpid()}.{abs(hash(str(destination))) % 100000}.tmp{suffix}"
    )
    chosen_subtype = subtype or choose_subtype(buffer, destination)
    try:
        if suffix in SF_WRITE_SUFFIXES:
            sf.write(
                str(temp_in_dest),
                buffer.samples,
                int(buffer.sample_rate),
                subtype=chosen_subtype,
            )
        else:  # compressed: encode from a temporary lossless WAV
            ffmpeg = paths.find_ffmpeg()
            if ffmpeg is None:
                raise AudioWriteError(
                    f"Cannot encode {suffix} output: ffmpeg is not available",
                    detail={"destination": str(destination)},
                )
            work_dir = Path(temp_dir) if temp_dir is not None else destination.parent
            work_dir.mkdir(parents=True, exist_ok=True)
            intermediate = work_dir / f".nlvs_encode_{os.getpid()}_{abs(hash(str(destination))) % 100000}.wav"
            sf.write(str(intermediate), buffer.samples, int(buffer.sample_rate), subtype="PCM_24")
            command = [str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(intermediate)]
            if suffix == ".mp3":
                command += ["-c:a", "libmp3lame", "-b:a", "256k"]
            command += [str(temp_in_dest)]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
                if result.returncode != 0 or not temp_in_dest.is_file():
                    raise AudioWriteError(
                        f"ffmpeg failed to encode {suffix}: {result.stderr.strip()[:500]}",
                        detail={"destination": str(destination), "ffmpeg_returncode": result.returncode},
                    )
            finally:
                try:
                    intermediate.unlink()
                except OSError:
                    pass
        os.replace(temp_in_dest, destination)
    except AudioError:
        raise
    except Exception as exc:
        raise AudioWriteError(
            f"Failed to write audio to {destination}: {exc}",
            detail={"destination": str(destination)},
        ) from exc
    finally:
        if temp_in_dest.exists():
            try:
                temp_in_dest.unlink()
            except OSError:
                pass
    stat = destination.stat()
    return {
        "path": str(destination),
        "format": suffix.lstrip("."),
        "subtype": chosen_subtype,
        "sample_rate": int(buffer.sample_rate),
        "channels": buffer.channels,
        "frames": buffer.frames,
        "bytes": int(stat.st_size),
        "sha256": sha256_file(destination),
    }


def write_temp_wav(buffer: AudioBuffer, directory: str | Path, name: str, subtype: str = "PCM_24") -> Path:
    """Write a staging WAV inside the job directory, atomically."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.wav"
    write_audio_atomic(buffer, target, subtype=subtype)
    return target
