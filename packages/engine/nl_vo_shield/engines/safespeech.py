"""SafeSpeech adapter placeholder — deliberately unavailable.

SafeSpeech is external research code (USENIX Security 2025) whose official
dataset/checkpoint pipeline differs from the legacy wrapper CLI. In this
environment the checkpoints are incomplete (only ``DUR_0.pth`` is present,
``D_0.pth`` is missing), so the engine must not pretend to run. It is reported
as ``unavailable`` with the real reason and is never downloaded automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import paths
from ..errors import EngineUnavailable
from .base import BaseEngine, EngineContext

_EXPECTED_WEIGHTS = (
    "checkpoints/base_models/DUR_0.pth",
    "checkpoints/base_models/D_0.pth",
)


class SafeSpeechEngine(BaseEngine):
    id = "safespeech"
    name = "SafeSpeech (research)"
    kind = "anti_clone"
    version = "external"
    capabilities = ["perturb"]
    requires = ["SafeSpeech research code", "complete official checkpoints (user-provided)"]
    requires_download = False
    quality_note = (
        "Not integrated: research pipeline and checkpoints are external. Never downloaded "
        "automatically; unavailable unless the user installs a complete checkpoint set."
    )

    description = (
        "SafeSpeech (USENIX Security 2025) research anti-cloning perturbation. External "
        "research code and checkpoints, never bundled or downloaded by NL VO Shield."
    )
    impact = "Would perturb the voice signal to reduce cloning fidelity (not validated here)."
    limitations = [
        "Not integrated in this build.",
        "Checkpoints are incomplete in this environment (D_0.pth missing).",
        "Requires a manual external installation by the user.",
    ]
    recommended_levels = ["strong"]
    incompatible_with = ["spectral_cloak"]

    def __init__(self, context: Any) -> None:
        self.context = context
        self._directory = paths.find_safespeech_dir()

    def _weight_status(self) -> tuple[list[str], list[str]]:
        if self._directory is None:
            return [], list(_EXPECTED_WEIGHTS)
        found, missing = [], []
        for relative in _EXPECTED_WEIGHTS:
            (found if (self._directory / relative).is_file() else missing).append(relative)
        return found, missing

    def availability(self) -> tuple[bool, str | None]:
        if self._directory is None:
            return False, (
                "SafeSpeech research code not found (expected under tools/SafeSpeech or set "
                "NLVS_SAFESPEECH_DIR). SafeSpeech is external research code and is never installed "
                "or downloaded automatically."
            )
        found, missing = self._weight_status()
        if missing:
            return False, (
                "SafeSpeech checkpoints are incomplete: found "
                + (", ".join(found) if found else "none")
                + "; missing "
                + ", ".join(missing)
                + ". The official research pipeline requires the complete checkpoint set; "
                "install it manually to enable this engine (no automatic download)."
            )
        return False, (
            "SafeSpeech checkpoint files are present but its official dataset/checkpoint pipeline "
            "is not integrated in this build. The engine is intentionally disabled rather than "
            "simulating protection."
        )

    def path(self) -> str | None:
        return str(self._directory) if self._directory else None

    def execute(self, action: str, buffer, params: dict[str, Any], ctx: EngineContext):
        _available, reason = self.availability()
        raise EngineUnavailable(reason or "SafeSpeech is unavailable")
