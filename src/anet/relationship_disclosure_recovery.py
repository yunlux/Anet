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
from .relationship_disclosures import RelationshipDisclosure


RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND = (
    "social.relationship.disclosure.gap-notice"
)
RELATIONSHIP_DISCLOSURE_GAP_NOTICE_TYPE = (
    "anet.relationship.disclosure-gap-notice.v1"
)
MAX_GAP_SEQUENCES = 100
MAX_ARCHIVED_DISCLOSURES = 1000
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

_NOTICE_ID_RE = re.compile(r"^rgap_[0-9a-f]{64}$")
_SERIES_ID_RE = re.compile(r"^rdsr_[0-9a-f]{32}$")
_SCHEDULE_ID_RE = re.compile(r"^rdsc_[0-9a-f]{32}$")
_PACKET_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


@dataclass(frozen=True)
class RelationshipDisclosureGapNotice:
    notice_id: str
    reporter_actor_id: str
    observer_actor_id: str
    series_id: str
    missing_sequences: tuple[int, ...]
    detected_through_sequence: int
    issued_ms: int

    def __post_init__(self) -> None:
        if not _NOTICE_ID_RE.fullmatch(self.notice_id):
            raise ValueError("invalid relationship disclosure gap notice ID")
        reporter = validate_actor_id(self.reporter_actor_id)
        observer = validate_actor_id(self.observer_actor_id)
        if reporter == observer:
            raise ValueError("gap notice observer must be another Actor")
        if not _SERIES_ID_RE.fullmatch(self.series_id):
            raise ValueError("invalid relationship disclosure series ID")
        if (
            not self.missing_sequences
            or len(self.missing_sequences) > MAX_GAP_SEQUENCES
            or any(
                type(sequence) is not int
                or sequence < 0
                or sequence > self.detected_through_sequence
                for sequence in self.missing_sequences
            )
            or tuple(sorted(set(self.missing_sequences)))
            != self.missing_sequences
        ):
            raise ValueError("invalid relationship disclosure gap sequences")
        if (
            type(self.detected_through_sequence) is not int
            or self.detected_through_sequence < 0
        ):
            raise ValueError("invalid relationship disclosure gap horizon")
        if type(self.issued_ms) is not int or self.issued_ms <= 0:
            raise ValueError("invalid relationship disclosure gap notice time")

    def content_fields(self) -> dict[str, Any]:
        return {
            "version": 1,
            "type": RELATIONSHIP_DISCLOSURE_GAP_NOTICE_TYPE,
            "reporter_actor_id": self.reporter_actor_id,
            "observer_actor_id": self.observer_actor_id,
            "series_id": self.series_id,
            "missing_sequences": list(self.missing_sequences),
            "detected_through_sequence": self.detected_through_sequence,
            "issued_ms": self.issued_ms,
            "meaning": "delivery-gap-observed",
            "requested_action": "none",
            "scope_change": False,
            "authorization_effect": "none",
        }

    def to_dict(self) -> dict[str, Any]:
        return {"notice_id": self.notice_id, **self.content_fields()}

    def validate_binding(
        self,
        *,
        sender_node_id: str,
        destination_node_id: str,
        now: int | None = None,
    ) -> None:
        if self.reporter_actor_id != validate_actor_id(sender_node_id):
            raise ValueError("gap notice reporter is not its Packet sender")
        if self.observer_actor_id != validate_actor_id(destination_node_id):
            raise ValueError("gap notice observer is not its Packet destination")
        if self.issued_ms > _now_ms(now) + MAX_CLOCK_SKEW_MS:
            raise ValueError("gap notice was issued too far in the future")

    @classmethod
    def create(
        cls,
        *,
        reporter_actor_id: str,
        observer_actor_id: str,
        series_id: str,
        missing_sequences: list[int] | tuple[int, ...],
        detected_through_sequence: int,
        now: int | None = None,
    ) -> "RelationshipDisclosureGapNotice":
        unsigned = cls(
            notice_id="rgap_" + ("0" * 64),
            reporter_actor_id=validate_actor_id(reporter_actor_id),
            observer_actor_id=validate_actor_id(observer_actor_id),
            series_id=str(series_id).strip().lower(),
            missing_sequences=tuple(
                sorted(set(int(item) for item in missing_sequences))
            ),
            detected_through_sequence=int(detected_through_sequence),
            issued_ms=_now_ms(now),
        )
        return cls(
            **{
                **unsigned.__dict__,
                "notice_id": "rgap_"
                + hashlib.sha256(
                    canonical_pack(unsigned.content_fields())
                ).hexdigest(),
            }
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RelationshipDisclosureGapNotice":
        expected = {
            "version",
            "type",
            "notice_id",
            "reporter_actor_id",
            "observer_actor_id",
            "series_id",
            "missing_sequences",
            "detected_through_sequence",
            "issued_ms",
            "meaning",
            "requested_action",
            "scope_change",
            "authorization_effect",
        }
        if set(value) != expected:
            raise ValueError("gap notice has unexpected fields")
        if (
            value.get("version") != 1
            or value.get("type")
            != RELATIONSHIP_DISCLOSURE_GAP_NOTICE_TYPE
            or value.get("meaning") != "delivery-gap-observed"
            or value.get("requested_action") != "none"
            or value.get("scope_change") is not False
            or value.get("authorization_effect") != "none"
            or not isinstance(value.get("missing_sequences"), list)
            or any(
                type(item) is not int
                for item in value.get("missing_sequences", [])
            )
            or type(value.get("detected_through_sequence")) is not int
            or type(value.get("issued_ms")) is not int
            or any(
                not isinstance(value.get(key), str)
                for key in {
                    "notice_id",
                    "reporter_actor_id",
                    "observer_actor_id",
                    "series_id",
                }
            )
        ):
            raise ValueError("gap notice boundary is invalid")
        notice = cls(
            notice_id=str(value["notice_id"]),
            reporter_actor_id=str(value["reporter_actor_id"]),
            observer_actor_id=str(value["observer_actor_id"]),
            series_id=str(value["series_id"]),
            missing_sequences=tuple(value["missing_sequences"]),
            detected_through_sequence=int(value["detected_through_sequence"]),
            issued_ms=int(value["issued_ms"]),
        )
        expected_id = "rgap_" + hashlib.sha256(
            canonical_pack(notice.content_fields())
        ).hexdigest()
        if notice.notice_id != expected_id:
            raise ValueError("gap notice digest is invalid")
        return notice


def validate_relationship_disclosure_gap_notice(
    value: Any,
    *,
    sender_node_id: str,
    destination_node_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("gap notice must be an object")
    notice = RelationshipDisclosureGapNotice.from_dict(value)
    notice.validate_binding(
        sender_node_id=sender_node_id,
        destination_node_id=destination_node_id,
        now=now,
    )
    return notice.to_dict()


@dataclass(frozen=True)
class ReceivedRelationshipDisclosureGapNotice:
    packet_id: str
    sender_actor_id: str
    received_ms: int
    notice: RelationshipDisclosureGapNotice

    def __post_init__(self) -> None:
        if not _PACKET_ID_RE.fullmatch(self.packet_id):
            raise ValueError("invalid gap notice Packet ID")
        if self.sender_actor_id != self.notice.reporter_actor_id:
            raise ValueError("gap notice sender does not match reporter")
        if type(self.received_ms) is not int or self.received_ms <= 0:
            raise ValueError("invalid gap notice receive time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "sender_actor_id": self.sender_actor_id,
            "received_ms": self.received_ms,
            "notice": self.notice.to_dict(),
            "source_proof": "authenticated-encrypted-packet",
            "authorization_effect": "none",
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ReceivedRelationshipDisclosureGapNotice":
        if set(value) != {
            "packet_id",
            "sender_actor_id",
            "received_ms",
            "notice",
            "source_proof",
            "authorization_effect",
        }:
            raise ValueError("received gap notice has unexpected fields")
        if (
            value.get("source_proof") != "authenticated-encrypted-packet"
            or value.get("authorization_effect") != "none"
            or not isinstance(value.get("notice"), Mapping)
            or not isinstance(value.get("packet_id"), str)
            or not isinstance(value.get("sender_actor_id"), str)
            or type(value.get("received_ms")) is not int
        ):
            raise ValueError("received gap notice boundary is invalid")
        return cls(
            packet_id=str(value["packet_id"]),
            sender_actor_id=str(value["sender_actor_id"]),
            received_ms=int(value["received_ms"]),
            notice=RelationshipDisclosureGapNotice.from_dict(value["notice"]),
        )


class RelationshipDisclosureGapNoticeBook:
    """Persist authenticated advisory gap notices for the local observer."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = validate_actor_id(own_actor_id)
        self._received: dict[str, ReceivedRelationshipDisclosureGapNotice] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._received = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != 1
            or value.get("own_actor_id") != self.own_actor_id
            or not isinstance(value.get("received"), list)
        ):
            raise ValueError("invalid relationship disclosure gap notice book")
        received: dict[str, ReceivedRelationshipDisclosureGapNotice] = {}
        notice_ids: set[str] = set()
        for raw in value["received"]:
            if not isinstance(raw, Mapping):
                raise ValueError("gap notice book entry must be an object")
            item = ReceivedRelationshipDisclosureGapNotice.from_dict(raw)
            item.notice.validate_binding(
                sender_node_id=item.sender_actor_id,
                destination_node_id=self.own_actor_id,
            )
            if item.packet_id in received or item.notice.notice_id in notice_ids:
                raise ValueError("gap notice book contains a duplicate")
            received[item.packet_id] = item
            notice_ids.add(item.notice.notice_id)
        self._received = received

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": 1,
                "own_actor_id": self.own_actor_id,
                "received": [
                    item.to_dict()
                    for item in sorted(
                        self._received.values(),
                        key=lambda current: (
                            current.received_ms,
                            current.packet_id,
                        ),
                    )
                ],
            },
            private=True,
        )

    def add(
        self,
        notice: RelationshipDisclosureGapNotice,
        *,
        packet_id: str,
        sender_actor_id: str,
        received_ms: int | None = None,
    ) -> bool:
        notice.validate_binding(
            sender_node_id=sender_actor_id,
            destination_node_id=self.own_actor_id,
        )
        item = ReceivedRelationshipDisclosureGapNotice(
            packet_id=str(packet_id).strip().lower(),
            sender_actor_id=validate_actor_id(sender_actor_id),
            received_ms=_now_ms(received_ms),
            notice=notice,
        )
        if item.packet_id in self._received or any(
            current.notice.notice_id == notice.notice_id
            for current in self._received.values()
        ):
            return False
        self._received[item.packet_id] = item
        self.save()
        return True

    def require(self, notice_id: str) -> ReceivedRelationshipDisclosureGapNotice:
        normalized = str(notice_id).strip().lower()
        for item in self._received.values():
            if item.notice.notice_id == normalized:
                return item
        raise KeyError(f"unknown relationship disclosure gap notice: {normalized}")

    def all(
        self,
        *,
        reporter_actor_id: str = "",
        limit: int = 100,
    ) -> tuple[ReceivedRelationshipDisclosureGapNotice, ...]:
        page_limit = int(limit)
        if not 1 <= page_limit <= 500:
            raise ValueError("gap notice list limit must be 1-500")
        reporter = (
            validate_actor_id(reporter_actor_id)
            if str(reporter_actor_id).strip()
            else ""
        )
        items = sorted(
            self._received.values(),
            key=lambda item: (item.received_ms, item.packet_id),
            reverse=True,
        )
        if reporter:
            items = [
                item for item in items if item.sender_actor_id == reporter
            ]
        return tuple(items[:page_limit])


@dataclass(frozen=True)
class ArchivedRelationshipDisclosure:
    schedule_id: str
    packet_id: str
    archived_ms: int
    disclosure: RelationshipDisclosure

    def __post_init__(self) -> None:
        if not _SCHEDULE_ID_RE.fullmatch(self.schedule_id):
            raise ValueError("invalid archived disclosure schedule ID")
        if not _PACKET_ID_RE.fullmatch(self.packet_id):
            raise ValueError("invalid archived disclosure Packet ID")
        if type(self.archived_ms) is not int or self.archived_ms <= 0:
            raise ValueError("invalid archived disclosure time")
        if self.disclosure.version != 2:
            raise ValueError("only disclosure series pages can be archived")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "packet_id": self.packet_id,
            "archived_ms": self.archived_ms,
            "disclosure": self.disclosure.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArchivedRelationshipDisclosure":
        if set(value) != {
            "schedule_id",
            "packet_id",
            "archived_ms",
            "disclosure",
        }:
            raise ValueError("archived disclosure has unexpected fields")
        if (
            not isinstance(value.get("schedule_id"), str)
            or not isinstance(value.get("packet_id"), str)
            or type(value.get("archived_ms")) is not int
            or not isinstance(value.get("disclosure"), Mapping)
        ):
            raise ValueError("archived disclosure boundary is invalid")
        return cls(
            schedule_id=str(value["schedule_id"]),
            packet_id=str(value["packet_id"]),
            archived_ms=int(value["archived_ms"]),
            disclosure=RelationshipDisclosure.from_dict(value["disclosure"]),
        )


class RelationshipDisclosureArchiveBook:
    """Retain exact scheduled pages so an active observer can retransmit."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = validate_actor_id(own_actor_id)
        self._items: dict[tuple[str, int], ArchivedRelationshipDisclosure] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._items = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != 1
            or value.get("own_actor_id") != self.own_actor_id
            or not isinstance(value.get("archived"), list)
        ):
            raise ValueError("invalid relationship disclosure archive")
        items: dict[tuple[str, int], ArchivedRelationshipDisclosure] = {}
        for raw in value["archived"]:
            if not isinstance(raw, Mapping):
                raise ValueError("archived disclosure must be an object")
            item = ArchivedRelationshipDisclosure.from_dict(raw)
            if item.disclosure.observer_actor_id != self.own_actor_id:
                raise ValueError("archived disclosure belongs to another observer")
            key = (item.disclosure.series_id, item.disclosure.sequence)
            if key in items:
                raise ValueError("duplicate archived disclosure sequence")
            items[key] = item
        self._items = items

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": 1,
                "own_actor_id": self.own_actor_id,
                "archived": [
                    item.to_dict()
                    for item in sorted(
                        self._items.values(),
                        key=lambda current: (
                            current.archived_ms,
                            current.disclosure.series_id,
                            current.disclosure.sequence,
                        ),
                    )
                ],
            },
            private=True,
        )

    def add(
        self,
        schedule_id: str,
        packet_id: str,
        disclosure: RelationshipDisclosure,
        *,
        archived_ms: int | None = None,
    ) -> bool:
        if disclosure.observer_actor_id != self.own_actor_id:
            raise ValueError("cannot archive another observer's disclosure")
        item = ArchivedRelationshipDisclosure(
            schedule_id=str(schedule_id).strip().lower(),
            packet_id=str(packet_id).strip().lower(),
            archived_ms=_now_ms(archived_ms),
            disclosure=disclosure,
        )
        key = (disclosure.series_id, disclosure.sequence)
        previous = self._items.get(key)
        if previous is not None:
            if previous.disclosure != disclosure:
                raise ValueError("archived disclosure sequence conflicts")
            return False
        self._items[key] = item
        if len(self._items) > MAX_ARCHIVED_DISCLOSURES:
            oldest = min(
                self._items,
                key=lambda current: (
                    self._items[current].archived_ms,
                    current,
                ),
            )
            del self._items[oldest]
        self.save()
        return True

    def find(
        self,
        series_id: str,
        sequence: int,
    ) -> ArchivedRelationshipDisclosure | None:
        return self._items.get(
            (str(series_id).strip().lower(), int(sequence))
        )
