"""Real AudioSeal adapter (optional dependency, never downloads implicitly).

The official 16-bit generator/detector work at 16 kHz. Audio at another sample
rate is resampled to 16 kHz, watermarked per channel, then resampled back to the
original rate so the delivered file keeps its sample rate. This resampling is
reported in the job result and affects quality (see TEST B).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import math
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .. import __version__
from ..audio_io import AudioBuffer, fit_length, resample_buffer
from ..errors import EngineExecutionError, EngineUnavailable
from .base import BaseEngine, EngineContext

_MODEL_RATE = 16000
_MIN_SAMPLES = int(_MODEL_RATE * 0.3)  # AudioSeal needs a minimum context window
_CACHE: dict[tuple[str, float], Any] = {}
_LOAD_LOCK = threading.Lock()


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class AudioSealEngine(BaseEngine):
    id = "audioseal"
    name = "AudioSeal"
    kind = "watermark"
    version = "unknown"
    capabilities = ["embed", "detect"]
    requires = ["torch", "audioseal", "weights: audioseal-generator", "weights: audioseal-detector"]
    requires_download = True
    quality_note = (
        "Watermark = attribution aid, not anti-cloning. 16-bit payload only; the full receipt ID "
        "is stored separately. Detector threshold is not calibrated for a false-positive rate."
    )

    description = (
        "Neural watermark from Meta AudioSeal: embeds a 16-bit payload and provides a "
        "matching detector. Used for attribution of protected audio."
    )
    impact = (
        "Adds an inaudible watermark (no audible change expected). Audio is resampled to "
        "16 kHz for watermarking and back to the original rate; quality impact is minimal "
        "but measured in every job."
    )
    limitations = [
        "Only a 16-bit payload: the full unique ID lives in the signed receipt, not in the audio.",
        "Requires the optional torch/audioseal stack and the official weights (never downloaded automatically).",
        "Detector threshold is not calibrated to a false-positive rate.",
        "A watermark is an attribution aid, not anti-cloning protection.",
    ]
    recommended_levels = ["light", "balanced", "strong"]
    incompatible_with = []

    def __init__(self, context: Any) -> None:
        self.context = context
        self.version = _package_version("audioseal")

    # ------------------------------------------------------------------ status
    def availability(self) -> tuple[bool, str | None]:
        if not _has_module("torch"):
            return False, (
                "torch is not installed in the engine runtime; install the optional AI stack "
                "(requirements-optional.txt) to enable AudioSeal. AudioSeal weights are then "
                "downloaded only on explicit request (models download); no silent download happens."
            )
        if not _has_module("audioseal"):
            return False, (
                "audioseal package is not installed; install requirements-optional.txt to enable "
                "AudioSeal (the base release does not include it)."
            )
        ready, reason = self._models().is_ready("audioseal")
        if not ready:
            return False, reason
        return True, None

    def _models(self):
        return self.context.models

    def _model_paths(self) -> tuple[Path, Path]:
        store = self._models()
        from ..models import model_spec

        generator = store.path_for(model_spec("audioseal-generator"))
        detector = store.path_for(model_spec("audioseal-detector"))
        if generator is None or detector is None:
            raise EngineUnavailable("AudioSeal model paths are not configured")
        if not generator.is_file() or not detector.is_file():
            raise EngineUnavailable(
                "AudioSeal weights are missing. Run the explicit model download first "
                "(models.download / `models download audioseal-generator|detector`).",
                detail={"generator": str(generator), "detector": str(detector)},
            )
        return generator, detector

    def _load(self, kind: str):
        """Load (and cache) the official generator/detector from an explicit file."""
        generator, detector = self._model_paths()
        path = generator if kind == "generator" else detector
        key = (kind, path.stat().st_mtime)
        with _LOAD_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                return cached
            available, reason = self.availability()
            if not available:
                raise EngineUnavailable(reason or "AudioSeal is unavailable")
            try:
                import torch  # noqa: F401  (import here: env is configured in AppContext)
                from audioseal import AudioSeal

                try:
                    torch.set_num_threads(max(1, min(4, (torch.get_num_threads() or 1))))
                except Exception:
                    pass
                if kind == "generator":
                    model = AudioSeal.load_generator(str(path), nbits=16, device="cpu")
                else:
                    model = AudioSeal.load_detector(str(path), nbits=16, device="cpu")
                model.eval()
            except EngineUnavailable:
                raise
            except Exception as exc:
                raise EngineExecutionError(
                    f"Failed to load the AudioSeal {kind}: {type(exc).__name__}: {exc}",
                    detail={"path": str(path)},
                ) from exc
            _CACHE[key] = model
            return model

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _bits(payload: int) -> Any:
        import torch

        return torch.tensor(
            [[(int(payload) >> bit) & 1 for bit in range(16)]], dtype=torch.int32
        )

    def _to_model(self, channel: np.ndarray, sample_rate: int) -> np.ndarray:
        internal = AudioBuffer(np.asarray(channel, dtype=np.float32)[:, None], sample_rate)
        resampled = resample_buffer(internal, _MODEL_RATE)
        return resampled.samples[:, 0]

    def _from_model(self, watermark: np.ndarray, target_rate: int, target_frames: int) -> np.ndarray:
        internal = AudioBuffer(np.asarray(watermark, dtype=np.float32)[:, None], _MODEL_RATE)
        resampled = resample_buffer(internal, target_rate)
        fitted = fit_length(resampled, target_frames)
        return fitted.samples[:, 0]

    # ------------------------------------------------------------------ embed
    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        if action != "embed":
            raise EngineExecutionError(f"AudioSeal supports 'embed', not '{action}'")
        if ctx.payload is None:
            raise EngineExecutionError("AudioSeal embed requires a 16-bit payload")
        payload = int(ctx.payload) & 0xFFFF
        ctx.check_cancel()
        ctx.emit("audioseal", 5, "loading AudioSeal generator")
        generator = self._load("generator")
        bits = self._bits(payload)

        sample_rate = buffer.sample_rate
        watermarks: list[np.ndarray] = []
        for channel in range(buffer.channels):
            if ctx.cancelled:
                from ..errors import JobCancelled

                raise JobCancelled("Job cancelled during AudioSeal embedding")
            ctx.emit(
                "audioseal",
                int(5 + 70 * channel / max(buffer.channels, 1)),
                f"AudioSeal embed channel {channel + 1}/{buffer.channels}",
            )
            mono = buffer.samples[:, channel].astype(np.float32)
            model_audio = self._to_model(mono, sample_rate)
            if model_audio.size < _MIN_SAMPLES:
                raise EngineExecutionError(
                    f"Audio too short for AudioSeal: {model_audio.size} samples at 16 kHz "
                    f"(minimum ~{_MIN_SAMPLES})",
                )
            import torch

            tensor = torch.from_numpy(model_audio.astype(np.float32)).unsqueeze(0).unsqueeze(0)
            channel_start = int(5 + 70 * channel / max(buffer.channels, 1))
            channel_end = int(5 + 70 * (channel + 1) / max(buffer.channels, 1))
            try:
                with ctx.heartbeat(
                    "audioseal", channel_start, channel_end,
                    f"AudioSeal inference channel {channel + 1}/{buffer.channels}",
                ):
                    with torch.no_grad():
                        watermark = generator.get_watermark(tensor, message=bits)
                watermark_np = watermark.squeeze().detach().cpu().numpy().astype(np.float32)
            except Exception as exc:
                raise EngineExecutionError(
                    f"AudioSeal generator failed: {type(exc).__name__}: {exc}"
                ) from exc
            back = self._from_model(watermark_np, sample_rate, buffer.frames)
            watermarks.append(back)

        # Peak guard: scale the watermark to keep headroom, never clip silently.
        input_peak = float(np.max(np.abs(buffer.samples)))
        target_peak = 0.995
        headroom = target_peak - input_peak
        if headroom <= 1e-4:
            raise EngineExecutionError(
                f"Input peak is {input_peak:.4f}: not enough headroom to add a watermark without clipping. "
                "Lower the level or trim the file first (no silent clipping).",
            )
        needed = max(float(np.max(np.abs(wm))) for wm in watermarks)
        alpha = min(1.0, headroom / needed) if needed > 0 else 1.0
        if alpha < 0.05:
            raise EngineExecutionError(
                "AudioSeal watermark would need heavy attenuation to avoid clipping; refusing to write "
                "an unreliable watermark."
            )
        samples = buffer.samples.astype(np.float32).copy()
        for channel in range(buffer.channels):
            samples[:, channel] += np.float32(alpha) * watermarks[channel]
        output_peak = float(np.max(np.abs(samples)))
        if output_peak > 0.999:
            raise EngineExecutionError(
                f"Watermarked output would clip (peak {output_peak:.4f}); aborting (fail-closed)."
            )
        output = AudioBuffer(
            samples,
            sample_rate,
            source_path=buffer.source_path,
            source_format=buffer.source_format,
            source_subtype=buffer.source_subtype,
            notes=list(buffer.notes),
        )
        output.notes.append(
            "AudioSeal watermark applied at 16 kHz internally and resampled back to the original rate."
        )
        ctx.emit("audioseal", 80, "watermark embedded; verifying")
        record = {
            "engine": self.id,
            "action": "embed",
            "version": self.version,
            "params": {
                "payload_16bit": payload,
                "payload_hex": f"{payload:04x}",
                "model_rate_hz": _MODEL_RATE,
                "attenuation": round(alpha, 6),
            },
            "detail": {
                "channels": buffer.channels,
                "sample_rate": sample_rate,
                "input_peak": round(input_peak, 6),
                "output_peak": round(output_peak, 6),
                "resampled_for_processing": sample_rate != _MODEL_RATE,
                "watermark_peak": round(needed * alpha, 8),
            },
        }
        return output, record

    # ------------------------------------------------------------------ detect
    def detect(self, buffer: AudioBuffer, ctx: EngineContext) -> dict[str, Any]:
        available, reason = self.availability()
        if not available:
            return {
                "engine": self.id,
                "detected": False,
                "confidence": 0.0,
                "payload_16bit": None,
                "payload_hex": None,
                "available": False,
                "reason": reason,
            }
        detector = self._load("detector")
        import torch

        threshold = float(ctx.settings.get("watermark_threshold", 0.5))
        per_channel = []
        best = None
        for channel in range(buffer.channels):
            mono = buffer.samples[:, channel].astype(np.float32)
            model_audio = self._to_model(mono, buffer.sample_rate)
            if model_audio.size < _MIN_SAMPLES:
                per_channel.append({"channel": channel, "error": "audio too short for detector"})
                continue
            tensor = torch.from_numpy(model_audio.astype(np.float32)).unsqueeze(0).unsqueeze(0)
            try:
                with ctx.heartbeat(
                    "audioseal", 10, 70,
                    f"AudioSeal detector channel {channel + 1}/{buffer.channels}",
                ):
                    with torch.no_grad():
                        score, bits = detector.detect_watermark(tensor)
                confidence = float(torch.as_tensor(score).float().mean().item())
                values = torch.as_tensor(bits).float().reshape(-1)
                payload = None
                if values.numel() >= 16:
                    payload = sum(
                        (int(float(values[i]) > 0.5) << i) for i in range(16)
                    )
            except Exception as exc:
                per_channel.append(
                    {"channel": channel, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            entry = {
                "channel": channel,
                "score": round(confidence, 6),
                "payload_16bit": payload,
                "payload_hex": f"{payload:04x}" if payload is not None else None,
            }
            per_channel.append(entry)
            if best is None or confidence > best["score"]:
                best = entry
        if best is None:
            return {
                "engine": self.id,
                "detected": False,
                "confidence": 0.0,
                "payload_16bit": None,
                "payload_hex": None,
                "available": True,
                "threshold": threshold,
                "per_channel": per_channel,
                "reason": "detector could not run on this audio",
            }
        detected = bool(best["score"] >= threshold)
        return {
            "engine": self.id,
            "detected": detected,
            "confidence": best["score"],
            "payload_16bit": best["payload_16bit"],
            "payload_hex": best["payload_hex"],
            "bits": 16,
            "threshold": threshold,
            "available": True,
            "per_channel": per_channel,
            "note": (
                "Confidence is the detector score mean; the 0.5 threshold is preliminary and not "
                "calibrated to a false-positive rate."
            ),
        }

    # ------------------------------------------------------------------ verify
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
        if not detection.get("available", True):
            ok = False
        return {
            "check": "audioseal.detect",
            "ok": ok,
            "detail": {
                **detection,
                "expected_payload_16bit": expected,
                "payload_match": payload_match,
            },
        }

    def path(self) -> str | None:
        try:
            generator, _detector = self._model_paths()
            return str(generator.parent)
        except Exception:
            return None
