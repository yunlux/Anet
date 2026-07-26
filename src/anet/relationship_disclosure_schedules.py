from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .actors import validate_actor_id
from .encoding import atomic_json
from .relationship_disclosures import RelationshipDisclosure


RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION = 2
RELATIONSHIP_DISCLOSURE_SCHEDULE_TYPE = (
    "anet.relationship.disclosure-schedule.v2"
)
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 86400
MAX_SCHEDULE_LIFETIME_SECONDS = 365 * 86400

_SCHEDULE_ID_RE = re.compile(r"^rdsc_[0-9a-f]{32}$")
_SERIES_ID_RE = re.compile(r"^rdsr_[0-9a-f]{32}$")
_SUBJECT_REF_RE = re.compile(r"^subj_[0-9a-f]{16}$")
_CURSOR_RE = re.compile(r"^(|rac_[0-9a-f]{16}_revt_[0-9a-f]{24})$")
_PACKET_ID_RE = re.compile(r"^(|[0-9a-f]{32})$")


def _now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


def _bounded_reason(value: str) -> str:
    reason = str(value).strip()
    if len(reason) > 128:
        raise ValueError("relationship disclosure schedule reason is too long")
    return reason


@dataclass(frozen=True)
class PendingRelationshipDisclosure:
    disclosure: RelationshipDisclosure
    start_cursor: str
    next_cursor: str
    prepared_ms: int

    def __post_init__(self) -> None:
        if not _CURSOR_RE.fullmatch(self.start_cursor):
            raise ValueError("invalid pending disclosure start cursor")
        if self.next_cursor != self.disclosure.next_cursor:
            raise ValueError("pending disclosure cursor does not match body")
        if type(self.prepared_ms) is not int or self.prepared_ms <= 0:
            raise ValueError("invalid pending disclosure preparation time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure": self.disclosure.to_dict(),
            "start_cursor": self.start_cursor,
            "next_cursor": self.next_cursor,
            "prepared_ms": self.prepared_ms,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "PendingRelationshipDisclosure":
        if set(value) != {
            "disclosure",
            "start_cursor",
            "next_cursor",
            "prepared_ms",
        }:
            raise ValueError("pending disclosure has unexpected fields")
        disclosure = value.get("disclosure")
        if not isinstance(disclosure, Mapping):
            raise ValueError("pending disclosure body must be an object")
        return cls(
            disclosure=RelationshipDisclosure.from_dict(disclosure),
            start_cursor=str(value.get("start_cursor", "")),
            next_cursor=str(value.get("next_cursor", "")),
            prepared_ms=int(value.get("prepared_ms", 0)),
        )


@dataclass(frozen=True)
class RelationshipDisclosureSchedule:
    schedule_id: str
    observer_actor_id: str
    audience_actor_id: str
    subject_ref: str
    interval_seconds: int
    batch_limit: int
    packet_ttl_seconds: int
    created_ms: int
    expires_ms: int
    cursor: str
    next_due_ms: int
    series_id: str
    next_sequence: int
    baseline: str
    revoked_ms: int = 0
    revoke_reason: str = ""
    last_attempt_ms: int = 0
    last_success_ms: int = 0
    last_packet_id: str = ""
    failure_count: int = 0
    last_error: str = ""
    pending: PendingRelationshipDisclosure | None = None
    version: int = RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION
    object_type: str = RELATIONSHIP_DISCLOSURE_SCHEDULE_TYPE

    def __post_init__(self) -> None:
        if (
            self.version != RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION
            or self.object_type != RELATIONSHIP_DISCLOSURE_SCHEDULE_TYPE
        ):
            raise ValueError("unsupported relationship disclosure schedule")
        if not _SCHEDULE_ID_RE.fullmatch(self.schedule_id):
            raise ValueError("invalid relationship disclosure schedule ID")
        if not _SERIES_ID_RE.fullmatch(self.series_id):
            raise ValueError("invalid relationship disclosure series ID")
        if type(self.next_sequence) is not int or self.next_sequence < 0:
            raise ValueError("invalid relationship disclosure next sequence")
        if self.baseline not in {"history-start", "current-cursor"}:
            raise ValueError("invalid relationship disclosure schedule baseline")
        observer = validate_actor_id(self.observer_actor_id)
        audience = validate_actor_id(self.audience_actor_id)
        if observer == audience:
            raise ValueError("schedule audience must be another Actor")
        if self.subject_ref and not _SUBJECT_REF_RE.fullmatch(self.subject_ref):
            raise ValueError("invalid relationship disclosure schedule Subject")
        if not MIN_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValueError("schedule interval must be 30-86400 seconds")
        if not 1 <= self.batch_limit <= 100:
            raise ValueError("schedule batch limit must be 1-100")
        if not 60 <= self.packet_ttl_seconds <= 30 * 86400:
            raise ValueError("schedule Packet lifetime must be 60-2592000 seconds")
        if (
            type(self.created_ms) is not int
            or type(self.expires_ms) is not int
            or self.created_ms <= 0
            or self.expires_ms <= self.created_ms
            or self.expires_ms - self.created_ms
            > MAX_SCHEDULE_LIFETIME_SECONDS * 1000
        ):
            raise ValueError("invalid relationship disclosure schedule lifetime")
        if not _CURSOR_RE.fullmatch(self.cursor):
            raise ValueError("invalid relationship disclosure schedule cursor")
        if type(self.next_due_ms) is not int or self.next_due_ms < self.created_ms:
            raise ValueError("invalid relationship disclosure schedule due time")
        if self.revoked_ms and self.revoked_ms < self.created_ms:
            raise ValueError("invalid relationship disclosure revocation time")
        if not _PACKET_ID_RE.fullmatch(self.last_packet_id):
            raise ValueError("invalid relationship disclosure Packet ID")
        if self.failure_count < 0:
            raise ValueError("invalid relationship disclosure failure count")
        _bounded_reason(self.revoke_reason)
        _bounded_reason(self.last_error)
        if self.revoked_ms and self.pending is not None:
            raise ValueError("revoked schedule cannot retain pending disclosure")
        if self.pending is not None:
            if self.pending.start_cursor != self.cursor:
                raise ValueError("pending disclosure does not start at schedule cursor")
            disclosure = self.pending.disclosure
            if (
                disclosure.observer_actor_id != observer
                or disclosure.audience_actor_id != audience
            ):
                raise ValueError("pending disclosure does not match schedule actors")
            if disclosure.version == 2 and (
                disclosure.series_id != self.series_id
                or disclosure.sequence != self.next_sequence
                or disclosure.starts_after != self.cursor
                or disclosure.scope_subject_ref != self.subject_ref
                or disclosure.baseline != self.baseline
            ):
                raise ValueError(
                    "pending disclosure does not match schedule series"
                )
            if disclosure.version == 1 and (
                self.next_sequence != 0
                or self.baseline != "current-cursor"
            ):
                raise ValueError(
                    "legacy pending disclosure is outside migration boundary"
                )

    @property
    def scope(self) -> str:
        return "subject" if self.subject_ref else "all"

    def state(self, *, now: int | None = None) -> str:
        current = _now_ms(now)
        if self.revoked_ms:
            return "revoked"
        if current >= self.expires_ms:
            return "expired"
        return "active"

    def due(self, *, now: int | None = None) -> bool:
        current = _now_ms(now)
        return self.state(now=current) == "active" and current >= self.next_due_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "schedule_id": self.schedule_id,
            "observer_actor_id": self.observer_actor_id,
            "audience_actor_id": self.audience_actor_id,
            "scope": self.scope,
            "subject_ref": self.subject_ref,
            "interval_seconds": self.interval_seconds,
            "batch_limit": self.batch_limit,
            "packet_ttl_seconds": self.packet_ttl_seconds,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "cursor": self.cursor,
            "next_due_ms": self.next_due_ms,
            "series_id": self.series_id,
            "next_sequence": self.next_sequence,
            "baseline": self.baseline,
            "revoked_ms": self.revoked_ms,
            "revoke_reason": self.revoke_reason,
            "last_attempt_ms": self.last_attempt_ms,
            "last_success_ms": self.last_success_ms,
            "last_packet_id": self.last_packet_id,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "pending": self.pending.to_dict() if self.pending else None,
            "control": "observer-local",
            "audience_pull": False,
            "authorization_effect": "disclosure-only",
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "RelationshipDisclosureSchedule":
        base_expected = {
            "version",
            "type",
            "schedule_id",
            "observer_actor_id",
            "audience_actor_id",
            "scope",
            "subject_ref",
            "interval_seconds",
            "batch_limit",
            "packet_ttl_seconds",
            "created_ms",
            "expires_ms",
            "cursor",
            "next_due_ms",
            "revoked_ms",
            "revoke_reason",
            "last_attempt_ms",
            "last_success_ms",
            "last_packet_id",
            "failure_count",
            "last_error",
            "pending",
            "control",
            "audience_pull",
            "authorization_effect",
        }
        series_fields = {"series_id", "next_sequence", "baseline"}
        version = value.get("version")
        expected = (
            base_expected | series_fields
            if version == RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION
            else base_expected
        )
        if set(value) != expected:
            raise ValueError("relationship disclosure schedule has unexpected fields")
        integer_fields = {
            "version",
            "interval_seconds",
            "batch_limit",
            "packet_ttl_seconds",
            "created_ms",
            "expires_ms",
            "next_due_ms",
            "revoked_ms",
            "last_attempt_ms",
            "last_success_ms",
            "failure_count",
        }
        if version == RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION:
            integer_fields.add("next_sequence")
        string_fields = {
            "type",
            "schedule_id",
            "observer_actor_id",
            "audience_actor_id",
            "scope",
            "subject_ref",
            "cursor",
            "revoke_reason",
            "last_packet_id",
            "last_error",
            "control",
            "authorization_effect",
        }
        if version == RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION:
            string_fields.update({"series_id", "baseline"})
        if (
            any(type(value.get(key)) is not int for key in integer_fields)
            or any(not isinstance(value.get(key), str) for key in string_fields)
            or value.get("control") != "observer-local"
            or value.get("audience_pull") is not False
            or value.get("authorization_effect") != "disclosure-only"
            or value.get("scope")
            != ("subject" if value.get("subject_ref") else "all")
        ):
            raise ValueError("relationship disclosure schedule boundary is invalid")
        if (
            version == 1
            and value.get("type")
            != "anet.relationship.disclosure-schedule.v1"
        ) or (
            version == RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION
            and value.get("type") != RELATIONSHIP_DISCLOSURE_SCHEDULE_TYPE
        ):
            raise ValueError("relationship disclosure schedule type is invalid")
        pending = value.get("pending")
        if pending is not None and not isinstance(pending, Mapping):
            raise ValueError("relationship disclosure schedule pending is invalid")
        if version not in {1, RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION}:
            raise ValueError("unsupported relationship disclosure schedule")
        schedule_id = str(value["schedule_id"])
        migrated_series = "rdsr_" + hashlib.sha256(
            f"anet-schedule-migration:{schedule_id}".encode("utf-8")
        ).hexdigest()[:32]
        return cls(
            version=RELATIONSHIP_DISCLOSURE_SCHEDULE_VERSION,
            object_type=RELATIONSHIP_DISCLOSURE_SCHEDULE_TYPE,
            schedule_id=schedule_id,
            observer_actor_id=str(value["observer_actor_id"]),
            audience_actor_id=str(value["audience_actor_id"]),
            subject_ref=str(value["subject_ref"]),
            interval_seconds=int(value["interval_seconds"]),
            batch_limit=int(value["batch_limit"]),
            packet_ttl_seconds=int(value["packet_ttl_seconds"]),
            created_ms=int(value["created_ms"]),
            expires_ms=int(value["expires_ms"]),
            cursor=str(value["cursor"]),
            next_due_ms=int(value["next_due_ms"]),
            series_id=str(value.get("series_id", migrated_series)),
            next_sequence=int(value.get("next_sequence", 0)),
            baseline=str(value.get("baseline", "current-cursor")),
            revoked_ms=int(value["revoked_ms"]),
            revoke_reason=str(value["revoke_reason"]),
            last_attempt_ms=int(value["last_attempt_ms"]),
            last_success_ms=int(value["last_success_ms"]),
            last_packet_id=str(value["last_packet_id"]),
            failure_count=int(value["failure_count"]),
            last_error=str(value["last_error"]),
            pending=(
                PendingRelationshipDisclosure.from_dict(pending)
                if isinstance(pending, Mapping)
                else None
            ),
        )


class RelationshipDisclosureScheduleBook:
    """Persist observer-controlled disclosure instructions and retry state."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = validate_actor_id(own_actor_id)
        self._schedules: dict[str, RelationshipDisclosureSchedule] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._schedules = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or type(value.get("version")) is not int
            or value.get("version") not in {1, 2}
            or value.get("own_actor_id") != self.own_actor_id
            or not isinstance(value.get("schedules"), list)
        ):
            raise ValueError("invalid relationship disclosure schedule book")
        schedules: dict[str, RelationshipDisclosureSchedule] = {}
        for raw in value["schedules"]:
            if not isinstance(raw, Mapping):
                raise ValueError("relationship disclosure schedule must be an object")
            item = RelationshipDisclosureSchedule.from_dict(raw)
            if (
                item.observer_actor_id != self.own_actor_id
                or item.schedule_id in schedules
            ):
                raise ValueError("invalid or duplicate relationship disclosure schedule")
            schedules[item.schedule_id] = item
        self._schedules = schedules

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": 2,
                "own_actor_id": self.own_actor_id,
                "schedules": [
                    self._schedules[key].to_dict()
                    for key in sorted(self._schedules)
                ],
            },
            private=True,
        )

    def create(
        self,
        audience_actor_id: str,
        *,
        cursor: str,
        subject_ref: str = "",
        interval_seconds: int = 300,
        batch_limit: int = 100,
        packet_ttl_seconds: int = 7 * 86400,
        lifetime_seconds: int = 30 * 86400,
        baseline: str = "current-cursor",
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        current = _now_ms(now)
        lifetime = int(lifetime_seconds)
        if not 60 <= lifetime <= MAX_SCHEDULE_LIFETIME_SECONDS:
            raise ValueError("schedule lifetime must be 60-31536000 seconds")
        item = RelationshipDisclosureSchedule(
            schedule_id="rdsc_" + secrets.token_hex(16),
            observer_actor_id=self.own_actor_id,
            audience_actor_id=audience_actor_id,
            subject_ref=str(subject_ref).strip(),
            interval_seconds=int(interval_seconds),
            batch_limit=int(batch_limit),
            packet_ttl_seconds=int(packet_ttl_seconds),
            created_ms=current,
            expires_ms=current + lifetime * 1000,
            cursor=str(cursor).strip(),
            next_due_ms=current,
            series_id="rdsr_" + secrets.token_hex(16),
            next_sequence=0,
            baseline=str(baseline).strip(),
        )
        self._schedules[item.schedule_id] = item
        self.save()
        return item

    def all(self) -> tuple[RelationshipDisclosureSchedule, ...]:
        return tuple(
            sorted(
                self._schedules.values(),
                key=lambda item: (item.created_ms, item.schedule_id),
                reverse=True,
            )
        )

    def require(self, schedule_id: str) -> RelationshipDisclosureSchedule:
        normalized = str(schedule_id).strip().lower()
        item = self._schedules.get(normalized)
        if item is None:
            raise KeyError(f"unknown relationship disclosure schedule: {normalized}")
        return item

    def replace(
        self,
        item: RelationshipDisclosureSchedule,
    ) -> RelationshipDisclosureSchedule:
        if (
            item.observer_actor_id != self.own_actor_id
            or item.schedule_id not in self._schedules
        ):
            raise ValueError("relationship disclosure schedule does not belong here")
        self._schedules[item.schedule_id] = item
        self.save()
        return item

    def revoke(
        self,
        schedule_id: str,
        *,
        reason: str = "",
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        if item.revoked_ms:
            return item
        revoked = replace(
            item,
            revoked_ms=_now_ms(now),
            revoke_reason=_bounded_reason(reason),
            pending=None,
            next_due_ms=max(item.next_due_ms, _now_ms(now)),
        )
        return self.replace(revoked)

    def due(
        self,
        *,
        now: int | None = None,
        schedule_id: str = "",
    ) -> tuple[RelationshipDisclosureSchedule, ...]:
        if schedule_id:
            item = self.require(schedule_id)
            return (item,) if item.due(now=now) else ()
        return tuple(item for item in self.all() if item.due(now=now))

    def prepare(
        self,
        schedule_id: str,
        disclosure: RelationshipDisclosure,
        *,
        start_cursor: str,
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        if item.state(now=now) != "active":
            raise ValueError("relationship disclosure schedule is not active")
        if item.pending is not None:
            return item
        if disclosure.version != 2:
            raise ValueError(
                "new scheduled disclosures must use a continuity series"
            )
        prepared = replace(
            item,
            pending=PendingRelationshipDisclosure(
                disclosure=disclosure,
                start_cursor=start_cursor,
                next_cursor=disclosure.next_cursor,
                prepared_ms=_now_ms(now),
            ),
        )
        return self.replace(prepared)

    def record_idle(
        self,
        schedule_id: str,
        *,
        cursor: str | None = None,
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        current = _now_ms(now)
        return self.replace(
            replace(
                item,
                cursor=item.cursor if cursor is None else str(cursor).strip(),
                last_attempt_ms=current,
                next_due_ms=current + item.interval_seconds * 1000,
                last_error="",
            )
        )

    def discard_expired_pending(
        self,
        schedule_id: str,
        *,
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        if item.state(now=now) != "expired" or item.pending is None:
            return item
        return self.replace(replace(item, pending=None))

    def record_success(
        self,
        schedule_id: str,
        packet_id: str,
        *,
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        if item.pending is None:
            raise ValueError("schedule has no pending disclosure")
        current = _now_ms(now)
        return self.replace(
            replace(
                item,
                cursor=item.pending.next_cursor,
                next_sequence=(
                    item.next_sequence + 1
                    if item.pending.disclosure.version == 2
                    else item.next_sequence
                ),
                pending=None,
                last_attempt_ms=current,
                last_success_ms=current,
                last_packet_id=str(packet_id).strip().lower(),
                failure_count=0,
                last_error="",
                next_due_ms=current + item.interval_seconds * 1000,
            )
        )

    def record_failure(
        self,
        schedule_id: str,
        error: str,
        *,
        now: int | None = None,
    ) -> RelationshipDisclosureSchedule:
        item = self.require(schedule_id)
        current = _now_ms(now)
        failures = min(item.failure_count + 1, 16)
        delay = min(
            item.interval_seconds * (2 ** min(failures - 1, 6)),
            MAX_INTERVAL_SECONDS,
        )
        return self.replace(
            replace(
                item,
                last_attempt_ms=current,
                failure_count=failures,
                last_error=_bounded_reason(error),
                next_due_ms=current + delay * 1000,
            )
        )
