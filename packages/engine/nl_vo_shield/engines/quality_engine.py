"""Quality measurement engine (objective metrics, no protection claim)."""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..audio_io import AudioBuffer
from ..quality import analyze as analyze_quality
from .base import BaseEngine, EngineContext


class QualityEngine(BaseEngine):
    id = "quality"
    name = "Audio Quality Metrics"
    kind = "quality"
    version = __version__
    capabilities = ["analyze", "compare"]
    requires = ["numpy", "scipy"]
    requires_download = False
    quality_note = (
        "Peak/true-peak/LUFS/clipping/SNR/spectral distance. Objective measurements only: not a "
        "substitute for a blind listening test."
    )

    def __init__(self, context: Any) -> None:
        self.context = context

    def analyze(self, buffer: AudioBuffer, reference: AudioBuffer | None = None,
                ctx: EngineContext | None = None) -> dict[str, Any]:
        return analyze_quality(buffer, reference)

    def execute(self, action: str, buffer: AudioBuffer, params: dict[str, Any], ctx: EngineContext):
        # Quality is not a transforming step in a protection chain.
        raise NotImplementedError(
            "quality is a measurement engine and cannot be used as a protection step"
        )

    def verify_effect(self, action: str, buffer: AudioBuffer, params: dict[str, Any],
                      ctx: EngineContext) -> dict[str, Any]:
        return {
            "check": "quality.analyze",
            "ok": True,
            "detail": self.analyze(buffer, None, ctx),
        }
