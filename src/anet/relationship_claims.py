from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .encoding import atomic_json, b64d, b64e, canonical_pack
from .identity import Identity, PeerCard
from .relations import MAX_LABEL_LENGTH, RELATION_CIRCLES


RELATIONSHIP_CLAIM_VERSION = 1
RELATIONSHIP_PROPOSAL_TYPE = "anet.relationship.proposal.v1"
RELATIONSHIP_ACCEPTANCE_TYPE = "anet.relationship.acceptance.v1"
RELATIONSHIP_WITHDRAWAL_TYPE = "anet.relationship.withdrawal.v1"
RELATIONSHIP_CLAIM_BOOK_VERSION = 2
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_PROPOSAL_TTL_MS = 7 * 24 * 60 * 60 * 1000
MAX_RELATIONSHIP_LABELS = 32
_ACTOR_ID_RE = re.compile(r"^an1[a-z2-7]{32}$")
_CLAIM_ID_RE = re.compile(r"^mrel_[0-9a-f]{64}$")


def _now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


def _circle(value: str) -> str:
    circle = str(value).strip().lower()
    if circle not in RELATION_CIRCLES[1:]:
        raise ValueError("mutual relationship circle must be known or closer")
    return circle


def _labels(values: Iterable[str]) -> tuple[str, ...]:
    labels = tuple(
        sorted(
            {
                str(value).strip()
                for value in values
                if str(value).strip()
            }
        )
    )
    if len(labels) > MAX_RELATIONSHIP_LABELS:
        raise ValueError("too many mutual relationship labels")
    if any(len(value) > MAX_LABEL_LENGTH for value in labels):
        raise ValueError("mutual relationship label is too long")
    return labels


def _proposal_id(value: str) -> str:
    proposal_id = str(value).strip().lower()
    if len(proposal_id) != 32:
        raise ValueError("invalid relationship proposal ID")
    try:
        bytes.fromhex(proposal_id)
    except ValueError as exc:
        raise ValueError("invalid relationship proposal ID") from exc
    return proposal_id


def _actor_id(value: str) -> str:
    actor_id = str(value).strip().lower()
    if not _ACTOR_ID_RE.fullmatch(actor_id):
        raise ValueError("invalid relationship claim Actor ID")
    return actor_id


def _claim_id(value: str) -> str:
    claim_id = str(value).strip().lower()
    if not _CLAIM_ID_RE.fullmatch(claim_id):
        raise ValueError("invalid mutual relationship claim ID")
    return claim_id


def _validate_window(
    created_ms: int,
    expires_ms: int,
    *,
    now: int,
    require_active: bool,
) -> None:
    if created_ms <= 0 or expires_ms <= created_ms:
        raise ValueError("invalid relationship proposal validity window")
    if expires_ms - created_ms > MAX_PROPOSAL_TTL_MS:
        raise ValueError("relationship proposal validity window is too long")
    if created_ms > now + MAX_CLOCK_SKEW_MS:
        raise ValueError("relationship proposal was created too far in the future")
    if require_active and expires_ms < now:
        raise ValueError("relationship proposal has expired")


@dataclass(frozen=True)
class RelationshipProposal:
    proposal_id: str
    proposer_card: PeerCard
    peer_actor_id: str
    circle: str
    labels: tuple[str, ...]
    created_ms: int
    expires_ms: int
    signature: bytes
    version: int = RELATIONSHIP_CLAIM_VERSION
    object_type: str = RELATIONSHIP_PROPOSAL_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.proposal_id,
            self.proposer_card.to_dict(),
            self.peer_actor_id,
            self.circle,
            list(self.labels),
            self.created_ms,
            self.expires_ms,
        ]

    def verify(
        self,
        *,
        now: int | None = None,
        require_active: bool = True,
    ) -> None:
        current = _now_ms(now)
        if (
            self.version != RELATIONSHIP_CLAIM_VERSION
            or self.object_type != RELATIONSHIP_PROPOSAL_TYPE
        ):
            raise ValueError("unsupported relationship proposal")
        _proposal_id(self.proposal_id)
        self.proposer_card.verify()
        if (
            self.peer_actor_id != _actor_id(self.peer_actor_id)
            or self.peer_actor_id == self.proposer_card.node_id
        ):
            raise ValueError("invalid relationship proposal peer Actor")
        if self.circle != _circle(self.circle):
            raise ValueError("relationship proposal circle is not normalized")
        if self.labels != _labels(self.labels):
            raise ValueError("relationship proposal labels are not normalized")
        _validate_window(
            self.created_ms,
            self.expires_ms,
            now=current,
            require_active=require_active,
        )
        Ed25519PublicKey.from_public_bytes(
            self.proposer_card.sign_public
        ).verify(
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
            "proposal_id": self.proposal_id,
            "proposer_card": self.proposer_card.to_dict(),
            "peer_actor_id": self.peer_actor_id,
            "circle": self.circle,
            "labels": list(self.labels),
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        identity: Identity,
        card: PeerCard,
        *,
        peer_actor_id: str,
        circle: str,
        labels: Iterable[str] = (),
        ttl_seconds: int = 3600,
        now: int | None = None,
    ) -> "RelationshipProposal":
        current = _now_ms(now)
        if card.node_id != identity.node_id:
            raise ValueError("relationship proposal card is not local")
        ttl_ms = max(
            60_000,
            min(int(ttl_seconds) * 1000, MAX_PROPOSAL_TTL_MS),
        )
        unsigned = cls(
            proposal_id=secrets.token_hex(16),
            proposer_card=card,
            peer_actor_id=_actor_id(peer_actor_id),
            circle=_circle(circle),
            labels=_labels(labels),
            created_ms=current,
            expires_ms=current + ttl_ms,
            signature=b"",
        )
        proposal = cls(
            **{
                **unsigned.__dict__,
                "signature": identity.sign(
                    canonical_pack(unsigned.signing_fields())
                ),
            }
        )
        proposal.verify(now=current)
        return proposal

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipProposal":
        expected = {
            "version",
            "type",
            "proposal_id",
            "proposer_card",
            "peer_actor_id",
            "circle",
            "labels",
            "created_ms",
            "expires_ms",
            "signature",
        }
        if set(value) != expected:
            raise ValueError("relationship proposal has unexpected fields")
        labels = value["labels"]
        if not isinstance(labels, list) or any(
            not isinstance(item, str) for item in labels
        ):
            raise ValueError("relationship proposal labels must be a string list")
        return cls(
            version=int(value["version"]),
            object_type=str(value["type"]),
            proposal_id=str(value["proposal_id"]),
            proposer_card=PeerCard.from_dict(dict(value["proposer_card"])),
            peer_actor_id=str(value["peer_actor_id"]),
            circle=str(value["circle"]),
            labels=tuple(labels),
            created_ms=int(value["created_ms"]),
            expires_ms=int(value["expires_ms"]),
            signature=b64d(str(value["signature"])),
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        now: int | None = None,
    ) -> "RelationshipProposal":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("relationship proposal must be a JSON object")
        proposal = cls.from_dict(value)
        proposal.verify(now=now)
        return proposal

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


@dataclass(frozen=True)
class MutualRelationshipClaim:
    proposal: RelationshipProposal
    proposal_digest: bytes
    accepter_card: PeerCard
    accepted_ms: int
    signature: bytes
    version: int = RELATIONSHIP_CLAIM_VERSION
    object_type: str = RELATIONSHIP_ACCEPTANCE_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.proposal_digest,
            self.accepter_card.to_dict(),
            self.accepted_ms,
        ]

    def verify(self, *, now: int | None = None) -> None:
        current = _now_ms(now)
        if (
            self.version != RELATIONSHIP_CLAIM_VERSION
            or self.object_type != RELATIONSHIP_ACCEPTANCE_TYPE
        ):
            raise ValueError("unsupported mutual relationship claim")
        self.proposal.verify(now=current, require_active=False)
        if self.proposal_digest != self.proposal.digest:
            raise ValueError("relationship acceptance is not bound to its proposal")
        self.accepter_card.verify()
        if self.accepter_card.node_id != self.proposal.peer_actor_id:
            raise ValueError("relationship acceptance came from another Actor")
        if self.accepted_ms < self.proposal.created_ms - MAX_CLOCK_SKEW_MS:
            raise ValueError("relationship acceptance predates its proposal")
        if self.accepted_ms > self.proposal.expires_ms:
            raise ValueError("relationship acceptance occurred after expiry")
        if self.accepted_ms > current + MAX_CLOCK_SKEW_MS:
            raise ValueError("relationship acceptance was created too far in the future")
        Ed25519PublicKey.from_public_bytes(
            self.accepter_card.sign_public
        ).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    @property
    def claim_id(self) -> str:
        digest = hashlib.sha256(
            canonical_pack([self.signing_fields(), self.signature])
        ).hexdigest()
        return f"mrel_{digest}"

    @property
    def circle(self) -> str:
        return self.proposal.circle

    @property
    def labels(self) -> tuple[str, ...]:
        return self.proposal.labels

    def peer_card_for(self, local_actor_id: str) -> PeerCard:
        local = _actor_id(local_actor_id)
        if local == self.proposal.proposer_card.node_id:
            return self.accepter_card
        if local == self.accepter_card.node_id:
            return self.proposal.proposer_card
        raise ValueError("local Actor is not a participant in this relationship claim")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "proposal": self.proposal.to_dict(),
            "proposal_digest": b64e(self.proposal_digest),
            "accepter_card": self.accepter_card.to_dict(),
            "accepted_ms": self.accepted_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        proposal: RelationshipProposal,
        identity: Identity,
        card: PeerCard,
        *,
        now: int | None = None,
    ) -> "MutualRelationshipClaim":
        current = _now_ms(now)
        proposal.verify(now=current, require_active=True)
        if card.node_id != identity.node_id:
            raise ValueError("relationship acceptance card is not local")
        if card.node_id != proposal.peer_actor_id:
            raise ValueError("local Actor is not the proposed relationship peer")
        unsigned = cls(
            proposal=proposal,
            proposal_digest=proposal.digest,
            accepter_card=card,
            accepted_ms=current,
            signature=b"",
        )
        claim = cls(
            **{
                **unsigned.__dict__,
                "signature": identity.sign(
                    canonical_pack(unsigned.signing_fields())
                ),
            }
        )
        claim.verify(now=current)
        return claim

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MutualRelationshipClaim":
        expected = {
            "version",
            "type",
            "proposal",
            "proposal_digest",
            "accepter_card",
            "accepted_ms",
            "signature",
        }
        if set(value) != expected:
            raise ValueError("mutual relationship claim has unexpected fields")
        return cls(
            version=int(value["version"]),
            object_type=str(value["type"]),
            proposal=RelationshipProposal.from_dict(dict(value["proposal"])),
            proposal_digest=b64d(str(value["proposal_digest"])),
            accepter_card=PeerCard.from_dict(dict(value["accepter_card"])),
            accepted_ms=int(value["accepted_ms"]),
            signature=b64d(str(value["signature"])),
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        now: int | None = None,
    ) -> "MutualRelationshipClaim":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("mutual relationship claim must be a JSON object")
        claim = cls.from_dict(value)
        claim.verify(now=now)
        return claim

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


@dataclass(frozen=True)
class RelationshipClaimWithdrawal:
    """A participant's signed withdrawal of a previous mutual claim.

    Withdrawal changes only the portable claim's active status. It does not
    assert an identity change or rewrite either observer's local relationship.
    """

    claim_id: str
    withdrawing_card: PeerCard
    withdrawn_ms: int
    signature: bytes
    version: int = RELATIONSHIP_CLAIM_VERSION
    object_type: str = RELATIONSHIP_WITHDRAWAL_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.claim_id,
            self.withdrawing_card.to_dict(),
            self.withdrawn_ms,
        ]

    @property
    def withdrawal_id(self) -> str:
        digest = hashlib.sha256(
            canonical_pack([self.signing_fields(), self.signature])
        ).hexdigest()
        return f"mrelw_{digest}"

    def verify(
        self,
        claim: MutualRelationshipClaim,
        *,
        now: int | None = None,
    ) -> None:
        current = _now_ms(now)
        if (
            self.version != RELATIONSHIP_CLAIM_VERSION
            or self.object_type != RELATIONSHIP_WITHDRAWAL_TYPE
        ):
            raise ValueError("unsupported relationship claim withdrawal")
        if self.claim_id != _claim_id(self.claim_id):
            raise ValueError("relationship claim withdrawal ID is not normalized")
        claim.verify(now=current)
        if self.claim_id != claim.claim_id:
            raise ValueError("relationship claim withdrawal targets another claim")
        self.withdrawing_card.verify()
        participants = {
            claim.proposal.proposer_card.node_id,
            claim.accepter_card.node_id,
        }
        if self.withdrawing_card.node_id not in participants:
            raise ValueError("relationship claim withdrawal came from another Actor")
        if self.withdrawn_ms < claim.accepted_ms - MAX_CLOCK_SKEW_MS:
            raise ValueError("relationship claim withdrawal predates acceptance")
        if self.withdrawn_ms > current + MAX_CLOCK_SKEW_MS:
            raise ValueError("relationship claim withdrawal was created too far in the future")
        Ed25519PublicKey.from_public_bytes(
            self.withdrawing_card.sign_public
        ).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    @classmethod
    def create(
        cls,
        claim: MutualRelationshipClaim,
        identity: Identity,
        card: PeerCard,
        *,
        now: int | None = None,
    ) -> "RelationshipClaimWithdrawal":
        current = _now_ms(now)
        claim.verify(now=current)
        if card.node_id != identity.node_id:
            raise ValueError("relationship claim withdrawal card is not local")
        if card.node_id not in {
            claim.proposal.proposer_card.node_id,
            claim.accepter_card.node_id,
        }:
            raise ValueError("local Actor is not a participant in this relationship claim")
        unsigned = cls(
            claim_id=claim.claim_id,
            withdrawing_card=card,
            withdrawn_ms=current,
            signature=b"",
        )
        withdrawal = cls(
            **{
                **unsigned.__dict__,
                "signature": identity.sign(
                    canonical_pack(unsigned.signing_fields())
                ),
            }
        )
        withdrawal.verify(claim, now=current)
        return withdrawal

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "claim_id": self.claim_id,
            "withdrawing_card": self.withdrawing_card.to_dict(),
            "withdrawn_ms": self.withdrawn_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipClaimWithdrawal":
        expected = {
            "version",
            "type",
            "claim_id",
            "withdrawing_card",
            "withdrawn_ms",
            "signature",
        }
        if set(value) != expected:
            raise ValueError("relationship claim withdrawal has unexpected fields")
        return cls(
            version=int(value["version"]),
            object_type=str(value["type"]),
            claim_id=str(value["claim_id"]),
            withdrawing_card=PeerCard.from_dict(dict(value["withdrawing_card"])),
            withdrawn_ms=int(value["withdrawn_ms"]),
            signature=b64d(str(value["signature"])),
        )

    @classmethod
    def load(
        cls,
        path: Path,
        claim: MutualRelationshipClaim,
        *,
        now: int | None = None,
    ) -> "RelationshipClaimWithdrawal":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("relationship claim withdrawal must be a JSON object")
        withdrawal = cls.from_dict(value)
        withdrawal.verify(claim, now=now)
        return withdrawal

    def save(self, path: Path) -> None:
        atomic_json(Path(path), self.to_dict())


class RelationshipClaimBook:
    """Durable verified claim objects keyed by their signed digest."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._claims: dict[str, MutualRelationshipClaim] = {}
        self._withdrawals: dict[str, RelationshipClaimWithdrawal] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._claims = {}
            self._withdrawals = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("relationship claim book must be a JSON object")
        version = int(value.get("version", 0))
        if version not in {1, RELATIONSHIP_CLAIM_BOOK_VERSION}:
            raise ValueError("unsupported relationship claim book version")
        raw_claims = value.get("claims", ())
        if not isinstance(raw_claims, list):
            raise ValueError("relationship claim book claims must be a list")
        claims: dict[str, MutualRelationshipClaim] = {}
        for raw in raw_claims:
            if not isinstance(raw, dict):
                raise ValueError("relationship claim book entry must be an object")
            claim = MutualRelationshipClaim.from_dict(dict(raw))
            claim.verify()
            if claim.claim_id in claims:
                raise ValueError("relationship claim book contains a duplicate")
            claims[claim.claim_id] = claim
        raw_withdrawals = value.get("withdrawals", ()) if version >= 2 else ()
        if not isinstance(raw_withdrawals, list):
            raise ValueError("relationship claim book withdrawals must be a list")
        withdrawals: dict[str, RelationshipClaimWithdrawal] = {}
        for raw in raw_withdrawals:
            if not isinstance(raw, dict):
                raise ValueError("relationship claim book withdrawal must be an object")
            withdrawal = RelationshipClaimWithdrawal.from_dict(dict(raw))
            claim = claims.get(withdrawal.claim_id)
            if claim is None:
                raise ValueError("relationship claim withdrawal references an unknown claim")
            withdrawal.verify(claim)
            if withdrawal.withdrawal_id in withdrawals:
                raise ValueError("relationship claim book contains a duplicate withdrawal")
            withdrawals[withdrawal.withdrawal_id] = withdrawal
        self._claims = claims
        self._withdrawals = withdrawals

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": RELATIONSHIP_CLAIM_BOOK_VERSION,
                "claims": [
                    self._claims[key].to_dict()
                    for key in sorted(self._claims)
                ],
                "withdrawals": [
                    self._withdrawals[key].to_dict()
                    for key in sorted(self._withdrawals)
                ],
            },
            private=True,
        )

    def add(self, claim: MutualRelationshipClaim) -> bool:
        claim.verify()
        if claim.claim_id in self._claims:
            return False
        self._claims[claim.claim_id] = claim
        self.save()
        return True

    def all(self) -> tuple[MutualRelationshipClaim, ...]:
        return tuple(self._claims[key] for key in sorted(self._claims))

    def claim(self, claim_id: str) -> MutualRelationshipClaim | None:
        return self._claims.get(_claim_id(claim_id))

    def add_withdrawal(self, withdrawal: RelationshipClaimWithdrawal) -> bool:
        claim = self.claim(withdrawal.claim_id)
        if claim is None:
            raise ValueError("relationship claim withdrawal references an unknown claim")
        withdrawal.verify(claim)
        if withdrawal.withdrawal_id in self._withdrawals:
            return False
        self._withdrawals[withdrawal.withdrawal_id] = withdrawal
        self.save()
        return True

    def withdrawals_for(
        self,
        claim_id: str,
    ) -> tuple[RelationshipClaimWithdrawal, ...]:
        claim_key = _claim_id(claim_id)
        return tuple(
            withdrawal
            for _key, withdrawal in sorted(self._withdrawals.items())
            if withdrawal.claim_id == claim_key
        )

    def is_active(self, claim_id: str) -> bool:
        if self.claim(claim_id) is None:
            raise ValueError("relationship claim is not stored")
        return not self.withdrawals_for(claim_id)
