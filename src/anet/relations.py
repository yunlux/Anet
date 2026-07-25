from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .encoding import atomic_json
from .identity import PeerCard


RELATION_BOOK_VERSION = 1
RELATION_CIRCLES = (
    "public",
    "known",
    "collab",
    "friend",
    "close",
    "family",
)


@dataclass(frozen=True)
class RelationshipRecord:
    subject_ref: str
    actor_id: str
    actor_label: str
    circle: str
    state: str
    relationship_labels: tuple[str, ...]
    subject_confidence: int
    relationship_confidence: int
    evidence_refs: tuple[str, ...]
    updated_ms: int

    def __post_init__(self) -> None:
        if not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid local subject reference")
        if not self.actor_id.startswith("an1"):
            raise ValueError("relationship actor must be an Anet Node ID")
        if self.circle not in RELATION_CIRCLES:
            raise ValueError("invalid relationship circle")
        if self.state not in {"active", "revoked"}:
            raise ValueError("invalid relationship state")
        if not 0 <= self.subject_confidence <= 100:
            raise ValueError("invalid subject confidence")
        if not 0 <= self.relationship_confidence <= 100:
            raise ValueError("invalid relationship confidence")
        if self.updated_ms <= 0:
            raise ValueError("invalid relationship update time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "actor_id": self.actor_id,
            "actor_label": self.actor_label,
            "circle": self.circle,
            "state": self.state,
            "relationship_labels": list(self.relationship_labels),
            "subject_confidence": self.subject_confidence,
            "relationship_confidence": self.relationship_confidence,
            "evidence_refs": list(self.evidence_refs),
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipRecord":
        return cls(
            subject_ref=str(value["subject_ref"]),
            actor_id=str(value["actor_id"]),
            actor_label=str(value.get("actor_label", ""))[:128],
            circle=str(value["circle"]),
            state=str(value.get("state", "active")),
            relationship_labels=tuple(
                sorted({str(item) for item in value.get("relationship_labels", [])})
            ),
            subject_confidence=int(value["subject_confidence"]),
            relationship_confidence=int(value["relationship_confidence"]),
            evidence_refs=tuple(
                sorted({str(item) for item in value.get("evidence_refs", [])})
            ),
            updated_ms=int(value["updated_ms"]),
        )


class RelationshipBook:
    """Local subject hypotheses and relationship circles for verified actors."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = str(own_actor_id)
        self._records: dict[str, RelationshipRecord] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if int(value.get("version", 0)) != RELATION_BOOK_VERSION:
            raise ValueError("unsupported relationship book version")
        records: dict[str, RelationshipRecord] = {}
        for item in value.get("relationships", []):
            record = RelationshipRecord.from_dict(dict(item))
            if record.actor_id == self.own_actor_id:
                raise ValueError("relationship book contains the local actor")
            if record.actor_id in records:
                raise ValueError("relationship book contains a duplicate actor")
            records[record.actor_id] = record
        self._records = records

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": RELATION_BOOK_VERSION,
                "relationships": [
                    self._records[key].to_dict() for key in sorted(self._records)
                ],
            },
            private=True,
        )

    def confirm_friend(
        self,
        card: PeerCard,
        *,
        evidence_ref: str,
        now: int | None = None,
    ) -> RelationshipRecord:
        card.verify()
        if card.node_id == self.own_actor_id:
            raise ValueError("cannot create a relationship with the local actor")
        evidence = str(evidence_ref).strip()
        if not evidence or len(evidence) > 256:
            raise ValueError("invalid relationship evidence reference")
        current = int(time.time() * 1000) if now is None else int(now)
        existing = self._records.get(card.node_id)
        existing_circle = existing.circle if existing is not None else "public"
        circle = (
            existing_circle
            if RELATION_CIRCLES.index(existing_circle)
            >= RELATION_CIRCLES.index("friend")
            else "friend"
        )
        record = RelationshipRecord(
            subject_ref=(
                existing.subject_ref
                if existing is not None
                else f"subj_{secrets.token_hex(8)}"
            ),
            actor_id=card.node_id,
            actor_label=card.label,
            circle=circle,
            state="active",
            relationship_labels=tuple(
                sorted(
                    {
                        *(
                            existing.relationship_labels
                            if existing is not None
                            else ()
                        ),
                        "relationship:friend",
                    }
                )
            ),
            # A verified Node is an Actor fact. It is not proof of the concrete
            # human, AI, team, or hybrid Subject controlling that Actor.
            subject_confidence=(
                existing.subject_confidence if existing is not None else 50
            ),
            relationship_confidence=100,
            evidence_refs=tuple(
                sorted(
                    {
                        *(existing.evidence_refs if existing is not None else ()),
                        evidence,
                    }
                )
            ),
            updated_ms=current,
        )
        self._records[card.node_id] = record
        self.save()
        return record

    def revoke_actor(
        self,
        actor_id: str,
        *,
        evidence_ref: str,
        now: int | None = None,
    ) -> RelationshipRecord | None:
        actor = str(actor_id).strip()
        existing = self._records.get(actor)
        if existing is None:
            return None
        evidence = str(evidence_ref).strip()
        if not evidence or len(evidence) > 256:
            raise ValueError("invalid relationship evidence reference")
        current = int(time.time() * 1000) if now is None else int(now)
        record = RelationshipRecord(
            subject_ref=existing.subject_ref,
            actor_id=existing.actor_id,
            actor_label=existing.actor_label,
            circle="public",
            state="revoked",
            relationship_labels=tuple(
                sorted(
                    {
                        *existing.relationship_labels,
                        "status:revoked",
                    }
                )
            ),
            subject_confidence=existing.subject_confidence,
            relationship_confidence=0,
            evidence_refs=tuple(
                sorted({*existing.evidence_refs, evidence})
            ),
            updated_ms=current,
        )
        self._records[actor] = record
        self.save()
        return record

    def all(self) -> tuple[RelationshipRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def get(self, actor_id: str) -> RelationshipRecord | None:
        return self._records.get(str(actor_id))
