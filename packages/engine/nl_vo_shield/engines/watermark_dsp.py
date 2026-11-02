"""Genuine DSP spread-spectrum watermark (fallback when AudioSeal is absent).

Real embed/detect: a 16-bit payload is encoded as the sign of 16 band-limited
pseudo-noise segments (DSSS). Embedding adds a low-level (+-30 dB SNR) masked
noise-like signal; detection applies the same band-pass filter and matched
correlation per segment, then decodes the signs and reports a per-bit z-score.

Limitations (stated honestly):
* the key lives in the local data directory, so detection works on this
  installation (not a global identity);
* robustness to PCM WAV round-trips is tested; resistance to lossy codecs or
  resynthesis is not validated;
* it is a watermark (attribution aid), never an anti-cloning guarantee.
"""

from __future__ import annotations

import hashlib
import math
import threading
from typing import Any

import numpy as np

from .. import __version__
from ..audio_io import AudioBuffer
from ..errors import EngineExecutionError, EngineUnavailable
from ..keys import watermark_secret
from .base import BaseEngine, EngineContext

_BITS = 16
_MIN_SEGMENT = 128          # samples per bit segment
_DEFAULT_SNR_DB = 30.0      # kept for API compatibility (upper bound only)
_DEFAULT_TARGET_Z = 6.0     # target per-bit correlation z-score at embed time
_Z_DETECT_MEAN = 3.0
_Z_DETECT_FRACTION = 0.85
_Z_DETECT_FRACTION_MIN = 2.5
_AMP_FLOOR = 1e-4           # inaudible floor (-80 dBFS) so silence stays detectable
_AMP_CAP_ABS = 0.02
_CAP_REL_RMS = 0.08         # never louder than ~-22 dB relative to the signal RMS


def _band_edges(sample_rate: int) -> tuple[float, float]:
    """Watermark band: mid/high speech band, where masking keeps it unobtrusive."""
    lo = max(1200.0, min(2500.0, sample_rate * 0.10))
    hi = min(6500.0, sample_rate * 0.45)
    if hi <= lo * 1.5:
        lo = max(300.0, sample_rate * 0.05)
        hi = sample_rate * 0.40
    return lo, hi


def _filter_sos(sample_rate: int):
    from scipy.signal import butter

    lo, hi = _band_edges(sample_rate)
    nyquist = sample_rate / 2.0
    return butter(4, [lo / nyquist, hi / nyquist], btype="band", output="sos")


def _bandpass(signal: np.ndarray, sos) -> np.ndarray:
    from scipy.signal import sosfilt, sosfiltfilt

    signal = np.asarray(signal, dtype=np.float64)
    try:
        return sosfiltfilt(sos, signal)
    except ValueError:
        return sosfilt(sos, signal)


def _pn(key: bytes, channel: int, bit: int, length: int) -> np.ndarray:
    seed_bytes = hashlib.sha256(
        key + b"|dsp-wm|" + bytes([channel & 0xFF]) + int(bit).to_bytes(2, "little")
    ).digest()
    seed = int.from_bytes(seed_bytes[:8], "big")
    rng = np.random.default_rng(seed)
    return rng.integers(0, 2, size=length, dtype=np.int64).astype(np.float64) * 2.0 - 1.0


def _segment_bounds(frames: int) -> list[tuple[int, int]]:
    edges = np.linspace(0, frames, _BITS + 1).round().astype(int)
    return [(int(edges[i]), int(edges[i + 1])) for i in range(_BITS)]


class DspWatermarkEngine(BaseEngine):
    id = "watermark_dsp"
    name = "DSP Spread-Spectrum Watermark"
    kind = "watermark"
    version = __version__
    capabilities = ["embed", "detect"]
    requires = ["numpy", "scipy"]
    requires_download = False
    quality_note = (
        "Real spread-spectrum watermark (no ML). Local key; detection on this installation only. "
        "Not validated against lossy codecs/resynthesis; attribution aid, not anti-cloning."
    )

    def __init__(self, context: Any) -> None:
        self.context = context

    def _key(self) -> bytes:
        return watermark_secret(self.context.data_dir)

    # ------------------------------------------------------------------ embed
    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "embed":
            raise EngineExecutionError(f"watermark_dsp supports 'embed', not '{action}'")
        if ctx.payload is None:
            raise EngineExecutionError("watermark_dsp requires a 16-bit payload")
        payload = int(ctx.payload) & 0xFFFF
        strength = str(params.get("strength", "standard"))
        try:
            target_z = float(params.get("target_z", _DEFAULT_TARGET_Z))
        except (TypeError, ValueError):
            target_z = _DEFAULT_TARGET_Z
        target_z = max(3.5, min(12.0, target_z))
        if strength == "strong":
            target_z = max(target_z, 8.0)
        key = self._key()
        frames = buffer.frames
        bounds = _segment_bounds(frames)
        shortest = min(end - start for start, end in bounds)
        if shortest < _MIN_SEGMENT:
            raise EngineExecutionError(
                f"Audio too short for the DSP watermark: {shortest} samples per bit segment "
                f"(minimum {_MIN_SEGMENT})"
            )
        ctx.check_cancel()
        sos = _filter_sos(buffer.sample_rate)
        reference_rms = max(buffer.rms, 0.005)
        cap_relative = _CAP_REL_RMS * reference_rms

        samples = buffer.samples.astype(np.float32).copy()
        watermark = np.zeros_like(samples, dtype=np.float64)
        amplitudes: list[float] = []
        for channel in range(buffer.channels):
            ctx.check_cancel()
            ctx.emit(
                "watermark_dsp",
                int(5 + 60 * channel / max(buffer.channels, 1)),
                f"DSP watermark channel {channel + 1}/{buffer.channels}",
            )
            # In-band signal energy per segment drives the amplitude so that the
            # detector reaches its z-score target on any content; quiet passages
            # stay quiet and digital silence only gets an inaudible floor.
            filtered = _bandpass(buffer.samples[:, channel].astype(np.float64), sos)
            for bit, (start, end) in enumerate(bounds):
                length = end - start
                pn = _pn(key, channel, bit, length)
                chip = _bandpass(pn, sos)
                rms = float(np.sqrt(np.mean(chip * chip))) if chip.size else 0.0
                if rms < 1e-9:
                    chip = pn
                    rms = 1.0
                chip = chip / rms
                segment = filtered[start:end]
                band_sigma = float(np.std(segment)) if segment.size > 1 else 0.0
                amplitude = target_z * band_sigma / math.sqrt(length)
                amplitude = min(max(amplitude, _AMP_FLOOR), cap_relative, _AMP_CAP_ABS)
                sign = 1.0 if (payload >> bit) & 1 else -1.0
                watermark[start:end, channel] = sign * amplitude * chip
                amplitudes.append(amplitude)

        input_peak = float(np.max(np.abs(buffer.samples))) if frames else 0.0
        wm_peak = float(np.max(np.abs(watermark))) if watermark.size else 0.0
        alpha = 1.0
        if input_peak + wm_peak > 0.999:
            available = 0.999 - input_peak
            if available <= 1e-4 or wm_peak <= 0:
                raise EngineExecutionError(
                    f"Not enough headroom for the DSP watermark (input peak {input_peak:.4f}); "
                    "no silent clipping is applied."
                )
            alpha = available / wm_peak
            if alpha < 0.05:
                raise EngineExecutionError(
                    "DSP watermark would need heavy attenuation to avoid clipping; aborting."
                )
        samples += np.float32(alpha) * watermark.astype(np.float32)
        output_peak = float(np.max(np.abs(samples)))
        if output_peak > 0.999:
            raise EngineExecutionError("Watermarked output would clip; aborting (fail-closed).")
        output = AudioBuffer(
            samples,
            buffer.sample_rate,
            source_path=buffer.source_path,
            source_format=buffer.source_format,
            source_subtype=buffer.source_subtype,
            notes=list(buffer.notes),
        )
        achieved = (
            10.0 * math.log10(np.var(buffer.samples) / max(np.var(samples - buffer.samples), 1e-20))
            if np.var(buffer.samples) > 0
            else None
        )
        record = {
            "engine": self.id,
            "action": "embed",
            "version": self.version,
            "params": {
                "payload_16bit": payload,
                "payload_hex": f"{payload:04x}",
                "target_z": round(target_z, 2),
                "strength": strength,
                "attenuation": round(float(alpha), 6),
            },
            "detail": {
                "channels": buffer.channels,
                "sample_rate": buffer.sample_rate,
                "band_hz": [round(v, 1) for v in _band_edges(buffer.sample_rate)],
                "segment_samples": shortest,
                "measured_snr_db": round(achieved, 3) if achieved is not None else None,
                "input_peak": round(input_peak, 6),
                "output_peak": round(output_peak, 6),
                "watermark_rms": round(float(np.sqrt(np.mean(np.square(watermark)))), 8),
                "amplitude_min": round(min(amplitudes), 8) if amplitudes else None,
                "amplitude_max": round(max(amplitudes), 8) if amplitudes else None,
            },
        }
        ctx.emit("watermark_dsp", 70, "DSP watermark embedded; verifying")
        return output, record

    # ------------------------------------------------------------------ detect
    def detect(self, buffer: AudioBuffer, ctx: EngineContext) -> dict[str, Any]:
        try:
            key = self._key()
        except Exception as exc:
            return {
                "engine": self.id,
                "detected": False,
                "confidence": 0.0,
                "payload_16bit": None,
                "payload_hex": None,
                "available": False,
                "reason": f"watermark key unavailable: {exc}",
            }
        frames = buffer.frames
        bounds = _segment_bounds(frames)
        shortest = min(end - start for start, end in bounds)
        if shortest < 32:
            return {
                "engine": self.id,
                "detected": False,
                "confidence": 0.0,
                "payload_16bit": None,
                "payload_hex": None,
                "available": True,
                "reason": "audio too short for the DSP watermark detector",
            }
        sos = _filter_sos(buffer.sample_rate)
        correlations = np.zeros(_BITS, dtype=np.float64)
        variances = np.zeros(_BITS, dtype=np.float64)
        for channel in range(buffer.channels):
            filtered = _bandpass(buffer.samples[:, channel].astype(np.float64), sos)
            for bit, (start, end) in enumerate(bounds):
                segment = filtered[start:end]
                length = end - start
                pn = _pn(key, channel, bit, length)
                chip = _bandpass(pn, sos)
                rms = float(np.sqrt(np.mean(chip * chip))) if chip.size else 0.0
                if rms < 1e-9:
                    chip = pn
                    rms = 1.0
                chip = chip / rms
                # Signed amplitude estimate of this bit's watermark in the segment.
                estimate = float(np.dot(segment, chip) / length)
                noise = float(np.std(segment)) if segment.size > 1 else 0.0
                correlations[bit] += estimate
                variances[bit] += (max(noise, 1e-12) ** 2) / length
        z_scores = np.abs(correlations) / np.sqrt(np.maximum(variances, 1e-18))
        signs = correlations > 0
        payload = int(sum((1 << bit) for bit in range(_BITS) if signs[bit]))
        mean_z = float(np.mean(z_scores))
        fraction_strong = float(np.mean(z_scores >= _Z_DETECT_FRACTION_MIN))
        detected = bool(
            (mean_z >= _Z_DETECT_MEAN and fraction_strong >= _Z_DETECT_FRACTION)
            or mean_z >= _Z_DETECT_MEAN + 1.0
        )
        confidence = float(np.clip(mean_z / 6.0, 0.0, 1.0))
        return {
            "engine": self.id,
            "detected": detected,
            "confidence": round(confidence, 6),
            "payload_16bit": payload if detected else None,
            "payload_hex": f"{payload:04x}" if detected else None,
            "bits": _BITS,
            "mean_z": round(mean_z, 4),
            "fraction_strong": round(fraction_strong, 4),
            "per_bit_z": [round(float(v), 3) for v in z_scores],
            "available": True,
            "note": (
                "Local spread-spectrum detector; z-score threshold is heuristic and not calibrated "
                "to a false-positive rate."
            ),
        }

    def verify_effect(self, action: str, buffer: AudioBuffer, params: dict[str, Any],
                      ctx: EngineContext) -> dict[str, Any]:
        detection = self.detect(buffer, ctx)
        expected = int(ctx.payload) & 0xFFFF if ctx.payload is not None else None
        payload_match = (
            expected is not None
            and detection.get("payload_16bit") is not None
            and int(detection["payload_16bit"]) == expected
        )
        ok = bool(detection.get("detected")) and payload_match
        return {
            "check": "watermark_dsp.detect",
            "ok": ok,
            "detail": {**detection, "expected_payload_16bit": expected, "payload_match": payload_match},
        }
