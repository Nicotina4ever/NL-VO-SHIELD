"""Ed25519-signed local receipts.

A receipt carries the *full* unique ID plus the 16-bit watermark payload as
separate fields (the watermark alone is far too small to be a global identity).
The signature is computed over canonical JSON; the public key is embedded so a
receipt is self-contained, but ``valid`` also requires it to match the signing
key of this installation (``receipts.verify`` with ``trust_mode='local'``).
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature

from . import __version__, keys
from .errors import ReceiptError
from .util import (
    atomic_write_text,
    canonical_json,
    full_id,
    json_safe,
    read_json,
    utc_now_iso,
)

RECEIPTS_DIRNAME = "receipts"


class ReceiptStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.directory = self.data_dir / RECEIPTS_DIRNAME
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ create
    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ReceiptError("receipt body must be an object")
        private_key = keys.load_receipt_private_key(self.data_dir)
        public_hex = keys.public_key_hex(private_key.public_key())
        receipt: dict[str, Any] = {
            "schema": "nlvs.receipt/1",
            "id": body.pop("id", None) or full_id(),
            "created_at": body.pop("created_at", None) or utc_now_iso(),
            "software": {
                "name": "NL VO Shield",
                "engine_version": __version__,
                **(body.pop("software", {}) or {}),
            },
        }
        receipt.update(json_safe(body))
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        signature = private_key.sign(canonical_json(unsigned))
        receipt["signature"] = {
            "alg": "ed25519",
            "key_id": keys.key_id(public_hex),
            "public_key_hex": public_hex,
            "signature_b64": base64.b64encode(signature).decode("ascii"),
        }
        with self._lock:
            path = self._path(receipt["id"])
            atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return receipt

    # ------------------------------------------------------------------ read
    def _path(self, receipt_id: str) -> Path:
        safe = "".join(ch for ch in str(receipt_id) if ch.isalnum() or ch in "-_.")
        if not safe:
            raise ReceiptError("invalid receipt id")
        return self.directory / f"{safe}.json"

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        path = self._path(receipt_id)
        data = read_json(path, default=None)
        return data if isinstance(data, dict) else None

    def list(self, *, limit: int = 200, include_body: bool = False) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            data = read_json(path, default=None)
            if not isinstance(data, dict):
                continue
            if include_body:
                receipts.append(data)
            else:
                receipts.append(
                    {
                        "id": data.get("id"),
                        "created_at": data.get("created_at"),
                        "status": data.get("status"),
                        "input": (data.get("input") or {}).get("path"),
                        "output": (data.get("output") or {}).get("path"),
                        "output_sha256": (data.get("output") or {}).get("sha256"),
                        "watermark": data.get("watermark"),
                        "c2pa_manifest_id": (data.get("c2pa") or {}).get("manifest_id"),
                        "file": str(path),
                    }
                )
        receipts.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return receipts[: max(1, int(limit))]

    def find_by_output_sha256(self, sha256: str) -> dict[str, Any] | None:
        for item in self.list(limit=10000):
            if item.get("output_sha256") == sha256:
                full = self.get(str(item.get("id")))
                if full:
                    return full
        return None

    # ------------------------------------------------------------------ verify
    def verify(self, receipt: dict[str, Any] | str, *, trust_mode: str = "local") -> dict[str, Any]:
        if isinstance(receipt, str):
            data = self.get(receipt)
            if data is None:
                return {"valid": False, "reason": f"receipt not found: {receipt}", "receipt": None}
            receipt = data
        if not isinstance(receipt, dict):
            return {"valid": False, "reason": "receipt must be an object", "receipt": None}
        signature = receipt.get("signature")
        if not isinstance(signature, dict):
            return {"valid": False, "reason": "receipt has no signature", "receipt": receipt}
        public_hex = signature.get("public_key_hex")
        signature_b64 = signature.get("signature_b64")
        if not isinstance(public_hex, str) or not isinstance(signature_b64, str):
            return {"valid": False, "reason": "receipt signature fields are malformed", "receipt": receipt}
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        try:
            public_key = keys.public_key_from_hex(public_hex)
            signature_bytes = base64.b64decode(signature_b64.encode("ascii"), validate=True)
            public_key.verify(signature_bytes, canonical_json(unsigned))
            signature_ok = True
            reason = "signature valid"
        except InvalidSignature:
            signature_ok = False
            reason = "signature does not match the receipt contents"
        except (ValueError, TypeError) as exc:
            signature_ok = False
            reason = f"receipt signature cannot be parsed: {type(exc).__name__}"
        key_trusted = False
        try:
            local_public = keys.load_receipt_public_key(self.data_dir)
            key_trusted = keys.public_key_hex(local_public) == public_hex
        except ReceiptError:
            key_trusted = False
        valid = bool(signature_ok and (key_trusted or trust_mode == "any"))
        if signature_ok and not key_trusted and trust_mode == "local":
            reason = "signature valid but the signing key is not this installation's trusted key"
        return {
            "valid": valid,
            "signature_valid": signature_ok,
            "key_trusted": key_trusted,
            "reason": reason,
            "receipt": receipt,
        }

    # ------------------------------------------------------------------ keys
    def trusted_public_key_hex(self) -> str:
        public_key = keys.load_receipt_public_key(self.data_dir)
        return keys.public_key_hex(public_key)
