from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .encoding import atomic_json, b64d, b64e, canonical_pack
from .identity import Identity, PeerCard
from .packet import now_ms
from .store import PacketStore


PREKEY_BUNDLE_VERSION = 2
SUPPORTED_PREKEY_BUNDLE_VERSIONS = frozenset({1, 2})
MAX_PREKEYS_PER_BUNDLE = 1000


def derive_prekey_id(public_key: bytes) -> str:
    public_key = bytes(public_key)
    if len(public_key) != 32:
        raise ValueError("prekey public key must be 32 bytes")
    return hashlib.blake2s(
        public_key, digest_size=16, person=b"anet-opk"
    ).hexdigest()


@dataclass(frozen=True)
class PreKey:
    prekey_id: str
    public_key: bytes

    def validate(self) -> None:
        if self.prekey_id != derive_prekey_id(self.public_key):
            raise ValueError("prekey ID does not match public key")

    def to_dict(self) -> dict[str, str]:
        return {"prekey_id": self.prekey_id, "public_key": b64e(self.public_key)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PreKey":
        key = cls(
            prekey_id=str(value["prekey_id"]).strip().lower(),
            public_key=b64d(str(value["public_key"])),
        )
        key.validate()
        return key


@dataclass(frozen=True)
class PreKeyBundle:
    node_id: str
    intended_peer_id: str
    generation: int
    created_ms: int
    expires_ms: int
    keys: tuple[PreKey, ...]
    signature: bytes
    version: int = PREKEY_BUNDLE_VERSION

    def signing_fields(self) -> list[Any]:
        keys = [[key.prekey_id, key.public_key] for key in self.keys]
        if self.version == 1:
            return [
                self.version,
                self.node_id,
                self.generation,
                self.created_ms,
                self.expires_ms,
                keys,
            ]
        return [
            self.version,
            self.node_id,
            self.intended_peer_id,
            self.generation,
            self.created_ms,
            self.expires_ms,
            keys,
        ]

    @property
    def bundle_hash(self) -> str:
        return hashlib.sha256(
            canonical_pack([self.signing_fields(), self.signature])
        ).hexdigest()

    def verify(
        self,
        card: PeerCard,
        *,
        recipient_node_id: str = "",
        require_unexpired: bool = True,
    ) -> None:
        card.verify()
        if self.version not in SUPPORTED_PREKEY_BUNDLE_VERSIONS:
            raise ValueError(f"unsupported prekey bundle version: {self.version}")
        if self.node_id != card.node_id:
            raise ValueError("prekey bundle belongs to a different peer")
        if self.version >= 2:
            if not self.intended_peer_id:
                raise ValueError("prekey bundle has no intended peer")
            if recipient_node_id and self.intended_peer_id != recipient_node_id:
                raise ValueError("prekey bundle is intended for a different peer")
        elif self.intended_peer_id:
            raise ValueError("legacy prekey bundle cannot name an intended peer")
        if self.generation < 1:
            raise ValueError("prekey generation must be positive")
        if self.created_ms < 1 or self.expires_ms <= self.created_ms:
            raise ValueError("invalid prekey bundle lifetime")
        if require_unexpired and self.expires_ms <= now_ms():
            raise ValueError("prekey bundle is expired")
        if not self.keys or len(self.keys) > MAX_PREKEYS_PER_BUNDLE:
            raise ValueError("prekey bundle must contain 1 to 1000 keys")
        seen: set[str] = set()
        for key in self.keys:
            key.validate()
            if key.prekey_id in seen:
                raise ValueError("duplicate prekey ID in bundle")
            seen.add(key.prekey_id)
        if not self.signature:
            raise ValueError("prekey bundle is unsigned")
        Ed25519PublicKey.from_public_bytes(card.sign_public).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "node_id": self.node_id,
            "intended_peer_id": self.intended_peer_id,
            "generation": self.generation,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "keys": [key.to_dict() for key in self.keys],
            "signature": b64e(self.signature),
            "bundle_hash": self.bundle_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PreKeyBundle":
        bundle = cls(
            version=int(value.get("version", PREKEY_BUNDLE_VERSION)),
            node_id=str(value["node_id"]),
            intended_peer_id=str(value.get("intended_peer_id", "")),
            generation=int(value["generation"]),
            created_ms=int(value["created_ms"]),
            expires_ms=int(value["expires_ms"]),
            keys=tuple(PreKey.from_dict(item) for item in value["keys"]),
            signature=b64d(str(value["signature"])),
        )
        expected_hash = str(value.get("bundle_hash", "")).strip().lower()
        if expected_hash and expected_hash != bundle.bundle_hash:
            raise ValueError("prekey bundle hash mismatch")
        return bundle

    @classmethod
    def load(cls, path: Path) -> "PreKeyBundle":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


def _signed_bundle(
    identity: Identity,
    *,
    peer_id: str,
    generation: int,
    created_ms: int,
    expires_ms: int,
    keys: tuple[PreKey, ...],
) -> PreKeyBundle:
    unsigned = PreKeyBundle(
        node_id=identity.node_id,
        intended_peer_id=peer_id,
        generation=generation,
        created_ms=created_ms,
        expires_ms=expires_ms,
        keys=keys,
        signature=b"",
    )
    return PreKeyBundle(
        **{
            **unsigned.__dict__,
            "signature": identity.sign(canonical_pack(unsigned.signing_fields())),
        }
    )


def generate_prekey_bundle(
    identity: Identity,
    store: PacketStore,
    *,
    peer_id: str,
    count: int = 100,
    ttl_ms: int = 30 * 86400 * 1000,
    created_ms: int | None = None,
) -> PreKeyBundle:
    count = int(count)
    ttl_ms = int(ttl_ms)
    if count < 1 or count > MAX_PREKEYS_PER_BUNDLE:
        raise ValueError("prekey count must be between 1 and 1000")
    if ttl_ms < 60_000:
        raise ValueError("prekey lifetime must be at least one minute")
    created_ms = now_ms() if created_ms is None else int(created_ms)
    expires_ms = created_ms + ttl_ms
    peer_id = str(peer_id).strip()
    if not peer_id.startswith("an1"):
        raise ValueError("invalid intended peer ID")
    generation = store.next_local_prekey_generation(peer_id)
    public_keys: list[PreKey] = []
    local_keys: list[dict[str, Any]] = []
    for _ in range(count):
        private = X25519PrivateKey.generate()
        public_raw = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        private_raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        prekey_id = derive_prekey_id(public_raw)
        public_keys.append(PreKey(prekey_id=prekey_id, public_key=public_raw))
        local_keys.append(
            {
                "prekey_id": prekey_id,
                "public_key": public_raw,
                "private_key": private_raw,
            }
        )
    bundle = _signed_bundle(
        identity,
        peer_id=peer_id,
        generation=generation,
        created_ms=created_ms,
        expires_ms=expires_ms,
        keys=tuple(public_keys),
    )
    # Persist private material before the public bundle can be exported.
    store.add_local_prekey_batch(
        local_keys,
        peer_id=peer_id,
        generation=generation,
        created_ms=created_ms,
        expires_ms=expires_ms,
    )
    return bundle


def load_local_prekey_bundle(
    identity: Identity,
    store: PacketStore,
    *,
    peer_id: str,
    generation: int | None = None,
) -> PreKeyBundle | None:
    batch = store.local_prekey_public_batch(peer_id, generation)
    if batch is None:
        return None
    keys = tuple(
        PreKey(
            prekey_id=str(item["prekey_id"]),
            public_key=bytes(item["public_key"]),
        )
        for item in batch["keys"]
    )
    return _signed_bundle(
        identity,
        peer_id=peer_id,
        generation=int(batch["generation"]),
        created_ms=int(batch["created_ms"]),
        expires_ms=int(batch["expires_ms"]),
        keys=keys,
    )


def import_prekey_bundle(
    bundle: PreKeyBundle,
    card: PeerCard,
    store: PacketStore,
    *,
    recipient_node_id: str,
) -> dict[str, Any]:
    bundle.verify(card, recipient_node_id=recipient_node_id)
    return store.import_peer_prekey_bundle(
        card.node_id,
        [
            {"prekey_id": key.prekey_id, "public_key": key.public_key}
            for key in bundle.keys
        ],
        bundle_version=bundle.version,
        intended_peer_id=bundle.intended_peer_id,
        generation=bundle.generation,
        bundle_hash=bundle.bundle_hash,
        created_ms=bundle.created_ms,
        expires_ms=bundle.expires_ms,
    )
