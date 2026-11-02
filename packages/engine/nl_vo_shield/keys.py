"""Local key material: Ed25519 receipt signing and the watermark secret.

Keys are generated on first use in ``<data_dir>/keys`` and never leave the
machine. They are never written to the repository and never embedded in a
receipt (only the public key is).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .errors import ReceiptError
from .util import atomic_write_bytes, sha256_bytes

KEYS_DIRNAME = "keys"
RECEIPT_PRIVATE = "receipt-signing-key.pem"
RECEIPT_PUBLIC = "receipt-signing-public.pem"
WATERMARK_KEY = "watermark.key"


def keys_dir(data_dir: str | Path) -> Path:
    path = Path(data_dir) / KEYS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows: best effort; the data dir is per-user anyway.
        pass


def ensure_receipt_keypair(data_dir: str | Path) -> tuple[Path, Path]:
    """Create (or load) the Ed25519 receipt signing key pair."""
    directory = keys_dir(data_dir)
    private_path = directory / RECEIPT_PRIVATE
    public_path = directory / RECEIPT_PUBLIC
    if private_path.is_file() and public_path.is_file():
        return private_path, public_path
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    atomic_write_bytes(private_path, private_pem)
    _restrict(private_path)
    atomic_write_bytes(public_path, public_pem)
    return private_path, public_path


def load_receipt_private_key(data_dir: str | Path) -> Ed25519PrivateKey:
    private_path, _public = ensure_receipt_keypair(data_dir)
    try:
        return serialization.load_pem_private_key(private_path.read_bytes(), password=None)  # type: ignore[return-value]
    except (OSError, ValueError) as exc:
        raise ReceiptError(
            f"Cannot load the receipt signing key: {private_path}: {exc}",
            code="RECEIPT_KEY_INVALID",
        ) from exc


def load_receipt_public_key(data_dir: str | Path) -> Ed25519PublicKey:
    _private, public_path = ensure_receipt_keypair(data_dir)
    try:
        return serialization.load_pem_public_key(public_path.read_bytes())  # type: ignore[return-value]
    except (OSError, ValueError) as exc:
        raise ReceiptError(
            f"Cannot load the receipt public key: {public_path}: {exc}",
            code="RECEIPT_KEY_INVALID",
        ) from exc


def public_key_hex(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw.hex()


def key_id(public_hex: str) -> str:
    return sha256_bytes(bytes.fromhex(public_hex))[:16]


def public_key_from_hex(value: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ReceiptError("Receipt public key is not valid hex", code="RECEIPT_KEY_INVALID") from exc
    if len(raw) != 32:
        raise ReceiptError("Receipt public key has an unexpected length", code="RECEIPT_KEY_INVALID")
    return Ed25519PublicKey.from_public_bytes(raw)


def watermark_secret(data_dir: str | Path) -> bytes:
    """32-byte secret used by the DSP spread-spectrum watermark."""
    path = keys_dir(data_dir) / WATERMARK_KEY
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReceiptError(f"Cannot read watermark key: {exc}") from exc
        if len(data) == 32:
            return data
        # Corrupt/truncated key: regenerate explicitly rather than silently
        # producing undetectable watermarks.
        path.unlink(missing_ok=True)
    data = os.urandom(32)
    atomic_write_bytes(path, data)
    _restrict(path)
    return data
