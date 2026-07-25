"""ABA-D0: a chain-independent Abazr cooperation lifecycle demo.

This module is deliberately outside ``src/anet``.  It exercises signed ABA
domain records and an in-memory Matcher without creating an Anet node, Ahub,
wallet, payment, or network connection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


VISIBILITIES = frozenset({"private", "circle", "public"})
KINDS = frozenset(
    {
        "aba.need.v1",
        "aba.offer.v1",
        "aba.match.v1",
        "aba.proposal.v1",
        "aba.agreement.v1",
        "aba.fulfillment.v1",
        "aba.evidence.v1",
    }
)
FINANCIAL_FIELDS = frozenset(
    {
        "buyer",
        "seller",
        "price",
        "currency",
        "token",
        "wallet",
        "chain_id",
        "escrow",
        "payment",
    }
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value)
    return hashlib.sha256(raw).hexdigest()


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _contains_financial_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in FINANCIAL_FIELDS
            or _contains_financial_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_financial_field(item) for item in value)
    return False


def _require_shape(
    payload: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
) -> None:
    fields = set(payload)
    if missing := required - fields:
        raise ValueError(f"missing fields: {sorted(missing)}")
    if extra := fields - allowed:
        raise ValueError(f"unexpected fields: {sorted(extra)}")


@dataclass(frozen=True)
class Participant:
    """An ephemeral ABA-D0 signer, not a final ABA or Anet identity model."""

    label: str
    _private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    @classmethod
    def generate(cls, label: str) -> Participant:
        return cls(label=label, _private_key=Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            Encoding.Raw,
            PublicFormat.Raw,
        )

    @property
    def participant_id(self) -> str:
        return f"aba:participant:{_sha256(self.public_key)[:32]}"

    def issue(
        self,
        kind: str,
        *,
        visibility: str,
        payload: Mapping[str, Any],
        now_ms: int,
        ttl_ms: int = 3_600_000,
        nonce: str | None = None,
    ) -> SignedRecord:
        unsigned = {
            "kind": kind,
            "issuer": self.participant_id,
            "public_key": _b64e(self.public_key),
            "visibility": visibility,
            "created_ms": now_ms,
            "expires_ms": now_ms + ttl_ms,
            "nonce": nonce or secrets.token_hex(12),
            "payload": dict(payload),
        }
        return SignedRecord(
            **unsigned,
            signature=_b64e(self._private_key.sign(_canonical(unsigned))),
        )


@dataclass(frozen=True)
class SignedRecord:
    kind: str
    issuer: str
    public_key: str
    visibility: str
    created_ms: int
    expires_ms: int
    nonce: str
    payload: Mapping[str, Any]
    signature: str

    def unsigned_fields(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "issuer": self.issuer,
            "public_key": self.public_key,
            "visibility": self.visibility,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "nonce": self.nonce,
            "payload": dict(self.payload),
        }

    @property
    def record_id(self) -> str:
        return "aba:sha256:" + _sha256(
            [self.unsigned_fields(), self.signature]
        )

    def verify(self, *, now_ms: int) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unsupported ABA kind: {self.kind}")
        if self.visibility not in VISIBILITIES:
            raise ValueError(f"unsupported visibility: {self.visibility}")
        if not self.created_ms <= now_ms < self.expires_ms:
            raise ValueError("record is not currently valid")
        public_key = _b64d(self.public_key)
        expected_issuer = f"aba:participant:{_sha256(public_key)[:32]}"
        if self.issuer != expected_issuer:
            raise ValueError("issuer does not match public key")
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                _b64d(self.signature),
                _canonical(self.unsigned_fields()),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("invalid ABA record signature") from exc

    def demo_view(self) -> dict[str, Any]:
        """Return a local transcript view; never use it as a projection."""
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "issuer": self.issuer,
            "visibility": self.visibility,
            "payload": dict(self.payload),
        }


class Bazaar:
    """Deep ABA-D0 module: validate records, enforce lifecycle, and match."""

    def __init__(self, matcher: Participant) -> None:
        self._matcher = matcher
        self._records: dict[str, SignedRecord] = {}

    def submit(self, record: SignedRecord, *, now_ms: int) -> SignedRecord:
        record.verify(now_ms=now_ms)
        if _contains_financial_field(record.payload):
            raise ValueError("financial fields require a Settlement Adapter")
        existing = self._records.get(record.record_id)
        if existing is not None:
            return existing
        self._validate_lifecycle(record)
        self._records[record.record_id] = record
        return record

    def find_matches(self, *, now_ms: int) -> list[SignedRecord]:
        needs = self._discoverable("aba.need.v1", now_ms=now_ms)
        offers = self._discoverable("aba.offer.v1", now_ms=now_ms)
        matches: list[SignedRecord] = []
        for need in needs:
            for offer in offers:
                if need.payload["capability"] != offer.payload["capability"]:
                    continue
                shared_tags = sorted(
                    set(need.payload["tags"]) & set(offer.payload["tags"])
                )
                reasons = [
                    f"capability:{need.payload['capability']}",
                    *[f"tag:{tag}" for tag in shared_tags],
                ]
                score = min(100, 80 + 5 * len(shared_tags))
                match = self._matcher.issue(
                    "aba.match.v1",
                    visibility="private",
                    payload={
                        "need_id": need.record_id,
                        "offer_id": offer.record_id,
                        "requester_id": need.issuer,
                        "provider_id": offer.issuer,
                        "score": score,
                        "reasons": reasons,
                        "authorized": False,
                    },
                    now_ms=now_ms,
                    nonce=_sha256([need.record_id, offer.record_id])[:24],
                )
                matches.append(self.submit(match, now_ms=now_ms))
        return matches

    def records(self) -> tuple[SignedRecord, ...]:
        return tuple(self._records.values())

    def _discoverable(
        self,
        kind: str,
        *,
        now_ms: int,
    ) -> list[SignedRecord]:
        result: list[SignedRecord] = []
        for record in self._records.values():
            # ABA-D0 has no circle membership model yet. Treating a circle
            # record as globally discoverable would silently widen its scope.
            if record.kind != kind or record.visibility != "public":
                continue
            record.verify(now_ms=now_ms)
            result.append(record)
        return result

    def _record(self, record_id: str, kind: str) -> SignedRecord:
        try:
            record = self._records[record_id]
        except KeyError as exc:
            raise ValueError(f"unknown referenced record: {record_id}") from exc
        if record.kind != kind:
            raise ValueError(f"expected {kind}, got {record.kind}")
        return record

    def _validate_lifecycle(self, record: SignedRecord) -> None:
        payload = record.payload
        if record.kind in {"aba.need.v1", "aba.offer.v1"}:
            _require_shape(
                payload,
                required={"title", "summary", "capability", "tags"},
                allowed={"title", "summary", "capability", "tags"},
            )
            if not isinstance(payload["tags"], list) or not all(
                isinstance(tag, str) for tag in payload["tags"]
            ):
                raise ValueError("tags must be a list of strings")
            return

        if record.visibility != "private":
            raise ValueError(f"{record.kind} must be private")

        if record.kind == "aba.match.v1":
            _require_shape(
                payload,
                required={
                    "need_id",
                    "offer_id",
                    "requester_id",
                    "provider_id",
                    "score",
                    "reasons",
                    "authorized",
                },
                allowed={
                    "need_id",
                    "offer_id",
                    "requester_id",
                    "provider_id",
                    "score",
                    "reasons",
                    "authorized",
                },
            )
            need = self._record(str(payload["need_id"]), "aba.need.v1")
            offer = self._record(str(payload["offer_id"]), "aba.offer.v1")
            if record.issuer != self._matcher.participant_id:
                raise ValueError("only the configured Matcher may issue Match")
            if payload["requester_id"] != need.issuer:
                raise ValueError("Match requester does not own Need")
            if payload["provider_id"] != offer.issuer:
                raise ValueError("Match provider does not own Offer")
            if payload["authorized"] is not False:
                raise ValueError("Match cannot grant authorization")
            return

        if record.kind == "aba.proposal.v1":
            _require_shape(
                payload,
                required={
                    "match_id",
                    "requester_id",
                    "provider_id",
                    "terms",
                },
                allowed={
                    "match_id",
                    "requester_id",
                    "provider_id",
                    "terms",
                },
            )
            match = self._record(str(payload["match_id"]), "aba.match.v1")
            if record.issuer != match.payload["provider_id"]:
                raise ValueError("Proposal issuer must be Match Provider")
            if payload["requester_id"] != match.payload["requester_id"]:
                raise ValueError("Proposal requester differs from Match")
            if payload["provider_id"] != match.payload["provider_id"]:
                raise ValueError("Proposal provider differs from Match")
            if not isinstance(payload["terms"], Mapping):
                raise ValueError("Proposal terms must be an object")
            return

        if record.kind == "aba.agreement.v1":
            _require_shape(
                payload,
                required={
                    "proposal_id",
                    "requester_id",
                    "provider_id",
                    "accepted_terms_digest",
                },
                allowed={
                    "proposal_id",
                    "requester_id",
                    "provider_id",
                    "accepted_terms_digest",
                },
            )
            proposal = self._record(
                str(payload["proposal_id"]),
                "aba.proposal.v1",
            )
            if record.issuer != proposal.payload["requester_id"]:
                raise ValueError("Agreement issuer must be Proposal Requester")
            if payload["requester_id"] != proposal.payload["requester_id"]:
                raise ValueError("Agreement requester differs from Proposal")
            if payload["provider_id"] != proposal.payload["provider_id"]:
                raise ValueError("Agreement provider differs from Proposal")
            if payload["accepted_terms_digest"] != _sha256(
                proposal.payload["terms"]
            ):
                raise ValueError("Agreement terms digest differs from Proposal")
            return

        if record.kind == "aba.fulfillment.v1":
            _require_shape(
                payload,
                required={"agreement_id", "artifact_digest", "summary"},
                allowed={"agreement_id", "artifact_digest", "summary"},
            )
            agreement = self._record(
                str(payload["agreement_id"]),
                "aba.agreement.v1",
            )
            if record.issuer != agreement.payload["provider_id"]:
                raise ValueError("Fulfillment issuer must be Agreement Provider")
            if not str(payload["artifact_digest"]).startswith("sha256:"):
                raise ValueError("Artifact must use an explicit digest")
            return

        if record.kind == "aba.evidence.v1":
            _require_shape(
                payload,
                required={
                    "agreement_id",
                    "fulfillment_id",
                    "outcome",
                    "note",
                },
                allowed={
                    "agreement_id",
                    "fulfillment_id",
                    "outcome",
                    "note",
                },
            )
            agreement = self._record(
                str(payload["agreement_id"]),
                "aba.agreement.v1",
            )
            fulfillment = self._record(
                str(payload["fulfillment_id"]),
                "aba.fulfillment.v1",
            )
            if fulfillment.payload["agreement_id"] != agreement.record_id:
                raise ValueError("Evidence references unrelated Fulfillment")
            if record.issuer != agreement.payload["requester_id"]:
                raise ValueError("Evidence issuer must be Agreement Requester")
            if payload["outcome"] not in {"accepted", "rejected", "disputed"}:
                raise ValueError("unsupported Evidence outcome")
            return

        raise ValueError(f"unhandled ABA kind: {record.kind}")


def run_demo(*, now_ms: int = 1_750_000_000_000) -> tuple[SignedRecord, ...]:
    requester = Participant.generate("requester")
    provider = Participant.generate("provider")
    matcher = Participant.generate("matcher")
    bazaar = Bazaar(matcher)

    need = bazaar.submit(
        requester.issue(
            "aba.need.v1",
            visibility="public",
            payload={
                "title": "Review an Agent protocol change",
                "summary": "Return a bounded compatibility and safety report.",
                "capability": "protocol.review",
                "tags": ["agent-infrastructure", "python", "security"],
            },
            now_ms=now_ms,
            nonce="need-demo",
        ),
        now_ms=now_ms,
    )
    offer = bazaar.submit(
        provider.issue(
            "aba.offer.v1",
            visibility="public",
            payload={
                "title": "Protocol review capability",
                "summary": "Reviews typed protocols and trust boundaries.",
                "capability": "protocol.review",
                "tags": ["agent-infrastructure", "python", "security"],
            },
            now_ms=now_ms,
            nonce="offer-demo",
        ),
        now_ms=now_ms,
    )
    match = bazaar.find_matches(now_ms=now_ms)[0]
    proposal = bazaar.submit(
        provider.issue(
            "aba.proposal.v1",
            visibility="private",
            payload={
                "match_id": match.record_id,
                "requester_id": requester.participant_id,
                "provider_id": provider.participant_id,
                "terms": {
                    "objective": "Review the proposed protocol change.",
                    "completion": "Structured findings with bounded evidence.",
                    "resource_budget": {
                        "max_minutes": 30,
                        "max_artifact_bytes": 200_000,
                    },
                },
            },
            now_ms=now_ms,
            nonce="proposal-demo",
        ),
        now_ms=now_ms,
    )
    agreement = bazaar.submit(
        requester.issue(
            "aba.agreement.v1",
            visibility="private",
            payload={
                "proposal_id": proposal.record_id,
                "requester_id": requester.participant_id,
                "provider_id": provider.participant_id,
                "accepted_terms_digest": _sha256(proposal.payload["terms"]),
            },
            now_ms=now_ms,
            nonce="agreement-demo",
        ),
        now_ms=now_ms,
    )
    artifact = _canonical(
        {
            "summary": "The protocol keeps discovery separate from authority.",
            "findings": [
                "Match is explicitly non-authorizing.",
                "Private cooperation records are not publicly indexed.",
            ],
        }
    )
    fulfillment = bazaar.submit(
        provider.issue(
            "aba.fulfillment.v1",
            visibility="private",
            payload={
                "agreement_id": agreement.record_id,
                "artifact_digest": f"sha256:{_sha256(artifact)}",
                "summary": "Compatibility and safety review completed.",
            },
            now_ms=now_ms,
            nonce="fulfillment-demo",
        ),
        now_ms=now_ms,
    )
    bazaar.submit(
        requester.issue(
            "aba.evidence.v1",
            visibility="private",
            payload={
                "agreement_id": agreement.record_id,
                "fulfillment_id": fulfillment.record_id,
                "outcome": "accepted",
                "note": "The Artifact digest and requested findings match.",
            },
            now_ms=now_ms,
            nonce="evidence-demo",
        ),
        now_ms=now_ms,
    )

    records = bazaar.records()
    assert need in records and offer in records
    return records


def main() -> int:
    transcript = [record.demo_view() for record in run_demo()]
    print(
        json.dumps(
            {
                "demo": "ABA-D0",
                "chain_required": False,
                "anet_state_changed": False,
                "records": transcript,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
