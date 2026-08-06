from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from anet.config import NodeConfig
from anet.identity import Identity
from anet.relations import (
    ActorObservation,
    ActorProof,
    RelationshipBook,
)


class RelationshipHub:
    """Observer-local relationship book access for the Amesh management plane.

    Reuses the Anet relations model as-is: actors, Subject hypotheses,
    circles, contextual trust, and evidence stay observer-local. Amesh adds no
    second relationship store.
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser().resolve()
        self.config = NodeConfig.load(self.home)
        self.identity = Identity.load(self.config.identity_path)
        self.book = RelationshipBook(
            self.config.relationships_path,
            own_actor_id=self.identity.node_id,
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
        observation = ActorObservation(
            actor_id=str(actor_id).strip().lower(),
            actor_kind=kind,
            actor_label=(
                str(label).strip() if label else f"{kind} · {str(actor_id)[-6:]}"
            ),
            proof=ActorProof(
                proof_type="operator.local.v1",
                scope="operator-attested",
                issuer_actor_id=self.identity.node_id,
                evidence_ref=str(evidence).strip(),
                observed_ms=int(time.time() * 1000),
            ),
        )
        subject = self.book.observe_typed_actor(
            observation,
            subject_confidence=int(confidence),
        )
        record = self._record_for_subject(subject.subject_ref)
        if record is None:
            raise RuntimeError("observed Actor was not persisted")
        return record

    def set_circle(
        self,
        subject_ref: str,
        circle: str,
        *,
        confidence: int,
        evidence_ref: str,
        labels: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        estimate = self.book.set_circle(
            subject_ref,
            circle,
            confidence=int(confidence),
            evidence_ref=str(evidence_ref).strip(),
            labels=labels,
        )
        return estimate.to_dict()

    def relationship(self, subject_ref: str) -> dict[str, Any] | None:
        estimate = self.book.relationship(subject_ref)
        return estimate.to_dict() if estimate is not None else None

    def _record_for_subject(self, subject_ref: str) -> dict[str, Any] | None:
        for record in self.book.all():
            if record.subject_ref == subject_ref:
                return record.to_dict()
        return None
