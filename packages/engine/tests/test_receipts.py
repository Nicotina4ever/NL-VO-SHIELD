"""Ed25519 receipts: full ID, payload separation, signature integrity."""

from __future__ import annotations

from pathlib import Path

from nl_vo_shield import keys


def _body() -> dict:
    return {
        "status": "protected",
        "job_id": "job-test",
        "input": {"path": "/tmp/in.wav", "sha256": "a" * 64},
        "output": {"path": "/tmp/out.wav", "sha256": "b" * 64, "partial": False},
        "watermark": {"engine": "watermark_dsp", "payload_16bit": 6699,
                      "payload_hex": "1a2b", "bits": 16},
        "fingerprint": {"algorithm": "nlvs-spectral-fp/1", "vector": [0.1, 0.2]},
    }


def test_receipt_roundtrip(ctx) -> None:
    receipt = ctx.receipts.create(_body())
    assert len(receipt["id"]) == 36  # full UUID, not truncated
    assert receipt["watermark"]["payload_16bit"] == 6699
    assert receipt["watermark"]["payload_hex"] == "1a2b"
    verification = ctx.receipts.verify(receipt["id"])
    assert verification["valid"] is True
    assert verification["signature_valid"] is True
    assert verification["key_trusted"] is True
    assert ctx.receipts.get(receipt["id"]) is not None
    listing = ctx.receipts.list()
    assert any(item["id"] == receipt["id"] for item in listing)


def test_tampered_receipt_is_invalid(ctx) -> None:
    receipt = ctx.receipts.create(_body())
    receipt["status"] = "partial"
    verification = ctx.receipts.verify(receipt)
    assert verification["valid"] is False
    assert verification["signature_valid"] is False


def test_tampered_signature_is_invalid(ctx) -> None:
    receipt = ctx.receipts.create(_body())
    signature = receipt["signature"]["signature_b64"]
    receipt["signature"]["signature_b64"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    verification = ctx.receipts.verify(receipt)
    assert verification["valid"] is False


def test_unknown_receipt_not_found(ctx) -> None:
    verification = ctx.receipts.verify("does-not-exist")
    assert verification["valid"] is False
    assert "not found" in verification["reason"]


def test_receipt_from_another_key_is_untrusted(tmp_path, ctx) -> None:
    # Create a receipt with a different data dir (different signing key).
    from nl_vo_shield.context import AppContext

    other = AppContext(tmp_path / "other-appdata")
    foreign = other.receipts.create(_body())
    verification = ctx.receipts.verify(foreign)
    assert verification["signature_valid"] is True
    assert verification["key_trusted"] is False
    assert verification["valid"] is False  # local trust mode
    trust_any = ctx.receipts.verify(foreign, trust_mode="any")
    assert trust_any["valid"] is True


def test_private_key_is_not_embedded_in_receipt(ctx) -> None:
    receipt = ctx.receipts.create(_body())
    private_path, public_path = keys.ensure_receipt_keypair(ctx.data_dir)
    private_text = private_path.read_text(encoding="utf-8")
    assert "BEGIN PRIVATE KEY" in private_text
    assert "BEGIN PRIVATE KEY" not in str(receipt)
    assert "public_key_hex" in receipt["signature"]
    # Only the public key (32 bytes) is embedded.
    assert len(bytes.fromhex(receipt["signature"]["public_key_hex"])) == 32


def test_find_by_output_sha256(ctx) -> None:
    receipt = ctx.receipts.create(_body())
    found = ctx.receipts.find_by_output_sha256("b" * 64)
    assert found is not None and found["id"] == receipt["id"]
    assert ctx.receipts.find_by_output_sha256("c" * 64) is None
