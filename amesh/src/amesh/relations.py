from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .identity import LocalIdentity, platform_actor_id
from .time import now_ms


def relations_database_path(home: Path) -> Path:
    return Path(home) / "amesh-relations.sqlite3"


@dataclass(frozen=True)
class ActorProof:
    proof_type: str
    scope: str
    issuer_actor_id: str
    evidence_ref: str
    observed_ms: int


@dataclass(frozen=True)
class ActorObservation:
    actor_id: str
    actor_kind: str
    actor_label: str
    proof: ActorProof


@dataclass(frozen=True)
class Subject:
    subject_ref: str
    actor_id: str


@dataclass(frozen=True)
class InteractionEvidence:
    actor_id: str
    subject_ref: str
    direction: str
    facets: tuple[str, ...]
    context: str
    outcome: str
    evidence_ref: str
    occurred_ms: int

    @classmethod
    def create(cls, **kwargs: Any) -> InteractionEvidence:
        facets = tuple(sorted({str(item) for item in kwargs.pop("facets", ())}))
        return cls(facets=facets, **kwargs)


@dataclass(frozen=True)
class RelationshipEstimate:
    subject_ref: str
    actor_id: str
    state: str
    circle: str
    confidence: int
    interaction_count: int
    labels: tuple[str, ...]
    evidence_ref: str
    updated_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "actor_id": self.actor_id,
            "state": self.state,
            "circle": self.circle,
            "confidence": self.confidence,
            "interaction_count": self.interaction_count,
            "labels": list(self.labels),
            "evidence_ref": self.evidence_ref,
            "updated_ms": self.updated_ms,
        }


class RelationshipBook:
    """Small observer-local relationship model owned by Amesh."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = str(own_actor_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS amesh_subjects (
                subject_ref TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL UNIQUE,
                actor_kind TEXT NOT NULL,
                actor_label TEXT NOT NULL,
                issuer_actor_id TEXT NOT NULL,
                proof_type TEXT NOT NULL,
                proof_scope TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                observed_ms INTEGER NOT NULL,
                state TEXT NOT NULL,
                circle TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                labels_json TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS amesh_interactions (
                evidence_ref TEXT PRIMARY KEY,
                subject_ref TEXT NOT NULL,
                occurred_ms INTEGER NOT NULL,
                context TEXT NOT NULL,
                outcome TEXT NOT NULL,
                facets_json TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def all(self) -> list[RelationshipEstimate]:
        rows = self._conn.execute(
            "SELECT * FROM amesh_subjects ORDER BY actor_id"
        ).fetchall()
        return [self._estimate(row) for row in rows]

    def primary_subject(self, actor_id: str) -> Subject | None:
        row = self._conn.execute(
            "SELECT subject_ref, actor_id FROM amesh_subjects WHERE actor_id = ?",
            (str(actor_id),),
        ).fetchone()
        return Subject(str(row["subject_ref"]), str(row["actor_id"])) if row else None

    def observe_typed_actor(
        self,
        observation: ActorObservation,
        *,
        subject_confidence: int = 50,
        now: int | None = None,
    ) -> Subject:
        if not 0 <= int(subject_confidence) <= 100:
            raise ValueError("relationship confidence must be between 0 and 100")
        actor_id = str(observation.actor_id).strip()
        if not actor_id or len(actor_id) > 256:
            raise ValueError("relationship actor ID is invalid")
        row = self._conn.execute(
            "SELECT subject_ref, actor_id FROM amesh_subjects WHERE actor_id = ?",
            (actor_id,),
        ).fetchone()
        current = int(now if now is not None else now_ms())
        if row:
            self._conn.execute(
                """UPDATE amesh_subjects SET updated_ms = ? WHERE subject_ref = ?""",
                (current, str(row["subject_ref"])),
            )
            return Subject(str(row["subject_ref"]), actor_id)
        digest = hashlib.sha256(
            f"amesh.subject.v1\0{self.own_actor_id}\0{actor_id}".encode("utf-8")
        ).hexdigest()[:32]
        subject_ref = "subj_" + digest
        self._conn.execute(
            """
            INSERT INTO amesh_subjects(
                subject_ref, actor_id, actor_kind, actor_label,
                issuer_actor_id, proof_type, proof_scope, evidence_ref,
                observed_ms, state, circle, confidence, interaction_count,
                labels_json, updated_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'public', ?, 0, '[]', ?)
            """,
            (
                subject_ref,
                actor_id,
                str(observation.actor_kind),
                str(observation.actor_label)[:256],
                str(observation.proof.issuer_actor_id),
                str(observation.proof.proof_type),
                str(observation.proof.scope),
                str(observation.proof.evidence_ref)[:256],
                int(observation.proof.observed_ms),
                int(subject_confidence),
                current,
            ),
        )
        return Subject(subject_ref, actor_id)

    def record_interaction(self, evidence: InteractionEvidence) -> bool:
        try:
            self._conn.execute(
                """
                INSERT INTO amesh_interactions(
                    evidence_ref, subject_ref, occurred_ms, context,
                    outcome, facets_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(evidence.evidence_ref),
                    str(evidence.subject_ref),
                    int(evidence.occurred_ms),
                    str(evidence.context),
                    str(evidence.outcome),
                    json.dumps(list(evidence.facets), sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.execute(
            """UPDATE amesh_subjects
               SET interaction_count = interaction_count + 1,
                   updated_ms = ?
               WHERE subject_ref = ?""",
            (int(evidence.occurred_ms), str(evidence.subject_ref)),
        )
        return True

    def relationship(self, subject_ref: str) -> RelationshipEstimate | None:
        row = self._conn.execute(
            "SELECT * FROM amesh_subjects WHERE subject_ref = ?",
            (str(subject_ref),),
        ).fetchone()
        return self._estimate(row) if row else None

    def set_circle(
        self,
        subject_ref: str,
        circle: str,
        *,
        confidence: int,
        evidence_ref: str,
        labels: tuple[str, ...] = (),
        now: int | None = None,
    ) -> RelationshipEstimate:
        circle = str(circle).strip().lower()
        if not re_match(r"^[a-z][a-z0-9_-]{0,31}$", circle):
            raise ValueError("invalid relationship circle")
        if not 0 <= int(confidence) <= 100:
            raise ValueError("relationship confidence must be between 0 and 100")
        row = self._conn.execute(
            "SELECT labels_json FROM amesh_subjects WHERE subject_ref = ?",
            (str(subject_ref),),
        ).fetchone()
        if row is None:
            raise ValueError("unknown relationship subject")
        current_labels = set(json.loads(str(row["labels_json"])))
        current_labels.update(str(item)[:64] for item in labels)
        current = int(now if now is not None else now_ms())
        self._conn.execute(
            """UPDATE amesh_subjects SET circle = ?, confidence = ?,
               evidence_ref = ?, labels_json = ?, updated_ms = ?
               WHERE subject_ref = ?""",
            (
                circle,
                int(confidence),
                str(evidence_ref)[:256],
                json.dumps(sorted(current_labels), sort_keys=True),
                current,
                str(subject_ref),
            ),
        )
        result = self.relationship(subject_ref)
        if result is None:  # pragma: no cover
            raise RuntimeError("relationship disappeared")
        return result

    @staticmethod
    def _estimate(row: sqlite3.Row) -> RelationshipEstimate:
        return RelationshipEstimate(
            subject_ref=str(row["subject_ref"]),
            actor_id=str(row["actor_id"]),
            state=str(row["state"]),
            circle=str(row["circle"]),
            confidence=int(row["confidence"]),
            interaction_count=int(row["interaction_count"]),
            labels=tuple(json.loads(str(row["labels_json"]))),
            evidence_ref=str(row["evidence_ref"]),
            updated_ms=int(row["updated_ms"]),
        )


def re_match(pattern: str, value: str) -> bool:
    import re

    return re.fullmatch(pattern, value) is not None


class RelationshipHub:
    """Amesh-owned relationship and evidence projection store."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser().resolve()
        self.identity = LocalIdentity(self.home)
        self.book = RelationshipBook(
            relations_database_path(self.home),
            own_actor_id=self.identity.identity_id,
        )

    def list_records(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.book.all()]

    def observe_actor(
        self,
        actor_id: str,
        *,
        kind: str,
        label: str = "",
        evidence: str = "",
        confidence: int = 50,
    ) -> dict[str, Any]:
        subject = self.book.observe_typed_actor(
            ActorObservation(
                actor_id=str(actor_id),
                actor_kind=str(kind),
                actor_label=label or f"{kind} · {str(actor_id)[-6:]}",
                proof=ActorProof(
                    proof_type="operator.local.v1",
                    scope="operator-attested",
                    issuer_actor_id=self.identity.identity_id,
                    evidence_ref=str(evidence),
                    observed_ms=now_ms(),
                ),
            ),
            subject_confidence=int(confidence),
        )
        record = self.book.relationship(subject.subject_ref)
        if record is None:
            raise RuntimeError("observed actor was not persisted")
        return record.to_dict()

    def set_circle(
        self,
        subject_ref: str,
        circle: str,
        *,
        confidence: int,
        evidence_ref: str,
        labels: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self.book.set_circle(
            subject_ref,
            circle,
            confidence=int(confidence),
            evidence_ref=str(evidence_ref),
            labels=labels,
        ).to_dict()

    def relationship(self, subject_ref: str) -> dict[str, Any] | None:
        estimate = self.book.relationship(subject_ref)
        return estimate.to_dict() if estimate is not None else None


__all__ = [
    "ActorObservation",
    "ActorProof",
    "InteractionEvidence",
    "RelationshipBook",
    "RelationshipEstimate",
    "RelationshipHub",
    "platform_actor_id",
]
