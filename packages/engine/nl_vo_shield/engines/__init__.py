"""Engine implementations and the registry builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import EngineRegistry

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext


def build_registry(context: "AppContext") -> EngineRegistry:
    """Instantiate every known engine, available or not (honest statuses)."""
    from .audioseal_engine import AudioSealEngine
    from .c2pa_engine import C2PAEngine
    from .fingerprint import FingerprintEngine
    from .enkidu import EnkiduEngine
    from .e2e_vguard import E2EVGuardEngine
    from .mimic_blocker import MimicBlockerEngine
    from .quality_engine import QualityEngine
    from .safespeech import SafeSpeechEngine
    from .spectral_cloak import SpectralCloakEngine
    from .watermark_dsp import DspWatermarkEngine

    registry = EngineRegistry()
    for engine in (
        AudioSealEngine(context),
        DspWatermarkEngine(context),
        SpectralCloakEngine(context),
        C2PAEngine(context),
        FingerprintEngine(context),
        QualityEngine(context),
        MimicBlockerEngine(context),
        EnkiduEngine(context),
        E2EVGuardEngine(context),
        SafeSpeechEngine(context),
    ):
        registry.register(engine)
    return registry
