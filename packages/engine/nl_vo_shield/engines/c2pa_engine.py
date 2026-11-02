"""Real C2PA provenance via ``c2patool`` (sign + verify), certs in the data dir.

Validated with c2patool 0.9.12 on WAV audio. Important facts encoded here:

* the manifest definition references the signing key/cert by PATH, not PEM text;
* a signing certificate generated locally must carry a **critical**
  Extended Key Usage ``emailProtection`` (id-kp-emailProtection), otherwise
  c2patool 0.9.12 fails with "COSE error parsing certificate";
* signature validity and certificate trust are reported as separate facts:
  a local self-signed chain is cryptographically valid but not publicly trusted;
* tampering is detected through ``validation_status`` codes returned by
  ``c2patool -d`` (``assertion.dataHash.mismatch``).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .. import __version__
from ..errors import EngineUnavailable, ToolExecutionError
from ..util import (
    atomic_write_bytes,
    atomic_write_text,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now_iso,
)
from .base import BaseEngine, EngineContext

_C2PA_DIR = "c2pa"
_ROOT_CERT = "local-root-cert.pem"
_ROOT_KEY = "local-root-key.pem"
_LEAF_KEY = "signing-key.pem"
_LEAF_CHAIN = "signing-cert.pem"
_IDENTITY = "identity.json"

_TOOL_TIMEOUT = 180


def _org_name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NL VO Shield"),
        ]
    )


# ---------------------------------------------------------------------------
# Minimal DER helpers.
#
# c2patool 0.9.12 `--certs` re-encodes EC AlgorithmIdentifier values with an
# explicit NULL parameter, which strict parsers reject even though the DER is
# structurally valid. We therefore read the raw PEM payloads ourselves and only
# extract the SubjectPublicKeyInfo (the part we need to match a trust anchor).
# ---------------------------------------------------------------------------
def _read_tlv(data: bytes, offset: int) -> tuple[int, int, int]:
    if offset + 2 > len(data):
        raise ValueError("truncated DER")
    tag = data[offset]
    length = data[offset + 1]
    cursor = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or cursor + count > len(data):
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[cursor:cursor + count], "big")
        cursor += count
    end = cursor + length
    if end > len(data):
        raise ValueError("truncated DER content")
    return tag, cursor, end


def extract_spki(der: bytes) -> bytes:
    """Return the raw SubjectPublicKeyInfo TLV from an X.509 certificate DER."""
    tag, content_start, _content_end = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("certificate is not a SEQUENCE")
    tag, tbs_start, _tbs_end = _read_tlv(der, content_start)
    if tag != 0x30:
        raise ValueError("tbsCertificate is not a SEQUENCE")
    tag, _s, serial_end = _read_tlv(der, tbs_start)
    if tag == 0xA0:  # explicit version
        tag, _s, serial_end = _read_tlv(der, serial_end)
        if tag != 0x02:
            raise ValueError("serial is not an INTEGER")
    cursor = serial_end
    for _field in ("signature", "issuer", "validity", "subject"):
        _tag, _s, cursor = _read_tlv(der, cursor)
    spki_start = cursor
    tag, _s, spki_end = _read_tlv(der, cursor)
    if tag != 0x30:
        raise ValueError("subjectPublicKeyInfo is not a SEQUENCE")
    return der[spki_start:spki_end]


def _pem_certificate_der(blob: str) -> list[bytes]:
    import base64
    import re

    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", blob, re.S
    )
    certificates: list[bytes] = []
    for block in blocks:
        try:
            certificates.append(base64.b64decode("".join(block.split())))
        except (ValueError, TypeError):
            continue
    return certificates


def _key_usage(ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=not ca,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def _build_certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    signer_key,
    ca: bool,
    leaf: bool,
    issuer_public_key=None,
    days: int = 3650,
) -> x509.Certificate:
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(_key_usage(ca), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )
    if leaf:
        # c2patool 0.9.12 requires a critical EKU emailProtection on the signing
        # certificate; without it COSE signing fails ("COSE error parsing certificate").
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=True
        )
        if issuer_public_key is not None:
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_public_key),
                critical=False,
            )
    return builder.sign(signer_key, hashes.SHA256())


class C2PAEngine(BaseEngine):
    id = "c2pa"
    name = "C2PA Provenance"
    kind = "provenance"
    capabilities = ["sign", "verify"]
    requires = ["c2patool 0.9.x"]
    requires_download = False
    quality_note = (
        "Signature integrity and certificate trust are separate. The local self-signed chain is "
        "cryptographically valid but NOT publicly trusted; C2PA does not by itself establish "
        "legal identity."
    )

    description = (
        "C2PA Content Credentials: embeds a signed provenance manifest (software, title, "
        "author/usage claims, protection metadata) into the audio file via c2patool."
    )
    impact = (
        "Does not modify the audio samples: appends a signed manifest container. Signature "
        "integrity and certificate trust are reported separately."
    )
    limitations = [
        "The local self-signed certificate is not a public identity; trust requires a trust anchor.",
        "C2PA improves provenance/attribution, it does not prevent cloning.",
        "Requires c2patool 0.9.x (bundled or provided in the data directory).",
    ]
    recommended_levels = ["light", "balanced", "strong"]
    incompatible_with = []

    def __init__(self, context: Any) -> None:
        self.context = context
        self._version_cache: str | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ tool
    def _tool(self) -> Path:
        tool = self.context.c2patool
        if tool is None or not Path(tool).is_file():
            raise EngineUnavailable(
                "c2patool is not available. Install c2patool 0.9.x in bin/c2patool or in the "
                "application data directory (or set NLVS_C2PATOOL)."
            )
        # External tools always receive absolute paths.
        return Path(tool).expanduser().resolve()

    def availability(self) -> tuple[bool, str | None]:
        try:
            self._tool()
        except EngineUnavailable as exc:
            return False, str(exc)
        return True, None

    @property
    def version(self) -> str:  # type: ignore[override]
        if self._version_cache is not None:
            return self._version_cache
        try:
            tool = self._tool()
            result = subprocess.run(
                [str(tool), "--version"], capture_output=True, text=True, timeout=30, check=False
            )
            text = (result.stdout or result.stderr or "").strip().splitlines()
            self._version_cache = text[0].replace("c2patool", "").strip() if text else "unknown"
        except Exception:
            self._version_cache = "unknown"
        return self._version_cache

    def path(self) -> str | None:
        tool = self.context.c2patool
        return str(tool) if tool else None

    # ------------------------------------------------------------------ identity
    @property
    def c2pa_dir(self) -> Path:
        return Path(self.context.data_dir) / _C2PA_DIR

    def _identity_files(self) -> dict[str, Path]:
        directory = self.c2pa_dir
        return {
            "root_cert": directory / _ROOT_CERT,
            "root_key": directory / _ROOT_KEY,
            "leaf_key": directory / _LEAF_KEY,
            "leaf_chain": directory / _LEAF_CHAIN,
            "meta": directory / _IDENTITY,
        }

    def _generate_identity(self, alg: str) -> dict[str, Any]:
        files = self._identity_files()
        files["root_cert"].parent.mkdir(parents=True, exist_ok=True)
        if alg == "es256":
            root_key = ec.generate_private_key(ec.SECP256R1())
            leaf_key = ec.generate_private_key(ec.SECP256R1())
        elif alg == "ps256":
            root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            raise ValueError(f"Unsupported signing algorithm: {alg}")
        root_name = _org_name("NL VO Shield Local Root CA")
        leaf_name = _org_name("NL VO Shield Local Signing")
        root_cert = _build_certificate(
            subject=root_name,
            issuer=root_name,
            public_key=root_key.public_key(),
            signer_key=root_key,
            ca=True,
            leaf=False,
        )
        leaf_cert = _build_certificate(
            subject=leaf_name,
            issuer=root_name,
            public_key=leaf_key.public_key(),
            signer_key=root_key,
            ca=False,
            leaf=True,
            issuer_public_key=root_key.public_key(),
        )
        atomic_write_bytes(
            files["root_cert"], root_cert.public_bytes(serialization.Encoding.PEM)
        )
        atomic_write_bytes(
            files["root_key"],
            root_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        atomic_write_bytes(
            files["leaf_key"],
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        chain = leaf_cert.public_bytes(serialization.Encoding.PEM) + root_cert.public_bytes(
            serialization.Encoding.PEM
        )
        atomic_write_bytes(files["leaf_chain"], chain)
        for key in ("root_key", "leaf_key"):
            try:
                os.chmod(files[key], 0o600)
            except OSError:
                pass
        meta = {
            "schema": "nlvs.c2pa-identity/1",
            "alg": alg,
            "created_at": utc_now_iso(),
            "subject_cn": "NL VO Shield Local Signing",
            "issuer_cn": "NL VO Shield Local Root CA",
            "leaf_serial": str(leaf_cert.serial_number),
            "root_serial": str(root_cert.serial_number),
            "leaf_sha256": sha256_file(files["leaf_chain"]),
            "root_sha256": sha256_file(files["root_cert"]),
            "root_fingerprint_sha256": root_cert.fingerprint(hashes.SHA256()).hex(),
            "trust_note": (
                "Local self-signed chain: signature integrity can be verified, public trust "
                "cannot. Add local-root-cert.pem as a trust anchor to change that."
            ),
        }
        atomic_write_text(files["meta"], json.dumps(meta, indent=2, sort_keys=True))
        return meta

    def ensure_identity(self, alg: str = "es256") -> dict[str, Any]:
        files = self._identity_files()
        with self._lock:
            meta = read_json(files["meta"], default=None)
            if (
                isinstance(meta, dict)
                and meta.get("alg") == alg
                and all(files[key].is_file() for key in ("root_cert", "root_key", "leaf_key", "leaf_chain"))
            ):
                return meta
            return self._generate_identity(alg)

    def identity_info(self) -> dict[str, Any] | None:
        meta = read_json(self._identity_files()["meta"], default=None)
        return meta if isinstance(meta, dict) else None

    # ------------------------------------------------------------------ sign
    def _build_manifest(
        self,
        params: dict[str, Any],
        ctx: EngineContext,
        *,
        alg: str,
    ) -> dict[str, Any]:
        files = self._identity_files()
        options = dict(ctx.options or {})
        claims = dict(params.get("manifest") or {})
        title = str(
            params.get("title")
            or claims.get("title")
            or options.get("title")
            or "NL VO Shield protected audio"
        )
        author = str(claims.get("author") or options.get("author") or "Unknown")
        usage_terms = str(claims.get("usage_terms") or options.get("usage_terms") or "")
        copyright_notice = str(
            claims.get("copyright") or options.get("copyright") or "Protected with NL VO Shield"
        )
        protection = {
            "schema": "nlvs.protection/1",
            "tool": f"NL VO Shield {__version__}",
            "created_at": utc_now_iso(),
            "engine_chain": options.get("engine_chain") or [],
            "payload_16bit": int(ctx.payload) & 0xFFFF if ctx.payload is not None else None,
            "receipt_id": options.get("receipt_id"),
            "statement": (
                "Audio processed locally by NL VO Shield. C2PA signature integrity and certificate "
                "trust are separate facts; anti-cloning effectiveness is not claimed."
            ),
        }
        assertions: list[dict[str, Any]] = [
            {
                "label": "stds.schema-org.CreativeWork",
                "data": {
                    "@context": "https://schema.org",
                    "@type": "CreativeWork",
                    "author": [{"@type": "Person", "name": author}],
                    "usageTerms": usage_terms,
                    "copyrightNotice": copyright_notice,
                },
            },
            {
                "label": "c2pa.actions",
                "data": {"actions": [{"action": "c2pa.opened"}]},
            },
            {"label": "nlvo.shield.protection", "data": protection},
        ]
        return {
            "alg": alg,
            "private_key": str(files["leaf_key"].resolve()),
            "sign_cert": str(files["leaf_chain"].resolve()),
            "claim_generator": f"NL VO Shield/{__version__}",
            "title": title,
            "assertions": assertions,
        }

    def _run_sign(self, input_path: Path, manifest_path: Path, output_path: Path, work_dir: Path) -> subprocess.CompletedProcess:
        tool = self._tool()
        command = [
            str(tool),
            str(input_path),
            "-m",
            str(manifest_path),
            "-o",
            str(output_path),
            "-f",
        ]
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_TOOL_TIMEOUT,
                cwd=str(work_dir),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(
                f"c2patool could not be executed: {exc}", detail={"command": command}
            ) from exc

    def sign(self, input_path: Path, work_dir: Path, params: dict[str, Any], ctx: EngineContext):
        # Absolute paths are mandatory: c2patool resolves manifest key/cert paths
        # and the input relative to its own working directory.
        input_path = Path(input_path).expanduser().resolve()
        work_dir = Path(work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        if not input_path.is_file():
            raise ToolExecutionError(f"Input for C2PA signing not found: {input_path}")
        output_path = work_dir / f"c2pa_signed{input_path.suffix or '.wav'}"
        warnings: list[str] = []
        alg = "es256"
        identity = self.ensure_identity(alg)
        manifest = self._build_manifest(params, ctx, alg=alg)
        manifest_path = work_dir / f"c2pa_manifest_{os.getpid()}.json"
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        ctx.emit("c2pa", 10, "c2patool signing")
        with ctx.heartbeat("c2pa", 10, 70, "c2patool signing"):
            result = self._run_sign(input_path, manifest_path, output_path, work_dir)
        if result.returncode != 0 and self.ensure_identity(alg).get("alg") == "es256":
            stderr = (result.stderr or "").strip()
            if "COSE" in stderr or "certificate" in stderr.lower():
                # Older c2patool builds may reject EC certificates; retry with RSA.
                warnings.append(
                    "c2patool rejected the default ES256 identity; regenerated a PS256 local "
                    "identity and retried."
                )
                alg = "ps256"
                identity = self._generate_identity(alg)
                manifest = self._build_manifest(params, ctx, alg=alg)
                atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
                result = self._run_sign(input_path, manifest_path, output_path, work_dir)
        if result.returncode != 0 or not output_path.is_file():
            raise ToolExecutionError(
                "c2patool signing failed",
                detail={
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "").strip()[:2000],
                    "stdout": (result.stdout or "").strip()[:2000],
                },
            )
        try:
            summary = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            summary = {}
        manifest_id = summary.get("active_manifest")
        manifest_entry = (summary.get("manifests") or {}).get(manifest_id, {}) if manifest_id else {}
        signer = (manifest_entry.get("signature_info") or {}).get("issuer")
        cert_serial = (manifest_entry.get("signature_info") or {}).get("cert_serial_number")
        record = {
            "engine": self.id,
            "action": "sign",
            "version": self.version,
            "params": {
                "alg": alg,
                "title": manifest.get("title"),
                "manifest_id": manifest_id,
                "certificate": "local self-signed (data dir)",
                "timestamp_authority": None,
            },
            "detail": {
                "signer": signer,
                "cert_serial_number": cert_serial,
                "cert_trusted": False,
                "trust_note": (
                    "Signed with the local self-signed certificate chain. Integrity is verifiable; "
                    "public trust requires adding the local root as a trust anchor."
                ),
                "format": input_path.suffix.lstrip("."),
                "output_sha256": sha256_file(output_path),
                "warnings": warnings,
            },
        }
        ctx.emit("c2pa", 80, "C2PA manifest embedded; verifying")
        return output_path, record

    # ------------------------------------------------------------------ verify
    def _read_manifest(self, path: Path) -> dict[str, Any]:
        tool = self._tool()
        command = [str(tool), str(path), "-d"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=_TOOL_TIMEOUT, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(f"c2patool verify failed to start: {exc}") from exc
        stderr = (result.stderr or "").strip()
        if result.returncode != 0 or not (result.stdout or "").strip():
            return {"present": False, "reason": stderr or "no C2PA manifest found", "returncode": result.returncode}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"present": False, "reason": "c2patool returned non-JSON output"}
        if not isinstance(data, dict) or not data.get("manifests"):
            return {"present": False, "reason": "no manifest in c2patool output"}
        return {"present": True, "data": data}

    def _certificate_chain(self, path: Path) -> list[bytes]:
        """Return the embedded signing chain as raw DER certificates (leaf first)."""
        tool = self._tool()
        try:
            result = subprocess.run(
                [str(tool), "--certs", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        return _pem_certificate_der(result.stdout or "")

    def _trust(self, chain_der: list[bytes], *, signature_valid: bool) -> dict[str, Any]:
        local_root_spki: str | None = None
        try:
            root_cert = x509.load_pem_x509_certificate(
                (self.c2pa_dir / _ROOT_CERT).read_bytes()
            )
            local_root_spki = sha256_bytes(
                root_cert.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
        except (OSError, ValueError, AttributeError):
            local_root_spki = None
        entries: list[dict[str, Any]] = []
        root_matched = False
        for index, der in enumerate(chain_der):
            try:
                spki = extract_spki(der)
            except ValueError:
                entries.append({"index": index, "parsed": False,
                                "reason": "certificate could not be parsed"})
                continue
            spki_sha = sha256_bytes(spki)
            is_local_root = local_root_spki is not None and spki_sha == local_root_spki
            root_matched = root_matched or is_local_root
            entries.append(
                {
                    "index": index,
                    "parsed": True,
                    "der_sha256": sha256_bytes(der),
                    "spki_sha256": spki_sha,
                    "is_local_root": is_local_root,
                }
            )
        trusted = bool(root_matched and signature_valid)
        if trusted:
            reason = "certificate chain leads to this installation's local self-signed root"
        elif not chain_der:
            reason = "certificate chain not available from c2patool"
        elif not signature_valid:
            reason = "claim signature is not valid, so trust cannot be established"
        else:
            reason = "certificate chain does not contain this installation's local root"
        return {
            "trusted": trusted,
            "chain_valid": bool(signature_valid and root_matched),
            "root_matched_local_anchor": root_matched,
            "chain": entries,
            "reason": reason,
            "trust_anchor": (
                "local self-signed root (NL VO Shield data dir)"
                if trusted
                else "no configured public trust anchor"
            ),
            "note": (
                "Signature integrity and certificate trust are separate facts. A locally trusted "
                "self-signed chain is not a public CA identity. c2patool re-encodes EC "
                "AlgorithmIdentifier values with an explicit NULL, so chain certificates are "
                "compared by SubjectPublicKeyInfo fingerprint instead of a strict re-parse."
            ),
        }

    def verify(self, path: Path) -> dict[str, Any]:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            return {"present": False, "valid": False, "reason": f"file not found: {path}"}
        read = self._read_manifest(path)
        if not read.get("present"):
            return {
                "present": False,
                "valid": False,
                "reason": read.get("reason"),
                "tool_version": self.version,
            }
        data = read["data"]
        manifest_id = data.get("active_manifest")
        manifests = data.get("manifests") or {}
        manifest = manifests.get(manifest_id) or next(iter(manifests.values()), {})
        validation = data.get("validation_status") or []
        codes = [str(item.get("code", "")) for item in validation]
        signature_valid = "claimSignature.validated" in codes
        data_hash_ok = "assertion.dataHash.match" in codes
        hashed_uri_ok = "assertion.hashedURI.match" in codes
        mismatch = any(
            "mismatch" in code.lower() or code.lower().endswith(".error") or "invalid" in code.lower()
            for code in codes
        )
        valid = bool(signature_valid and (data_hash_ok or hashed_uri_ok) and not mismatch)
        claim = manifest.get("claim") or {}
        assertion_store = manifest.get("assertion_store") or {}
        creative_work = assertion_store.get("stds.schema-org.CreativeWork") or {}
        protection = assertion_store.get("nlvo.shield.protection") or {}
        actions = (assertion_store.get("c2pa.actions") or {}).get("actions") or []
        chain = self._certificate_chain(path)
        trust = self._trust(chain, signature_valid=signature_valid)
        signer = (manifest.get("signature") or {}).get("issuer") or (
            manifest.get("signature_info") or {}
        ).get("issuer")
        return {
            "present": True,
            "valid": valid,
            "signature_valid": signature_valid,
            "data_hash_valid": data_hash_ok,
            "tampered": bool(mismatch),
            "signer": signer,
            "trusted": bool(trust.get("trusted")),
            "trust": trust,
            "manifest_id": manifest_id,
            "title": claim.get("dc:title"),
            "format": claim.get("dc:format"),
            "instance_id": claim.get("instanceID"),
            "actions": actions,
            "claims": {
                "creative_work": creative_work,
                "protection": protection,
            },
            "validation_status": validation,
            "validation_codes": codes,
            "certificate_chain": trust.get("chain", []),
            "tool_version": self.version,
            "note": (
                "valid=true means the claim signature and asset hash verified; trusted is reported "
                "separately and is false for a local self-signed certificate without a public trust "
                "anchor."
            ),
        }

    def verify_effect(self, action: str, buffer: Any, params: dict[str, Any],
                      ctx: EngineContext) -> dict[str, Any]:
        path = params.get("_signed_path")
        if path is None:
            return {
                "check": "c2pa.verify",
                "ok": False,
                "detail": {"reason": "signed file path missing"},
            }
        result = self.verify(Path(path))
        ok = bool(result.get("present") and result.get("valid") and result.get("data_hash_valid"))
        return {"check": "c2pa.verify", "ok": ok, "detail": result}

