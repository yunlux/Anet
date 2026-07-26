from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .identity import PeerCard
from .relations import (
    InteractionEvidence,
    RELATION_CIRCLES,
    RelationshipBook,
)


_NON_SOCIAL_KINDS = frozenset(
    {
        "receipt",
        "network.probe",
        "social.relationship.disclosure",
    }
)
_ARTIFACT_KEYS = frozenset(
    {
        "artifact",
        "artifacts",
        "attachment",
        "attachments",
        "file",
        "files",
        "filename",
        "mediatype",
    }
)


@dataclass(frozen=True)
class InteractionProjection:
    recorded: bool
    interaction_id: str
    subject_ref: str
    circle: str


def classify_interaction(
    kind: str,
    body: Any,
    *,
    direction: str,
) -> tuple[tuple[str, ...], str, str] | None:
    """Map a packet to content-free relationship evidence."""

    normalized = str(kind).strip().lower()
    if (
        not normalized
        or normalized in _NON_SOCIAL_KINDS
        or normalized.startswith("network.")
        or normalized.startswith("companion.")
    ):
        return None

    if normalized.startswith("agent.task."):
        facets = {"task"}
        outcome = "received" if direction == "incoming" else "queued"
        if normalized == "agent.task.request":
            outcome = "submitted"
            if isinstance(body, dict) and (
                body.get("required_capabilities")
                or (
                    isinstance(body.get("context"), dict)
                    and body["context"].get("a2a")
                )
            ):
                facets.add("skill")
        elif normalized == "agent.task.status" and isinstance(body, dict):
            candidate = str(body.get("state", "")).strip().lower()
            if candidate in {
                "working",
                "input-required",
                "auth-required",
            }:
                outcome = candidate
        elif normalized == "agent.task.result" and isinstance(body, dict):
            candidate = str(body.get("state", "")).strip().lower()
            if candidate in {"completed", "failed", "canceled", "rejected"}:
                outcome = candidate
            if body.get("output") is not None:
                facets.add("artifact")
        elif normalized == "agent.task.cancel":
            outcome = "canceled"
        return tuple(sorted(facets)), "task", outcome

    facets = {"message"}
    context = "communication"
    if normalized.startswith(("discord.", "social.discord.")):
        context = "social.discord"
    if normalized.startswith("skill."):
        facets.add("skill")
        context = "skill"
    if normalized.startswith(("file.", "artifact.")):
        facets.add("artifact")
        context = "artifact"
    if isinstance(body, dict) and {
        str(key).strip().lower() for key in body
    }.intersection(_ARTIFACT_KEYS):
        facets.add("artifact")
    return (
        tuple(sorted(facets)),
        context,
        "received" if direction == "incoming" else "queued",
    )


class RelationshipProjector:
    """Project verified packet metadata into one observer-local relation book."""

    def __init__(self, book: RelationshipBook) -> None:
        self.book = book

    def project_packet(
        self,
        card: PeerCard,
        *,
        packet_id: str,
        kind: str,
        body: Any,
        direction: str,
        occurred_ms: int,
    ) -> InteractionProjection | None:
        classified = classify_interaction(kind, body, direction=direction)
        if classified is None:
            return None
        facets, context, outcome = classified
        evidence_ref = f"packet:{str(packet_id).strip().lower()}"

        subject = self.book.primary_subject(card.node_id)
        if subject is None:
            subject = self.book.observe_actor(
                card,
                evidence_ref=evidence_ref,
                subject_confidence=50,
                now=occurred_ms,
            )
        evidence = InteractionEvidence.create(
            actor_id=card.node_id,
            subject_ref=subject.subject_ref,
            direction=direction,
            facets=facets,
            context=context,
            outcome=outcome,
            evidence_ref=evidence_ref,
            occurred_ms=occurred_ms,
        )
        recorded = self.book.record_interaction(evidence)

        relationship = self.book.relationship(subject.subject_ref)
        if relationship is None:
            raise RuntimeError("observed Subject has no relationship estimate")
        if relationship.circle == RELATION_CIRCLES[0]:
            relationship = self.book.set_circle(
                subject.subject_ref,
                "known",
                confidence=25,
                evidence_ref=evidence_ref,
                labels=("interaction:verified",),
                now=occurred_ms,
            )
        return InteractionProjection(
            recorded=recorded,
            interaction_id=evidence.interaction_id,
            subject_ref=subject.subject_ref,
            circle=relationship.circle,
        )
