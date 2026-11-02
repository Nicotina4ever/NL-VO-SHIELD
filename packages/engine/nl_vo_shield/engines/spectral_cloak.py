"""Anti-clone DSP perturbation (STFT, psychoacoustically bounded).

This engine really perturbs the signal: a seeded random perturbation is applied
to the STFT of each channel, bounded by a smoothed spectral magnitude floor
(a simplified masking threshold) and then scaled to a target SNR so the
distortion stays controlled.

Honesty: the effectiveness of this perturbation against modern voice-cloning
models is NOT validated. It is reported as ``experimental`` and must never be
described as "protected against ElevenLabs".
"""

from __future__ import annotations

import math
import secrets
from typing import Any

import numpy as np

from .. import __version__
from ..audio_io import AudioBuffer
from ..errors import EngineExecutionError
from .base import BaseEngine, EngineContext

_STRENGTHS: dict[str, dict[str, float]] = {
    # snr_db: target distortion; mask_db: magnitude floor below local spectrum;
    # phase_sigma: radians of phase perturbation.
    "low": {"snr_db": 30.0, "mask_db": 18.0, "phase_sigma": 0.10},
    "medium": {"snr_db": 24.0, "mask_db": 14.0, "phase_sigma": 0.20},
    "high": {"snr_db": 19.0, "mask_db": 10.0, "phase_sigma": 0.30},
}
_N_FFT = 1024
_HOP = 256
_MIN_FRAMES = 64
_MIN_ACCEPTED_SNR_DB = 12.0
_MAX_ACCEPTED_SNR_DB = 60.0


def _window(n_fft: int) -> np.ndarray:
    return np.hanning(n_fft + 1)[:-1]  # periodic Hann (COLA-friendly)


def _stft(signal: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    window = _window(n_fft)
    pad = n_fft // 2
    padded = np.pad(signal, (pad, pad + hop), mode="reflect" if signal.size > pad else "constant")
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
    return np.fft.rfft(frames * window, axis=1)


def _istft(spectrum: np.ndarray, n_fft: int, hop: int, length: int) -> np.ndarray:
    window = _window(n_fft)
    frames = np.fft.irfft(spectrum, n=n_fft, axis=1) * window
    frame_count = frames.shape[0]
    total = (frame_count - 1) * hop + n_fft
    output = np.zeros(total, dtype=np.float64)
    weights = np.zeros(total, dtype=np.float64)
    for index in range(frame_count):
        start = index * hop
        output[start:start + n_fft] += frames[index]
        weights[start:start + n_fft] += window * window
    valid = weights > 1e-8
    output[valid] /= weights[valid]
    pad = n_fft // 2
    trimmed = output[pad:pad + length]
    if trimmed.size < length:
        trimmed = np.pad(trimmed, (0, length - trimmed.size))
    return trimmed[:length]


def _smoothed_magnitude(magnitude: np.ndarray) -> np.ndarray:
    from scipy.ndimage import uniform_filter1d

    return uniform_filter1d(magnitude, size=9, axis=0, mode="nearest")


class SpectralCloakEngine(BaseEngine):
    id = "spectral_cloak"
    name = "Spectral Cloak (STFT perturbation)"
    kind = "anti_clone"
    version = __version__
    capabilities = ["perturb"]
    requires = ["numpy", "scipy"]
    requires_download = False
    status_override = "experimental"
    quality_note = (
        "Real STFT perturbation with a masking-style magnitude bound and measured distortion. "
        "Anti-cloning effectiveness is NOT validated: treat as experimental, never as "
        "'protection against ElevenLabs'."
    )

    description = (
        "STFT-domain anti-clone perturbation with a masking-style magnitude bound: a seeded "
        "random perturbation is added under a smoothed spectral floor, then scaled to a target SNR."
    )
    impact = (
        "Alters the spectrum slightly (target SNR 30/24/19 dB for low/medium/high). The "
        "distortion is measured and reported per job; high strength may be audible."
    )
    limitations = [
        "Anti-cloning effectiveness is NOT validated against any cloning model.",
        "Cannot perturb digital silence (no masking reference): the step fails closed there.",
        "Should not be stacked with other anti-clone perturbations.",
    ]
    recommended_levels = ["balanced", "strong"]
    incompatible_with = ["safespeech"]

    def __init__(self, context: Any) -> None:
        self.context = context

    @staticmethod
    def _config(strength: str) -> dict[str, float]:
        key = str(strength or "medium").lower()
        if key not in _STRENGTHS:
            raise EngineExecutionError(
                f"Unknown strength '{strength}'; choose one of {sorted(_STRENGTHS)}"
            )
        return _STRENGTHS[key]

    def _perturb_channel(
        self,
        channel: np.ndarray,
        sample_rate: int,
        config: dict[str, float],
        seed: int,
        channel_index: int,
        ctx: EngineContext,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        signal = channel.astype(np.float64)
        n_fft = _N_FFT
        if signal.size < n_fft * 2:
            n_fft = int(2 ** max(6, int(math.floor(math.log2(max(signal.size, _MIN_FRAMES))))))
            n_fft = min(n_fft, 1024)
        hop = max(1, n_fft // 4)
        spectrum = _stft(signal, n_fft, hop)
        magnitude = np.abs(spectrum)
        phase = np.angle(spectrum)
        smoothed = _smoothed_magnitude(magnitude)
        floor = smoothed * (10.0 ** (-config["mask_db"] / 20.0))
        rng = np.random.default_rng(seed + channel_index * 7919)
        delta_magnitude = rng.uniform(0.25, 1.0, size=magnitude.shape) * floor
        delta_phase = rng.normal(0.0, config["phase_sigma"], size=magnitude.shape)
        delta = delta_magnitude * np.exp(1j * (phase + delta_phase))
        perturbation = _istft(delta, n_fft, hop, signal.size).astype(np.float32)

        variance_signal = float(np.var(signal))
        variance_perturbation = float(np.var(perturbation))
        if variance_signal <= 1e-12:
            # Silence: a relative perturbation has no masking reference. Use a
            # small absolute level instead of reporting a fake SNR.
            perturbation *= 0.0
            alpha = 0.0
            achieved = None
        else:
            if variance_perturbation <= 1e-20:
                raise EngineExecutionError("Perturbation generator produced no signal")
            ratio = math.sqrt(variance_signal / variance_perturbation)
            target = 10.0 ** (config["snr_db"] / 20.0)
            alpha = min(1.0, ratio / target)
            achieved = 10.0 * math.log10(
                variance_signal / max((alpha * alpha) * variance_perturbation, 1e-20)
            )
        input_peak = float(np.max(np.abs(signal))) if signal.size else 0.0
        delta_peak = float(np.max(np.abs(perturbation))) if perturbation.size else 0.0
        if alpha > 0 and delta_peak > 0 and input_peak + alpha * delta_peak > 0.999:
            available = 0.999 - input_peak
            if available <= 1e-4:
                raise EngineExecutionError(
                    f"Input peak {input_peak:.4f} leaves no headroom for the anti-clone perturbation."
                )
            alpha = available / delta_peak
        output = signal + alpha * perturbation
        output_peak = float(np.max(np.abs(output))) if output.size else 0.0
        if output_peak > 0.999:
            raise EngineExecutionError("Perturbed output would clip; aborting (fail-closed).")
        metrics = {
            "alpha": round(float(alpha), 8),
            "n_fft": n_fft,
            "hop": hop,
            "target_snr_db": config["snr_db"],
            "measured_snr_db": round(achieved, 3) if achieved is not None else None,
            "max_delta": round(float(np.max(np.abs(output - signal))), 8) if output.size else 0.0,
            "input_peak": round(input_peak, 6),
            "output_peak": round(output_peak, 6),
            "mask_db": config["mask_db"],
        }
        return output.astype(np.float32), metrics

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action not in ("perturb", "embed"):
            raise EngineExecutionError(f"spectral_cloak supports 'perturb', not '{action}'")
        strength = str(params.get("strength", "medium"))
        config = self._config(strength)
        seed_value = params.get("seed")
        if seed_value is None:
            seed = secrets.randbits(31)
        else:
            try:
                seed = int(seed_value)
            except (TypeError, ValueError) as exc:
                raise EngineExecutionError(f"Invalid seed: {seed_value!r}") from exc
            if seed < 0:
                raise EngineExecutionError("seed must be a non-negative integer")
        samples = np.empty_like(buffer.samples)
        channel_metrics: list[dict[str, Any]] = []
        for channel in range(buffer.channels):
            ctx.check_cancel()
            ctx.emit(
                "spectral_cloak",
                int(5 + 75 * channel / max(buffer.channels, 1)),
                f"STFT perturbation channel {channel + 1}/{buffer.channels}",
            )
            perturbed, metrics = self._perturb_channel(
                buffer.samples[:, channel], buffer.sample_rate, config, seed, channel, ctx
            )
            samples[:, channel] = perturbed
            channel_metrics.append(metrics)
        output = AudioBuffer(
            samples,
            buffer.sample_rate,
            source_path=buffer.source_path,
            source_format=buffer.source_format,
            source_subtype=buffer.source_subtype,
            notes=list(buffer.notes),
        )
        output.notes.append(
            "Spectral Cloak perturbation applied (experimental anti-clone DSP; efficacy not validated)."
        )
        measured = [m["measured_snr_db"] for m in channel_metrics if m["measured_snr_db"] is not None]
        record = {
            "engine": self.id,
            "action": "perturb",
            "version": self.version,
            "params": {
                "strength": strength,
                "seed": seed,
                "target_snr_db": config["snr_db"],
            },
            "detail": {
                "channels": buffer.channels,
                "sample_rate": buffer.sample_rate,
                "n_fft": channel_metrics[0]["n_fft"] if channel_metrics else None,
                "measured_snr_db": round(float(np.mean(measured)), 3) if measured else None,
                "per_channel": channel_metrics,
                "efficacy_validated": False,
                "warning": (
                    "Anti-clone effectiveness was not validated against any cloning model. "
                    "This is an experimental DSP perturbation, not a guarantee."
                ),
            },
        }
        ctx.emit("spectral_cloak", 85, "perturbation applied; measuring distortion")
        return output, record

    def verify_effect(self, action: str, buffer: AudioBuffer, params: dict[str, Any],
                      ctx: EngineContext) -> dict[str, Any]:
        # The pipeline calls verify_effect with the *output* buffer; the input is
        # provided through params by the pipeline under "_input_buffer".
        reference = params.get("_input_buffer")
        if not isinstance(reference, AudioBuffer):
            return {
                "check": "spectral_cloak.distortion",
                "ok": False,
                "detail": {"reason": "input buffer not available for distortion measurement"},
            }
        frames = min(buffer.frames, reference.frames)
        signal = reference.samples[:frames].astype(np.float64)
        output = buffer.samples[:frames].astype(np.float64)
        delta = output - signal
        variance_signal = float(np.var(signal))
        variance_delta = float(np.var(delta))
        if variance_signal <= 1e-12:
            snr = None
            changed = float(np.max(np.abs(delta))) > 1e-9 if delta.size else False
        else:
            snr = (
                10.0 * math.log10(variance_signal / variance_delta)
                if variance_delta > 0
                else None
            )
            changed = variance_delta > 0
        peak = float(np.max(np.abs(buffer.samples)))
        ok = bool(
            changed
            and snr is not None
            and _MIN_ACCEPTED_SNR_DB <= snr <= _MAX_ACCEPTED_SNR_DB
            and peak <= 1.0
        )
        return {
            "check": "spectral_cloak.distortion",
            "ok": ok,
            "detail": {
                "changed": bool(changed),
                "snr_db": round(snr, 3) if snr is not None else None,
                "max_delta": round(float(np.max(np.abs(delta))), 8) if delta.size else 0.0,
                "output_peak": round(peak, 6),
                "accepted_snr_range_db": [_MIN_ACCEPTED_SNR_DB, _MAX_ACCEPTED_SNR_DB],
                "efficacy_validated": False,
            },
        }
