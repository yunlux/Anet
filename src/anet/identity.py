from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.x509.oid import NameOID

from .encoding import atomic_json, atomic_write, b64d, b64e, canonical_pack
from .locator import parse_locator


CARD_VERSION = 1
IDENTITY_VERSION = 1


def derive_node_id(sign_public: bytes, box_public: bytes) -> str:
    # Wire-domain v1 is immutable: changing this branding-era label would
    # silently change every existing node ID for the same key pair.
    digest = hashlib.blake2s(
        sign_public + box_public, digest_size=20, person=b"ainet-id"
    ).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"an1{token}"


@dataclass(frozen=True)
class PeerCard:
    node_id: str
    sign_public: bytes
    box_public: bytes
    label: str
    addresses: tuple[str, ...]
    capabilities: tuple[str, ...]
    created_ms: int
    signature: bytes
    version: int = CARD_VERSION

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.node_id,
            self.sign_public,
            self.box_public,
            self.label,
            sorted(self.addresses),
            sorted(self.capabilities),
            self.created_ms,
        ]

    def verify(self) -> None:
        if self.version != CARD_VERSION:
            raise ValueError(f"unsupported peer card version: {self.version}")
        expected = derive_node_id(self.sign_public, self.box_public)
        if self.node_id != expected:
            raise ValueError("peer card node id does not match public keys")
        if not self.signature:
            raise ValueError("peer card is unsigned")
        Ed25519PublicKey.from_public_bytes(self.sign_public).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )
        for address in self.addresses:
            parse_locator(address)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "node_id": self.node_id,
            "sign_public": b64e(self.sign_public),
            "box_public": b64e(self.box_public),
            "label": self.label,
            "addresses": list(self.addresses),
            "capabilities": list(self.capabilities),
            "created_ms": self.created_ms,
            "signature": b64e(self.signature),
        }

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": self.version,
            "id": self.node_id,
            "spk": self.sign_public,
            "bpk": self.box_public,
            "label": self.label,
            "addr": list(self.addresses),
            "caps": list(self.capabilities),
            "ts": self.created_ms,
            "sig": self.signature,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PeerCard":
        card = cls(
            version=int(value.get("version", CARD_VERSION)),
            node_id=str(value["node_id"]),
            sign_public=b64d(str(value["sign_public"])),
            box_public=b64d(str(value["box_public"])),
            label=str(value.get("label", "")),
            addresses=tuple(str(item) for item in value.get("addresses", [])),
            capabilities=tuple(str(item) for item in value.get("capabilities", [])),
            created_ms=int(value.get("created_ms", 0)),
            signature=b64d(str(value["signature"])),
        )
        card.verify()
        return card

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "PeerCard":
        card = cls(
            version=int(value.get("v", CARD_VERSION)),
            node_id=str(value["id"]),
            sign_public=bytes(value["spk"]),
            box_public=bytes(value["bpk"]),
            label=str(value.get("label", "")),
            addresses=tuple(str(item) for item in value.get("addr", [])),
            capabilities=tuple(str(item) for item in value.get("caps", [])),
            created_ms=int(value.get("ts", 0)),
            signature=bytes(value["sig"]),
        )
        card.verify()
        return card

    @classmethod
    def load(cls, path: Path) -> "PeerCard":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


class Identity:
    def __init__(
        self,
        *,
        label: str,
        sign_private: Ed25519PrivateKey,
        box_private: X25519PrivateKey,
        created_ms: int,
    ) -> None:
        self.label = label
        self.sign_private = sign_private
        self.box_private = box_private
        self.created_ms = int(created_ms)

    @property
    def sign_public(self) -> bytes:
        return self.sign_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def box_public(self) -> bytes:
        return self.box_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def node_id(self) -> str:
        return derive_node_id(self.sign_public, self.box_public)

    @classmethod
    def generate(cls, label: str) -> "Identity":
        clean = str(label or "node").strip()[:64]
        return cls(
            label=clean,
            sign_private=Ed25519PrivateKey.generate(),
            box_private=X25519PrivateKey.generate(),
            created_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )

    def sign(self, raw: bytes) -> bytes:
        return self.sign_private.sign(raw)

    def card(
        self,
        *,
        addresses: list[str] | tuple[str, ...] = (),
        capabilities: list[str] | tuple[str, ...] = (),
    ) -> PeerCard:
        unsigned = PeerCard(
            node_id=self.node_id,
            sign_public=self.sign_public,
            box_public=self.box_public,
            label=self.label,
            addresses=tuple(sorted(set(str(item) for item in addresses if str(item)))),
            capabilities=tuple(sorted(set(str(item) for item in capabilities if str(item)))),
            created_ms=self.created_ms,
            signature=b"",
        )
        return PeerCard(**{**unsigned.__dict__, "signature": self.sign(canonical_pack(unsigned.signing_fields()))})

    def to_private_dict(self) -> dict[str, Any]:
        sign_raw = self.sign_private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        box_raw = self.box_private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return {
            "version": IDENTITY_VERSION,
            "label": self.label,
            "created_ms": self.created_ms,
            "node_id": self.node_id,
            "sign_private": b64e(sign_raw),
            "box_private": b64e(box_raw),
        }

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_private_dict(), private=True)

    @classmethod
    def load(cls, path: Path) -> "Identity":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(value.get("version", 0)) != IDENTITY_VERSION:
            raise ValueError("unsupported identity version")
        identity = cls(
            label=str(value.get("label", "node")),
            created_ms=int(value["created_ms"]),
            sign_private=Ed25519PrivateKey.from_private_bytes(b64d(str(value["sign_private"]))),
            box_private=X25519PrivateKey.from_private_bytes(b64d(str(value["box_private"]))),
        )
        if value.get("node_id") != identity.node_id:
            raise ValueError("identity file node id mismatch")
        return identity

    def ensure_tls_material(self, directory: Path) -> tuple[Path, Path, bytes]:
        directory = Path(directory)
        cert_path = directory / "tls-cert.pem"
        key_path = directory / "tls-key.pem"
        if not cert_path.exists() or not key_path.exists():
            now = datetime.now(timezone.utc)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.node_id)])
            tls_private = Ed25519PrivateKey.generate()
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(tls_private.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(tls_private, algorithm=None)
            )
            key_pem = tls_private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            atomic_write(key_path, key_pem, private=True)
            atomic_write(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(common_names) != 1 or common_names[0].value != self.node_id:
            raise ValueError("TLS certificate does not belong to this node")
        tls_private = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        certificate_public = certificate.public_key()
        if not isinstance(tls_private, Ed25519PrivateKey) or not isinstance(
            certificate_public, Ed25519PublicKey
        ):
            raise ValueError("TLS material must use Ed25519")
        private_public_raw = tls_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        certificate_public_raw = certificate_public.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if private_public_raw != certificate_public_raw:
            raise ValueError("TLS certificate and private key do not match")
        fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).digest()
        return cert_path, key_path, fingerprint
