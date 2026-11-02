"""Objective audio quality measurements.

These are measurements, not a listening test and not a proof of protection.
LUFS (BS.1770) and true-peak are computed when the optional libraries are
available; otherwise the fields are ``None`` with an explicit note.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .audio_io import AudioBuffer, resample_buffer

_FLOOR = 1e-12
CLIPPING_THRESHOLD = 0.999


def _db(value: float) -> float | None:
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    return round(20.0 * math.log10(value), 4)


def _true_peak(samples: np.ndarray, oversample: int = 4) -> float:
    """Approximate true peak by polyphase oversampling per channel."""
    from scipy.signal import resample_poly

    peak = 0.0
    for channel in range(samples.shape[1]):
        up = resample_poly(samples[:, channel].astype(np.float64), oversample, 1)
        if up.size:
            peak = max(peak, float(np.max(np.abs(up))))
    return peak


def _loudness(buffer: AudioBuffer) -> tuple[float | None, list[str]]:
    notes: list[str] = []
    try:
        import pyloudnorm as pyln  # type: ignore
    except ImportError:
        return None, ["pyloudnorm not installed: integrated LUFS unavailable"]
    if buffer.duration < 0.45:
        return None, ["Audio shorter than ~0.4 s: BS.1770 loudness gating is not meaningful"]
    try:
        meter = pyln.Meter(buffer.sample_rate)
        value = float(meter.integrated_loudness(buffer.samples.astype(np.float64)))
    except Exception as exc:  # silence / unusual channel layouts
        return None, [f"LUFS not computable: {type(exc).__name__}"]
    if not np.isfinite(value):
        return None, ["LUFS is -inf (silence): integrated loudness undefined"]
    return round(value, 3), notes


def _spectral_moments(buffer: AudioBuffer) -> dict[str, float | None]:
    from scipy.signal import stft

    mono = buffer.mono()
    if mono.size < 64:
        return {"centroid_hz": None, "rolloff95_hz": None, "flatness": None}
    nperseg = int(min(1024, max(64, 2 ** int(math.floor(math.log2(max(mono.size // 8, 64)))))))
    nperseg = min(nperseg, mono.size)
    freqs, _times, spectrum = stft(
        mono,
        fs=buffer.sample_rate,
        window="hann",
        nperseg=nperseg,
        noverlap=int(nperseg * 0.75),
        boundary=None,
        padded=False,
    )
    magnitude = np.abs(spectrum)
    power = np.square(magnitude)
    total = power.sum(axis=0) + _FLOOR
    centroid = (freqs[:, None] * power).sum(axis=0) / total
    cumulative = np.cumsum(power, axis=0)
    rolloff = np.empty(power.shape[1])
    for index in range(power.shape[1]):
        target = 0.95 * cumulative[-1, index]
        rolloff[index] = freqs[min(int(np.searchsorted(cumulative[:, index], target)), len(freqs) - 1)]
    flatness = np.exp(np.mean(np.log(power + _FLOOR), axis=0)) / (np.mean(power, axis=0) + _FLOOR)
    return {
        "centroid_hz": round(float(np.mean(centroid)), 2),
        "rolloff95_hz": round(float(np.mean(rolloff)), 2),
        "flatness": round(float(np.mean(flatness)), 6),
    }


def _reference_metrics(buffer: AudioBuffer, reference: AudioBuffer) -> dict[str, Any]:
    from scipy.signal import stft

    notes: list[str] = []
    ref = reference
    if ref.sample_rate != buffer.sample_rate:
        notes.append(
            f"Reference resampled {ref.sample_rate} Hz -> {buffer.sample_rate} Hz for comparison"
        )
        ref = resample_buffer(ref, buffer.sample_rate)
    frames = min(buffer.frames, ref.frames)
    if frames <= 0:
        return {"comparable": False, "reason": "no overlapping frames"}
    a = buffer.samples[:frames].astype(np.float64)
    b = ref.samples[:frames].astype(np.float64)
    if a.shape[1] != b.shape[1]:
        notes.append(
            f"Channel layouts differ ({ref.channels} -> {buffer.channels}); comparison uses a mono mix"
        )
        a_m, b_m = a.mean(axis=1), b.mean(axis=1)
    else:
        a_m, b_m = a.reshape(-1), b.reshape(-1)
    if frames != ref.frames:
        notes.append(f"Compared the first {frames} frames of {ref.frames}")
    difference = a_m - b_m
    energy_source = float(np.sum(np.square(a_m)))
    energy_error = float(np.sum(np.square(difference)))
    identical = energy_error <= _FLOOR
    snr_db = None if identical else (
        round(10.0 * math.log10(energy_source / energy_error), 4) if energy_source > 0 else None
    )
    rms_a = float(np.sqrt(np.mean(np.square(a_m))))
    rms_b = float(np.sqrt(np.mean(np.square(b_m))))
    rms_delta_db = (
        round(20.0 * math.log10(rms_b / rms_a), 4) if rms_a > 0 and rms_b > 0 else None
    )
    correlation = None
    if a_m.size > 1 and np.std(a_m) > 0 and np.std(b_m) > 0:
        correlation = round(float(np.corrcoef(a_m, b_m)[0, 1]), 6)

    lsd_db = None
    try:
        mono_ref = ref.mono()[:frames]
        if mono_ref.size >= 256:
            nperseg = min(1024, mono_ref.size)
            freqs_a, _t, za = stft(a.mean(axis=1), fs=buffer.sample_rate, nperseg=nperseg,
                                  noverlap=nperseg // 2, boundary=None, padded=False)
            _f, _t2, zb = stft(mono_ref, fs=buffer.sample_rate, nperseg=nperseg,
                               noverlap=nperseg // 2, boundary=None, padded=False)
            if za.shape == zb.shape:
                ratio = (np.abs(za) + 1e-9) / (np.abs(zb) + 1e-9)
                lsd_db = round(float(np.mean(20.0 * np.log10(ratio))), 4)
                lsd_db = round(float(np.sqrt(np.mean(np.square(20.0 * np.log10(ratio))))), 4)
    except Exception:
        lsd_db = None
    return {
        "comparable": True,
        "frames_compared": int(frames),
        "identical": bool(identical),
        "snr_db": snr_db,
        "max_abs_delta": round(float(np.max(np.abs(difference))), 8),
        "rms_delta_db": rms_delta_db,
        "correlation": correlation,
        "log_spectral_distance_db": lsd_db,
        "notes": notes,
    }


def analyze(buffer: AudioBuffer, reference: AudioBuffer | None = None) -> dict[str, Any]:
    """Compute objective metrics for ``buffer`` and optionally vs ``reference``."""
    samples = buffer.samples
    peak = buffer.peak
    rms = buffer.rms
    clipping_samples = int(np.count_nonzero(np.abs(samples) >= CLIPPING_THRESHOLD))
    total_samples = int(samples.size)
    notes: list[str] = []

    true_peak = None
    try:
        true_peak = _true_peak(samples)
    except Exception:
        notes.append("True-peak oversampling unavailable")

    lufs, lufs_notes = _loudness(buffer)
    notes.extend(lufs_notes)
    try:
        spectral = _spectral_moments(buffer)
    except Exception:
        spectral = {"centroid_hz": None, "rolloff95_hz": None, "flatness": None}
        notes.append("Spectral descriptors unavailable")

    metrics: dict[str, Any] = {
        "sample_rate": buffer.sample_rate,
        "channels": buffer.channels,
        "frames": buffer.frames,
        "duration_seconds": round(buffer.duration, 4),
        "peak_dbfs": _db(peak),
        "true_peak_dbtp": _db(true_peak) if true_peak is not None else None,
        "rms_dbfs": _db(rms),
        "crest_factor_db": (
            round(20.0 * math.log10(peak / rms), 4) if peak > 0 and rms > 0 else None
        ),
        "dc_offset": round(float(np.max(np.abs(samples.mean(axis=0)))), 8),
        "clipping_samples": clipping_samples,
        "clipping_ratio": round(clipping_samples / total_samples, 8) if total_samples else 0.0,
        "clipping_threshold": CLIPPING_THRESHOLD,
        "lufs_integrated": lufs,
        "notes": notes,
        "measurement_note": (
            "Objective measurements only: not a listening test and not evidence of "
            "anti-cloning effectiveness."
        ),
    }
    metrics.update(spectral)
    if reference is not None:
        metrics["reference"] = _reference_metrics(buffer, reference)
    return metrics
