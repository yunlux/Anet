from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .encoding import atomic_json, b64d, b64e, canonical_pack
from .identity import Identity, PeerCard


PAIRING_VERSION = 1
OFFER_TYPE = "anet.pair.offer.v1"
RESPONSE_TYPE = "anet.pair.response.v1"
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_OFFER_TTL_MS = 7 * 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_window(created_ms: int, expires_ms: int, *, now: int) -> None:
    if created_ms <= 0 or expires_ms <= created_ms:
        raise ValueError("invalid pairing validity window")
    if expires_ms - created_ms > MAX_OFFER_TTL_MS:
        raise ValueError("pairing validity window is too long")
    if created_ms > now + MAX_CLOCK_SKEW_MS:
        raise ValueError("pairing object was created too far in the future")
    if expires_ms < now:
        raise ValueError("pairing object has expired")


@dataclass(frozen=True)
class PairOffer:
    offer_id: str
    card: PeerCard
    created_ms: int
    expires_ms: int
    signature: bytes
    version: int = PAIRING_VERSION
    object_type: str = OFFER_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.offer_id,
            self.created_ms,
            self.expires_ms,
            self.card.to_dict(),
        ]

    def verify(self, *, now: int | None = None) -> None:
        if self.version != PAIRING_VERSION or self.object_type != OFFER_TYPE:
            raise ValueError("unsupported pairing offer")
        if len(self.offer_id) != 32:
            raise ValueError("invalid pairing offer id")
        try:
            bytes.fromhex(self.offer_id)
        except ValueError as exc:
            raise ValueError("invalid pairing offer id") from exc
        self.card.verify()
        _validate_window(self.created_ms, self.expires_ms, now=_now_ms() if now is None else now)
        Ed25519PublicKey.from_public_bytes(self.card.sign_public).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(
            canonical_pack([self.signing_fields(), self.signature])
        ).digest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "offer_id": self.offer_id,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "card": self.card.to_dict(),
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        identity: Identity,
        card: PeerCard,
        *,
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> "PairOffer":
        created = _now_ms() if now is None else int(now)
        ttl_ms = max(60_000, min(int(ttl_seconds) * 1000, MAX_OFFER_TTL_MS))
        unsigned = cls(
            offer_id=secrets.token_hex(16),
            card=card,
            created_ms=created,
            expires_ms=created + ttl_ms,
            signature=b"",
        )
        if card.node_id != identity.node_id:
            raise ValueError("pairing offer card does not belong to local identity")
        return cls(
            **{
                **unsigned.__dict__,
                "signature": identity.sign(canonical_pack(unsigned.signing_fields())),
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PairOffer":
        return cls(
            version=int(value.get("version", 0)),
            object_type=str(value.get("type", "")),
            offer_id=str(value.get("offer_id", "")),
            created_ms=int(value.get("created_ms", 0)),
            expires_ms=int(value.get("expires_ms", 0)),
            card=PeerCard.from_dict(dict(value["card"])),
            signature=b64d(str(value.get("signature", ""))),
        )

    @classmethod
    def load(cls, path: Path, *, now: int | None = None) -> "PairOffer":
        offer = cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        offer.verify(now=now)
        return offer

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


@dataclass(frozen=True)
class PairResponse:
    offer_id: str
    offer_digest: bytes
    card: PeerCard
    accepted_ms: int
    signature: bytes
    version: int = PAIRING_VERSION
    object_type: str = RESPONSE_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.offer_id,
            self.offer_digest,
            self.accepted_ms,
            self.card.to_dict(),
        ]

    def verify(self, offer: PairOffer, *, now: int | None = None) -> None:
        current = _now_ms() if now is None else int(now)
        offer.verify(now=current)
        if self.version != PAIRING_VERSION or self.object_type != RESPONSE_TYPE:
            raise ValueError("unsupported pairing response")
        if self.offer_id != offer.offer_id or self.offer_digest != offer.digest:
            raise ValueError("pairing response is not bound to this offer")
        if self.card.node_id == offer.card.node_id:
            raise ValueError("cannot pair a node with itself")
        if self.accepted_ms < offer.created_ms - MAX_CLOCK_SKEW_MS:
            raise ValueError("pairing response predates its offer")
        if self.accepted_ms > offer.expires_ms:
            raise ValueError("pairing response was accepted after expiry")
        if self.accepted_ms > current + MAX_CLOCK_SKEW_MS:
            raise ValueError("pairing response was created too far in the future")
        self.card.verify()
        Ed25519PublicKey.from_public_bytes(self.card.sign_public).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "offer_id": self.offer_id,
            "offer_digest": b64e(self.offer_digest),
            "accepted_ms": self.accepted_ms,
            "card": self.card.to_dict(),
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        offer: PairOffer,
        identity: Identity,
        card: PeerCard,
        *,
        now: int | None = None,
    ) -> "PairResponse":
        accepted = _now_ms() if now is None else int(now)
        offer.verify(now=accepted)
        if card.node_id != identity.node_id:
            raise ValueError("pairing response card does not belong to local identity")
        unsigned = cls(
            offer_id=offer.offer_id,
            offer_digest=offer.digest,
            card=card,
            accepted_ms=accepted,
            signature=b"",
        )
        return cls(
            **{
                **unsigned.__dict__,
                "signature": identity.sign(canonical_pack(unsigned.signing_fields())),
            }
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PairResponse":
        return cls(
            version=int(value.get("version", 0)),
            object_type=str(value.get("type", "")),
            offer_id=str(value.get("offer_id", "")),
            offer_digest=b64d(str(value.get("offer_digest", ""))),
            accepted_ms=int(value.get("accepted_ms", 0)),
            card=PeerCard.from_dict(dict(value["card"])),
            signature=b64d(str(value.get("signature", ""))),
        )

    @classmethod
    def load(cls, path: Path) -> "PairResponse":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())
