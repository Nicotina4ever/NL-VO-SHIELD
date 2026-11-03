"""NL VO Shield — Python engine core.

Local-first audio protection pipeline: anti-clone DSP perturbation, watermarking
(AudioSeal and a genuine DSP spread-spectrum fallback), C2PA provenance,
local fingerprinting, quality metrics and Ed25519-signed receipts.

The engine never downloads models implicitly and never writes outside its
data directory. All paths are resolved relative to the repository/package or
from environment variables; no absolute development paths are embedded.
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "ENGINE_NAME",
    "APP_ID",
]

ENGINE_NAME = "NL VO Shield Engine"
APP_ID = "nl-vo-shield"
__version__ = "1.2.3"

# JSON-RPC stdio protocol version implemented by nl_vo_shield.rpc
PROTOCOL_VERSION = 1








