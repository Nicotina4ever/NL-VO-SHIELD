"""Local perceptual-ish fingerprint (log band-energy profile + temporal spread).

Purpose: link a verified file back to a locally stored receipt even when the
audio was re-encoded (different bytes). It is NOT a global identity and it is
NOT a speaker-recognition model: similarity is a cosine distance on a compact
spectral profile, validated only on the test material used during development.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .. import __version__
from ..audio_io import AudioBuffer
from .base import BaseEngine, EngineContext

_BANDS = 32
_MIN_HZ = 80.0
_MAX_HZ = 8000.0
DEFAULT_MATCH_THRESHOLD = 0.92


def _band_matrix(sample_rate: int, n_fft: int) -> list[tuple[int, int]]:
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    upper = min(_MAX_HZ, sample_rate / 2.0 * 0.95)
    edges = np.geomspace(_MIN_HZ, max(upper, _MIN_HZ * 2), _BANDS + 1)
    ranges: list[tuple[int, int]] = []
    for index in range(_BANDS):
        low = int(np.searchsorted(freqs, edges[index], side="left"))
        high = int(np.searchsorted(freqs, edges[index + 1], side="right"))
        if high <= low:
            high = min(low + 1, len(freqs))
        ranges.append((low, high))
    return ranges


def _frame_magnitudes(mono: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    window = np.hanning(n_fft + 1)[:-1]
    if mono.size < n_fft:
        mono = np.pad(mono, (0, n_fft - mono.size))
    frames = np.lib.stride_tricks.sliding_window_view(mono, n_fft)[::hop]
    return np.abs(np.fft.rfft(frames * window, axis=1))


class FingerprintEngine(BaseEngine):
    id = "fingerprint"
    name = "Local Spectral Fingerprint"
    kind = "fingerprint"
    version = __version__
    capabilities = ["compute", "match"]
    requires = ["numpy"]
    requires_download = False
    quality_note = (
        "Compact spectral profile for local receipt matching. Not a speaker-identity model; "
        "similarity to an unrelated recording can only be excluded empirically."
    )

    def __init__(self, context: Any) -> None:
        self.context = context

    def compute(self, buffer: AudioBuffer, ctx: EngineContext | None = None) -> dict[str, Any]:
        sample_rate = buffer.sample_rate
        n_fft = 1024 if buffer.frames >= 1024 else 512
        hop = n_fft // 2
        mono = buffer.mono()
        magnitude = _frame_magnitudes(mono, n_fft, hop)
        ranges = _band_matrix(sample_rate, n_fft)
        energies = np.zeros((magnitude.shape[0], _BANDS), dtype=np.float64)
        for index, (low, high) in enumerate(ranges):
            low = min(low, magnitude.shape[1] - 1)
            high = min(max(high, low + 1), magnitude.shape[1])
            energies[:, index] = np.sum(np.square(magnitude[:, low:high]), axis=1)
        log_energies = np.log10(energies + 1e-10)
        mean = log_energies.mean(axis=0)
        spread = log_energies.std(axis=0)
        vector = np.concatenate([mean, spread])
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            vector = np.zeros_like(vector)
        else:
            vector = vector / norm
        return {
            "algorithm": "nlvs-spectral-fp/1",
            "dimensions": int(vector.size),
            "bands": _BANDS,
            "band_range_hz": [_MIN_HZ, min(_MAX_HZ, sample_rate / 2.0 * 0.95)],
            "vector": [round(float(value), 8) for value in vector],
            "sample_rate": sample_rate,
            "duration_seconds": round(buffer.duration, 3),
            "note": "Cosine similarity on a 64-dim spectral profile; not a global identity.",
        }

    @staticmethod
    def similarity(vector_a: list[float] | np.ndarray, vector_b: list[float] | np.ndarray) -> float:
        a = np.asarray(vector_a, dtype=np.float64)
        b = np.asarray(vector_b, dtype=np.float64)
        if a.size == 0 or b.size == 0 or a.size != b.size:
            return 0.0
        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denominator <= 1e-12:
            return 0.0
        return float(np.clip(np.dot(a, b) / denominator, -1.0, 1.0))

    def find_best(
        self,
        buffer: AudioBuffer,
        receipts: list[dict[str, Any]],
        *,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> dict[str, Any]:
        current = self.compute(buffer)
        current_vector = current["vector"]
        candidates: list[dict[str, Any]] = []
        for receipt in receipts:
            fingerprint = receipt.get("fingerprint") or {}
            vector = fingerprint.get("vector") if isinstance(fingerprint, dict) else None
            if not vector:
                continue
            score = self.similarity(current_vector, vector)
            candidates.append(
                {
                    "receipt_id": receipt.get("id"),
                    "similarity": round(score, 6),
                    "matched": score >= threshold,
                }
            )
        candidates.sort(key=lambda item: item["similarity"], reverse=True)
        best = candidates[0] if candidates else None
        return {
            "algorithm": current["algorithm"],
            "threshold": threshold,
            "matched": bool(best and best["similarity"] >= threshold),
            "receipt_id": best["receipt_id"] if best and best["similarity"] >= threshold else None,
            "similarity": best["similarity"] if best else None,
            "candidates": candidates[:5],
            "note": (
                "Local heuristic match; verify the receipt signature and the watermark for "
                "confirmation. Similarity alone is not proof of authorship."
            ),
        }
