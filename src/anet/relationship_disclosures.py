from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .actors import validate_actor_id
from .encoding import atomic_json, canonical_pack
from .relation_activity import RelationshipActivityPage


RELATIONSHIP_DISCLOSURE_VERSION = 1
RELATIONSHIP_DISCLOSURE_KIND = "social.relationship.disclosure"
RELATIONSHIP_DISCLOSURE_TYPE = "anet.relationship.disclosure.v1"
RELATIONSHIP_DISCLOSURE_SERIES_VERSION = 2
RELATIONSHIP_DISCLOSURE_SERIES_TYPE = "anet.relationship.disclosure.v2"
MAX_DISCLOSURE_ACTIVITIES = 100
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

_DISCLOSURE_ID_RE = re.compile(r"^rdis_[0-9a-f]{64}$")
_SERIES_ID_RE = re.compile(r"^rdsr_[0-9a-f]{32}$")
_EVENT_ID_RE = re.compile(r"^revt_[0-9a-f]{24}$")
_CURSOR_RE = re.compile(r"^rac_[0-9a-f]{16}_revt_[0-9a-f]{24}$")
_SUBJECT_REF_RE = re.compile(r"^subj_[0-9a-f]{16}$")
_PACKET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ACTIVITY_FIELDS = frozenset(
    {
        "activity_id",
        "activity_type",
        "category",
        "fact_level",
        "actor_id",
        "subject_ref",
        "occurred_ms",
        "details",
        "evidence_digest",
        "privacy",
        "authorization_effect",
    }
)
_DETAIL_FIELDS = frozenset(
    {
        "actor_kind",
        "proof_scope",
        "actor_state",
        "interaction_id",
        "direction",
        "facets",
        "context",
        "outcome",
        "confidence",
        "transition_type",
        "source_subject_refs",
        "replacement_subject_refs",
        "circle",
        "estimate",
        "decision_id",
        "suggestion_id",
        "suggestion_type",
        "decision",
        "proposed_circle",
        "proposed_estimate",
        "applied",
    }
)
_LIST_DETAIL_FIELDS = frozenset(
    {"facets", "source_subject_refs", "replacement_subject_refs"}
)


def _now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


def _bounded_text(value: Any, *, label: str, maximum: int = 128) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum:
        raise ValueError(f"invalid relationship disclosure {label}")
    return text


def _subject_ref(value: Any, *, optional: bool = True) -> str:
    subject_ref = str(value).strip()
    if not subject_ref and optional:
        return ""
    if not _SUBJECT_REF_RE.fullmatch(subject_ref):
        raise ValueError("invalid relationship disclosure Subject reference")
    return subject_ref


def _normalize_details(value: Any) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping) or set(value) - _DETAIL_FIELDS:
        raise ValueError("invalid relationship disclosure activity details")
    normalized: list[tuple[str, Any]] = []
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key in _LIST_DETAIL_FIELDS:
            if not isinstance(raw_value, list) or any(
                not isinstance(item, str) for item in raw_value
            ):
                raise ValueError(
                    "invalid relationship disclosure activity detail list"
                )
            items = tuple(
                _bounded_text(
                    item,
                    label=f"{key} item",
                    maximum=128,
                )
                for item in raw_value
            )
            if len(items) > 32:
                raise ValueError(
                    "relationship disclosure activity detail list is too long"
                )
            normalized.append((key, items))
            continue
        if raw_value is None:
            normalized.append((key, None))
            continue
        if type(raw_value) is bool:
            normalized.append((key, raw_value))
            continue
        if type(raw_value) is int:
            if raw_value < 0 or raw_value > 100:
                raise ValueError(
                    "invalid relationship disclosure numeric detail"
                )
            normalized.append((key, raw_value))
            continue
        if not isinstance(raw_value, str):
            raise ValueError("invalid relationship disclosure activity detail")
        normalized.append(
            (
                key,
                _bounded_text(
                    raw_value,
                    label=key,
                    maximum=128,
                ),
            )
        )
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class DisclosedRelationshipActivity:
    activity_id: str
    activity_type: str
    category: str
    fact_level: str
    actor_id: str
    subject_ref: str
    occurred_ms: int
    details: tuple[tuple[str, Any], ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if not _EVENT_ID_RE.fullmatch(self.activity_id):
            raise ValueError("invalid disclosed relationship activity ID")
        _bounded_text(
            self.activity_type,
            label="activity type",
            maximum=96,
        )
        if self.category not in {
            "actor",
            "interaction",
            "subject",
            "relationship",
            "decision",
        }:
            raise ValueError("invalid disclosed relationship activity category")
        if self.fact_level not in {
            "verified",
            "inference",
            "estimate",
            "decision",
        }:
            raise ValueError("invalid disclosed relationship activity fact level")
        if self.actor_id:
            validate_actor_id(self.actor_id)
        _subject_ref(self.subject_ref)
        if type(self.occurred_ms) is not int or self.occurred_ms <= 0:
            raise ValueError("invalid disclosed relationship activity time")
        if not re.fullmatch(r"[0-9a-f]{32}", self.evidence_digest):
            raise ValueError(
                "invalid disclosed relationship evidence digest"
            )

    def to_dict(self) -> dict[str, Any]:
        details = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.details
        }
        return {
            "activity_id": self.activity_id,
            "activity_type": self.activity_type,
            "category": self.category,
            "fact_level": self.fact_level,
            "actor_id": self.actor_id,
            "subject_ref": self.subject_ref,
            "occurred_ms": self.occurred_ms,
            "details": details,
            "evidence_digest": self.evidence_digest,
            "privacy": "content-free",
            "authorization_effect": "none",
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "DisclosedRelationshipActivity":
        if set(value) != _ACTIVITY_FIELDS:
            raise ValueError(
                "relationship disclosure activity has unexpected fields"
            )
        if (
            value.get("privacy") != "content-free"
            or value.get("authorization_effect") != "none"
            or any(
                not isinstance(value.get(key), str)
                for key in {
                    "activity_id",
                    "activity_type",
                    "category",
                    "fact_level",
                    "actor_id",
                    "subject_ref",
                    "evidence_digest",
                }
            )
            or type(value.get("occurred_ms")) is not int
        ):
            raise ValueError(
                "relationship disclosure activity boundary is invalid"
            )
        return cls(
            activity_id=str(value["activity_id"]),
            activity_type=str(value["activity_type"]),
            category=str(value["category"]),
            fact_level=str(value["fact_level"]),
            actor_id=str(value["actor_id"]),
            subject_ref=str(value["subject_ref"]),
            occurred_ms=int(value["occurred_ms"]),
            details=_normalize_details(value["details"]),
            evidence_digest=str(value["evidence_digest"]),
        )


@dataclass(frozen=True)
class RelationshipDisclosure:
    disclosure_id: str
    observer_actor_id: str
    audience_actor_id: str
    issued_ms: int
    activities: tuple[DisclosedRelationshipActivity, ...]
    next_cursor: str
    has_more: bool
    series_id: str = ""
    sequence: int = 0
    starts_after: str = ""
    scope_subject_ref: str = ""
    baseline: str = ""
    version: int = RELATIONSHIP_DISCLOSURE_VERSION
    object_type: str = RELATIONSHIP_DISCLOSURE_TYPE

    def __post_init__(self) -> None:
        if self.version == RELATIONSHIP_DISCLOSURE_VERSION:
            if (
                self.object_type != RELATIONSHIP_DISCLOSURE_TYPE
                or self.series_id
                or self.sequence
                or self.starts_after
                or self.scope_subject_ref
                or self.baseline
            ):
                raise ValueError("invalid v1 relationship disclosure")
        elif self.version == RELATIONSHIP_DISCLOSURE_SERIES_VERSION:
            if self.object_type != RELATIONSHIP_DISCLOSURE_SERIES_TYPE:
                raise ValueError("invalid v2 relationship disclosure")
            if not _SERIES_ID_RE.fullmatch(self.series_id):
                raise ValueError("invalid relationship disclosure series ID")
            if type(self.sequence) is not int or self.sequence < 0:
                raise ValueError("invalid relationship disclosure sequence")
            if self.starts_after and not _CURSOR_RE.fullmatch(
                self.starts_after
            ):
                raise ValueError("invalid relationship disclosure start cursor")
            _subject_ref(self.scope_subject_ref)
            if self.baseline not in {"history-start", "current-cursor"}:
                raise ValueError("invalid relationship disclosure baseline")
            if self.baseline == "history-start" and self.sequence == 0:
                if self.starts_after:
                    raise ValueError(
                        "history-start disclosure cannot begin after a cursor"
                    )
            if self.sequence > 0 and not self.starts_after:
                raise ValueError(
                    "continued relationship disclosure must name its prior cursor"
                )
        else:
            raise ValueError("unsupported relationship disclosure")
        if not _DISCLOSURE_ID_RE.fullmatch(self.disclosure_id):
            raise ValueError("invalid relationship disclosure ID")
        observer = validate_actor_id(self.observer_actor_id)
        audience = validate_actor_id(self.audience_actor_id)
        if observer == audience:
            raise ValueError(
                "relationship disclosure audience must be another Actor"
            )
        if type(self.issued_ms) is not int or self.issued_ms <= 0:
            raise ValueError("invalid relationship disclosure issue time")
        minimum_activities = (
            1 if self.version == RELATIONSHIP_DISCLOSURE_VERSION else 0
        )
        if not minimum_activities <= len(
            self.activities
        ) <= MAX_DISCLOSURE_ACTIVITIES:
            raise ValueError(
                "relationship disclosure has an invalid activity count"
            )
        if not _CURSOR_RE.fullmatch(self.next_cursor):
            raise ValueError("invalid relationship disclosure cursor")

    def content_fields(self) -> dict[str, Any]:
        value = {
            "version": self.version,
            "type": self.object_type,
            "observer_actor_id": self.observer_actor_id,
            "audience_actor_id": self.audience_actor_id,
            "issued_ms": self.issued_ms,
            "activities": [item.to_dict() for item in self.activities],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "ordering": "observer-local-append",
            "privacy": "content-free",
            "visibility": "audience-private",
            "authorization_effect": "none",
        }
        if self.version == RELATIONSHIP_DISCLOSURE_SERIES_VERSION:
            value.update(
                {
                    "series_id": self.series_id,
                    "sequence": self.sequence,
                    "starts_after": self.starts_after,
                    "scope": (
                        "subject" if self.scope_subject_ref else "all"
                    ),
                    "scope_subject_ref": self.scope_subject_ref,
                    "baseline": self.baseline,
                }
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            **self.content_fields(),
        }

    def validate_binding(
        self,
        *,
        sender_node_id: str,
        destination_node_id: str,
        now: int | None = None,
    ) -> None:
        if self.observer_actor_id != validate_actor_id(sender_node_id):
            raise ValueError(
                "relationship disclosure observer is not its Packet sender"
            )
        if self.audience_actor_id != validate_actor_id(destination_node_id):
            raise ValueError(
                "relationship disclosure audience is not its Packet destination"
            )
        if self.issued_ms > _now_ms(now) + MAX_CLOCK_SKEW_MS:
            raise ValueError(
                "relationship disclosure was issued too far in the future"
            )

    @classmethod
    def create(
        cls,
        page: RelationshipActivityPage,
        *,
        audience_actor_id: str,
        now: int | None = None,
    ) -> "RelationshipDisclosure":
        issued_ms = _now_ms(now)
        activities = cls._activities_from_page(page)
        unsigned = cls(
            disclosure_id="rdis_" + ("0" * 64),
            observer_actor_id=page.observer_actor_id,
            audience_actor_id=validate_actor_id(audience_actor_id),
            issued_ms=issued_ms,
            activities=activities,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
        return cls._with_digest(unsigned)

    @classmethod
    def create_series(
        cls,
        page: RelationshipActivityPage,
        *,
        audience_actor_id: str,
        series_id: str,
        sequence: int,
        starts_after: str,
        scope_subject_ref: str = "",
        baseline: str,
        now: int | None = None,
    ) -> "RelationshipDisclosure":
        unsigned = cls(
            disclosure_id="rdis_" + ("0" * 64),
            observer_actor_id=page.observer_actor_id,
            audience_actor_id=validate_actor_id(audience_actor_id),
            issued_ms=_now_ms(now),
            activities=cls._activities_from_page(page),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            series_id=str(series_id).strip().lower(),
            sequence=int(sequence),
            starts_after=str(starts_after).strip(),
            scope_subject_ref=str(scope_subject_ref).strip(),
            baseline=str(baseline).strip(),
            version=RELATIONSHIP_DISCLOSURE_SERIES_VERSION,
            object_type=RELATIONSHIP_DISCLOSURE_SERIES_TYPE,
        )
        return cls._with_digest(unsigned)

    @staticmethod
    def _activities_from_page(
        page: RelationshipActivityPage,
    ) -> tuple[DisclosedRelationshipActivity, ...]:
        return tuple(
            DisclosedRelationshipActivity.from_dict(
                {
                    key: value
                    for key, value in item.to_dict().items()
                    if key in _ACTIVITY_FIELDS
                }
            )
            for item in page.activities
        )

    @classmethod
    def _with_digest(
        cls,
        unsigned: "RelationshipDisclosure",
    ) -> "RelationshipDisclosure":
        disclosure_id = "rdis_" + hashlib.sha256(
            canonical_pack(unsigned.content_fields())
        ).hexdigest()
        return cls(
            **{
                **unsigned.__dict__,
                "disclosure_id": disclosure_id,
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RelationshipDisclosure":
        base_expected = {
            "version",
            "type",
            "disclosure_id",
            "observer_actor_id",
            "audience_actor_id",
            "issued_ms",
            "activities",
            "next_cursor",
            "has_more",
            "ordering",
            "privacy",
            "visibility",
            "authorization_effect",
        }
        version = value.get("version")
        series_fields = {
            "series_id",
            "sequence",
            "starts_after",
            "scope",
            "scope_subject_ref",
            "baseline",
        }
        expected = (
            base_expected | series_fields
            if version == RELATIONSHIP_DISCLOSURE_SERIES_VERSION
            else base_expected
        )
        if set(value) != expected:
            raise ValueError("relationship disclosure has unexpected fields")
        if (
            value.get("ordering") != "observer-local-append"
            or value.get("privacy") != "content-free"
            or value.get("visibility") != "audience-private"
            or value.get("authorization_effect") != "none"
            or type(value.get("has_more")) is not bool
            or type(value.get("version")) is not int
            or type(value.get("issued_ms")) is not int
            or (
                version
                not in {
                    RELATIONSHIP_DISCLOSURE_VERSION,
                    RELATIONSHIP_DISCLOSURE_SERIES_VERSION,
                }
            )
            or any(
                not isinstance(value.get(key), str)
                for key in {
                    "type",
                    "disclosure_id",
                    "observer_actor_id",
                    "audience_actor_id",
                    "next_cursor",
                }
            )
        ):
            raise ValueError("relationship disclosure boundary is invalid")
        raw_activities = value.get("activities")
        if not isinstance(raw_activities, list):
            raise ValueError(
                "relationship disclosure activities must be a list"
            )
        if version == RELATIONSHIP_DISCLOSURE_SERIES_VERSION:
            if (
                type(value.get("sequence")) is not int
                or any(
                    not isinstance(value.get(key), str)
                    for key in {
                        "series_id",
                        "starts_after",
                        "scope",
                        "scope_subject_ref",
                        "baseline",
                    }
                )
                or value.get("scope")
                != (
                    "subject"
                    if value.get("scope_subject_ref")
                    else "all"
                )
            ):
                raise ValueError(
                    "relationship disclosure series boundary is invalid"
                )
        disclosure = cls(
            version=int(value["version"]),
            object_type=str(value["type"]),
            disclosure_id=str(value["disclosure_id"]),
            observer_actor_id=str(value["observer_actor_id"]),
            audience_actor_id=str(value["audience_actor_id"]),
            issued_ms=int(value["issued_ms"]),
            activities=tuple(
                DisclosedRelationshipActivity.from_dict(item)
                for item in raw_activities
                if isinstance(item, Mapping)
            ),
            next_cursor=str(value["next_cursor"]),
            has_more=value["has_more"],
            series_id=str(value.get("series_id", "")),
            sequence=int(value.get("sequence", 0)),
            starts_after=str(value.get("starts_after", "")),
            scope_subject_ref=str(value.get("scope_subject_ref", "")),
            baseline=str(value.get("baseline", "")),
        )
        if len(disclosure.activities) != len(raw_activities):
            raise ValueError(
                "relationship disclosure activity must be an object"
            )
        expected_id = "rdis_" + hashlib.sha256(
            canonical_pack(disclosure.content_fields())
        ).hexdigest()
        if disclosure.disclosure_id != expected_id:
            raise ValueError("relationship disclosure digest is invalid")
        return disclosure


def validate_relationship_disclosure(
    value: Any,
    *,
    sender_node_id: str,
    destination_node_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("relationship disclosure must be an object")
    disclosure = RelationshipDisclosure.from_dict(value)
    disclosure.validate_binding(
        sender_node_id=sender_node_id,
        destination_node_id=destination_node_id,
        now=now,
    )
    return disclosure.to_dict()


@dataclass(frozen=True)
class ReceivedRelationshipDisclosure:
    packet_id: str
    sender_actor_id: str
    received_ms: int
    disclosure: RelationshipDisclosure

    def __post_init__(self) -> None:
        if not _PACKET_ID_RE.fullmatch(self.packet_id):
            raise ValueError("invalid relationship disclosure Packet ID")
        if self.sender_actor_id != self.disclosure.observer_actor_id:
            raise ValueError(
                "relationship disclosure sender does not match observer"
            )
        if type(self.received_ms) is not int or self.received_ms <= 0:
            raise ValueError("invalid relationship disclosure receive time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "sender_actor_id": self.sender_actor_id,
            "received_ms": self.received_ms,
            "disclosure": self.disclosure.to_dict(),
            "source_proof": "authenticated-encrypted-packet",
            "authorization_effect": "none",
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReceivedRelationshipDisclosure":
        expected = {
            "packet_id",
            "sender_actor_id",
            "received_ms",
            "disclosure",
            "source_proof",
            "authorization_effect",
        }
        if set(value) != expected:
            raise ValueError(
                "received relationship disclosure has unexpected fields"
            )
        if (
            value.get("source_proof") != "authenticated-encrypted-packet"
            or value.get("authorization_effect") != "none"
            or not isinstance(value.get("disclosure"), Mapping)
            or not isinstance(value.get("packet_id"), str)
            or not isinstance(value.get("sender_actor_id"), str)
            or type(value.get("received_ms")) is not int
        ):
            raise ValueError("received relationship disclosure boundary is invalid")
        return cls(
            packet_id=str(value["packet_id"]),
            sender_actor_id=str(value["sender_actor_id"]),
            received_ms=int(value["received_ms"]),
            disclosure=RelationshipDisclosure.from_dict(value["disclosure"]),
        )


class RelationshipDisclosureBook:
    """Store trusted received disclosures without changing local relations."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = validate_actor_id(own_actor_id)
        self._received: dict[str, ReceivedRelationshipDisclosure] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._received = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or int(value.get("version", 0)) != 1
            or value.get("own_actor_id") != self.own_actor_id
            or not isinstance(value.get("received"), list)
        ):
            raise ValueError("invalid relationship disclosure book")
        received: dict[str, ReceivedRelationshipDisclosure] = {}
        disclosure_ids: set[str] = set()
        for raw in value["received"]:
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "relationship disclosure book entry must be an object"
                )
            item = ReceivedRelationshipDisclosure.from_dict(raw)
            item.disclosure.validate_binding(
                sender_node_id=item.sender_actor_id,
                destination_node_id=self.own_actor_id,
            )
            if (
                item.packet_id in received
                or item.disclosure.disclosure_id in disclosure_ids
            ):
                raise ValueError(
                    "relationship disclosure book contains a duplicate"
                )
            received[item.packet_id] = item
            disclosure_ids.add(item.disclosure.disclosure_id)
        self._received = received

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": 1,
                "own_actor_id": self.own_actor_id,
                "received": [
                    self._received[key].to_dict()
                    for key in sorted(
                        self._received,
                        key=lambda packet_id: (
                            self._received[packet_id].received_ms,
                            packet_id,
                        ),
                    )
                ],
            },
            private=True,
        )

    def add(
        self,
        disclosure: RelationshipDisclosure,
        *,
        packet_id: str,
        sender_actor_id: str,
        received_ms: int | None = None,
    ) -> bool:
        disclosure.validate_binding(
            sender_node_id=sender_actor_id,
            destination_node_id=self.own_actor_id,
        )
        item = ReceivedRelationshipDisclosure(
            packet_id=str(packet_id).strip().lower(),
            sender_actor_id=validate_actor_id(sender_actor_id),
            received_ms=_now_ms(received_ms),
            disclosure=disclosure,
        )
        if item.packet_id in self._received or any(
            current.disclosure.disclosure_id
            == item.disclosure.disclosure_id
            for current in self._received.values()
        ):
            return False
        self._received[item.packet_id] = item
        self.save()
        return True

    def all(
        self,
        *,
        sender_actor_id: str = "",
        limit: int | None = 100,
    ) -> tuple[ReceivedRelationshipDisclosure, ...]:
        page_limit = None if limit is None else int(limit)
        if page_limit is not None and not 1 <= page_limit <= 500:
            raise ValueError("relationship disclosure list limit must be 1-500")
        sender = (
            validate_actor_id(sender_actor_id)
            if str(sender_actor_id).strip()
            else ""
        )
        items = sorted(
            self._received.values(),
            key=lambda item: (item.received_ms, item.packet_id),
            reverse=True,
        )
        if sender:
            items = [item for item in items if item.sender_actor_id == sender]
        return tuple(items if page_limit is None else items[:page_limit])
