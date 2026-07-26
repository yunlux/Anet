from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .encoding import atomic_json
from .identity import PeerCard


RELATION_BOOK_VERSION = 3
RELATION_CIRCLES = (
    "public",
    "known",
    "collab",
    "friend",
    "close",
    "family",
)
ACTOR_STATES = frozenset({"active", "revoked"})
SUBJECT_STATES = frozenset({"active", "superseded"})
RELATIONSHIP_STATES = frozenset({"active", "dormant", "ended"})
MAX_LABEL_LENGTH = 128
MAX_EVIDENCE_LENGTH = 256
MAX_CONTEXT_LENGTH = 64
INTERACTION_DIRECTIONS = frozenset({"incoming", "outgoing"})
INTERACTION_FACETS = frozenset({"message", "task", "skill", "artifact"})
INTERACTION_OUTCOMES = frozenset(
    {
        "queued",
        "received",
        "submitted",
        "working",
        "input-required",
        "auth-required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    }
)


def _now_ms(now: int | None) -> int:
    value = int(time.time() * 1000) if now is None else int(now)
    if value <= 0:
        raise ValueError("invalid relationship event time")
    return value


def _bounded_text(value: str, *, label: str, maximum: int) -> str:
    result = str(value).strip()
    if not result or len(result) > maximum:
        raise ValueError(f"invalid {label}")
    return result


def _confidence(value: int, *, label: str) -> int:
    result = int(value)
    if not 0 <= result <= 100:
        raise ValueError(f"invalid {label}")
    return result


def _unique_text(values: Iterable[str], *, label: str, maximum: int) -> tuple[str, ...]:
    return tuple(
        sorted({_bounded_text(value, label=label, maximum=maximum) for value in values})
    )


@dataclass(frozen=True)
class ActorRecord:
    actor_id: str
    actor_label: str
    state: str
    evidence_refs: tuple[str, ...]
    first_seen_ms: int
    updated_ms: int

    def __post_init__(self) -> None:
        if not self.actor_id.startswith("an1"):
            raise ValueError("relationship actor must be an Anet Node ID")
        if len(self.actor_label) > MAX_LABEL_LENGTH:
            raise ValueError("invalid Actor label")
        if self.state not in ACTOR_STATES:
            raise ValueError("invalid Actor state")
        if self.first_seen_ms <= 0 or self.updated_ms < self.first_seen_ms:
            raise ValueError("invalid Actor observation time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "actor_label": self.actor_label,
            "state": self.state,
            "evidence_refs": list(self.evidence_refs),
            "first_seen_ms": self.first_seen_ms,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ActorRecord":
        return cls(
            actor_id=str(value["actor_id"]),
            actor_label=str(value.get("actor_label", "")),
            state=str(value.get("state", "active")),
            evidence_refs=_unique_text(
                value.get("evidence_refs", ()),
                label="Actor evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            first_seen_ms=int(value["first_seen_ms"]),
            updated_ms=int(value["updated_ms"]),
        )


@dataclass(frozen=True)
class SubjectActorLink:
    actor_id: str
    confidence: int
    evidence_refs: tuple[str, ...]
    updated_ms: int

    def __post_init__(self) -> None:
        if not self.actor_id.startswith("an1"):
            raise ValueError("Subject link Actor must be an Anet Node ID")
        _confidence(self.confidence, label="Subject link confidence")
        if self.updated_ms <= 0:
            raise ValueError("invalid Subject link update time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubjectActorLink":
        return cls(
            actor_id=str(value["actor_id"]),
            confidence=int(value["confidence"]),
            evidence_refs=_unique_text(
                value.get("evidence_refs", ()),
                label="Subject link evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=int(value["updated_ms"]),
        )


@dataclass(frozen=True)
class SubjectHypothesis:
    subject_ref: str
    state: str
    labels: tuple[str, ...]
    actor_links: tuple[SubjectActorLink, ...]
    evidence_refs: tuple[str, ...]
    updated_ms: int

    def __post_init__(self) -> None:
        if not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid local Subject reference")
        if self.state not in SUBJECT_STATES:
            raise ValueError("invalid Subject hypothesis state")
        if not self.actor_links:
            raise ValueError("Subject hypothesis requires at least one Actor link")
        actor_ids = [link.actor_id for link in self.actor_links]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("Subject hypothesis contains a duplicate Actor link")
        if self.updated_ms <= 0:
            raise ValueError("invalid Subject hypothesis update time")

    @property
    def confidence(self) -> int:
        return max(link.confidence for link in self.actor_links)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "state": self.state,
            "labels": list(self.labels),
            "actor_links": [
                link.to_dict()
                for link in sorted(self.actor_links, key=lambda item: item.actor_id)
            ],
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubjectHypothesis":
        return cls(
            subject_ref=str(value["subject_ref"]),
            state=str(value.get("state", "active")),
            labels=_unique_text(
                value.get("labels", ()),
                label="Subject label",
                maximum=MAX_LABEL_LENGTH,
            ),
            actor_links=tuple(
                SubjectActorLink.from_dict(dict(item))
                for item in value.get("actor_links", ())
            ),
            evidence_refs=_unique_text(
                value.get("evidence_refs", ()),
                label="Subject evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=int(value["updated_ms"]),
        )


@dataclass(frozen=True)
class ContextTrust:
    context: str
    estimate: int
    confidence: int
    evidence_refs: tuple[str, ...]
    updated_ms: int

    def __post_init__(self) -> None:
        _bounded_text(
            self.context,
            label="trust context",
            maximum=MAX_CONTEXT_LENGTH,
        )
        _confidence(self.estimate, label="context trust estimate")
        _confidence(self.confidence, label="context trust confidence")
        if self.updated_ms <= 0:
            raise ValueError("invalid context trust update time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "estimate": self.estimate,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContextTrust":
        return cls(
            context=str(value["context"]),
            estimate=int(value["estimate"]),
            confidence=int(value["confidence"]),
            evidence_refs=_unique_text(
                value.get("evidence_refs", ()),
                label="context trust evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=int(value["updated_ms"]),
        )


@dataclass(frozen=True)
class RelationshipEstimate:
    subject_ref: str
    circle: str
    state: str
    relationship_labels: tuple[str, ...]
    relationship_confidence: int
    context_trust: tuple[ContextTrust, ...]
    evidence_refs: tuple[str, ...]
    updated_ms: int

    def __post_init__(self) -> None:
        if not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid local Subject reference")
        if self.circle not in RELATION_CIRCLES:
            raise ValueError("invalid relationship circle")
        if self.state not in RELATIONSHIP_STATES:
            raise ValueError("invalid relationship state")
        _confidence(
            self.relationship_confidence,
            label="relationship confidence",
        )
        contexts = [item.context for item in self.context_trust]
        if len(contexts) != len(set(contexts)):
            raise ValueError("relationship contains a duplicate trust context")
        if self.updated_ms <= 0:
            raise ValueError("invalid relationship update time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "circle": self.circle,
            "state": self.state,
            "relationship_labels": list(self.relationship_labels),
            "relationship_confidence": self.relationship_confidence,
            "context_trust": [
                item.to_dict()
                for item in sorted(self.context_trust, key=lambda item: item.context)
            ],
            "evidence_refs": list(self.evidence_refs),
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipEstimate":
        return cls(
            subject_ref=str(value["subject_ref"]),
            circle=str(value["circle"]),
            state=str(value.get("state", "active")),
            relationship_labels=_unique_text(
                value.get("relationship_labels", ()),
                label="relationship label",
                maximum=MAX_LABEL_LENGTH,
            ),
            relationship_confidence=int(value["relationship_confidence"]),
            context_trust=tuple(
                ContextTrust.from_dict(dict(item))
                for item in value.get("context_trust", ())
            ),
            evidence_refs=_unique_text(
                value.get("evidence_refs", ()),
                label="relationship evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=int(value["updated_ms"]),
        )


@dataclass(frozen=True)
class RelationshipEvent:
    event_id: str
    event_type: str
    actor_id: str
    subject_ref: str
    evidence_ref: str
    observed_ms: int

    def __post_init__(self) -> None:
        if not self.event_id.startswith("revt_"):
            raise ValueError("invalid relationship event ID")
        _bounded_text(
            self.event_type,
            label="relationship event type",
            maximum=MAX_CONTEXT_LENGTH,
        )
        if self.actor_id and not self.actor_id.startswith("an1"):
            raise ValueError("invalid relationship event Actor")
        if self.subject_ref and not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid relationship event Subject")
        _bounded_text(
            self.evidence_ref,
            label="relationship event evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        if self.observed_ms <= 0:
            raise ValueError("invalid relationship event time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "subject_ref": self.subject_ref,
            "evidence_ref": self.evidence_ref,
            "observed_ms": self.observed_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationshipEvent":
        return cls(
            event_id=str(value["event_id"]),
            event_type=str(value["event_type"]),
            actor_id=str(value.get("actor_id", "")),
            subject_ref=str(value.get("subject_ref", "")),
            evidence_ref=str(value["evidence_ref"]),
            observed_ms=int(value["observed_ms"]),
        )


@dataclass(frozen=True)
class InteractionEvidence:
    """Content-free evidence that a verified Actor interaction occurred."""

    interaction_id: str
    actor_id: str
    subject_ref: str
    direction: str
    facets: tuple[str, ...]
    context: str
    outcome: str
    evidence_ref: str
    occurred_ms: int

    def __post_init__(self) -> None:
        if not self.interaction_id.startswith("iev_"):
            raise ValueError("invalid interaction evidence ID")
        if not self.actor_id.startswith("an1"):
            raise ValueError("invalid interaction Actor")
        if not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid interaction Subject")
        if self.direction not in INTERACTION_DIRECTIONS:
            raise ValueError("invalid interaction direction")
        if not self.facets or any(
            facet not in INTERACTION_FACETS for facet in self.facets
        ):
            raise ValueError("invalid interaction facets")
        if len(self.facets) != len(set(self.facets)):
            raise ValueError("duplicate interaction facet")
        _bounded_text(
            self.context,
            label="interaction context",
            maximum=MAX_CONTEXT_LENGTH,
        )
        if self.outcome not in INTERACTION_OUTCOMES:
            raise ValueError("invalid interaction outcome")
        _bounded_text(
            self.evidence_ref,
            label="interaction evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        if self.occurred_ms <= 0:
            raise ValueError("invalid interaction time")

    @classmethod
    def create(
        cls,
        *,
        actor_id: str,
        subject_ref: str,
        direction: str,
        facets: Iterable[str],
        context: str,
        outcome: str,
        evidence_ref: str,
        occurred_ms: int,
    ) -> "InteractionEvidence":
        normalized_facets = tuple(sorted({str(item) for item in facets}))
        source = "\0".join(
            (
                str(actor_id),
                str(direction),
                str(evidence_ref),
            )
        ).encode("utf-8")
        interaction_id = "iev_" + hashlib.blake2s(
            source,
            digest_size=16,
            person=b"anet-iev",
        ).hexdigest()
        return cls(
            interaction_id=interaction_id,
            actor_id=str(actor_id),
            subject_ref=str(subject_ref),
            direction=str(direction),
            facets=normalized_facets,
            context=str(context),
            outcome=str(outcome),
            evidence_ref=str(evidence_ref),
            occurred_ms=int(occurred_ms),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "actor_id": self.actor_id,
            "subject_ref": self.subject_ref,
            "direction": self.direction,
            "facets": list(self.facets),
            "context": self.context,
            "outcome": self.outcome,
            "evidence_ref": self.evidence_ref,
            "occurred_ms": self.occurred_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InteractionEvidence":
        return cls(
            interaction_id=str(value["interaction_id"]),
            actor_id=str(value["actor_id"]),
            subject_ref=str(value["subject_ref"]),
            direction=str(value["direction"]),
            facets=tuple(str(item) for item in value.get("facets", ())),
            context=str(value["context"]),
            outcome=str(value["outcome"]),
            evidence_ref=str(value["evidence_ref"]),
            occurred_ms=int(value["occurred_ms"]),
        )


@dataclass(frozen=True)
class RelationshipRecord:
    """Compatibility projection for one Actor through its primary Subject."""

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


class RelationshipBook:
    """Observer-local Actor facts, Subject hypotheses, and relationship estimates."""

    def __init__(self, path: Path, *, own_actor_id: str) -> None:
        self.path = Path(path)
        self.own_actor_id = str(own_actor_id)
        self._actors: dict[str, ActorRecord] = {}
        self._subjects: dict[str, SubjectHypothesis] = {}
        self._relationships: dict[str, RelationshipEstimate] = {}
        self._events: list[RelationshipEvent] = []
        self._interactions: dict[str, InteractionEvidence] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._actors = {}
            self._subjects = {}
            self._relationships = {}
            self._events = []
            self._interactions = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        version = int(value.get("version", 0))
        if version == 1:
            self._load_v1(value)
            return
        if version not in {2, RELATION_BOOK_VERSION}:
            raise ValueError("unsupported relationship book version")
        actors = {
            item.actor_id: item
            for item in (
                ActorRecord.from_dict(dict(value)) for value in value.get("actors", ())
            )
        }
        subjects = {
            item.subject_ref: item
            for item in (
                SubjectHypothesis.from_dict(dict(value))
                for value in value.get("subjects", ())
            )
        }
        relationships = {
            item.subject_ref: item
            for item in (
                RelationshipEstimate.from_dict(dict(value))
                for value in value.get("relationships", ())
            )
        }
        events = [
            RelationshipEvent.from_dict(dict(item)) for item in value.get("events", ())
        ]
        interactions = {
            item.interaction_id: item
            for item in (
                InteractionEvidence.from_dict(dict(item))
                for item in value.get("interactions", ())
            )
        }
        self._validate_model(
            actors,
            subjects,
            relationships,
            events,
            interactions,
        )
        self._actors = actors
        self._subjects = subjects
        self._relationships = relationships
        self._events = events
        self._interactions = interactions

    def _load_v1(self, value: dict[str, Any]) -> None:
        actors: dict[str, ActorRecord] = {}
        subjects: dict[str, SubjectHypothesis] = {}
        relationships: dict[str, RelationshipEstimate] = {}
        for raw in value.get("relationships", ()):
            actor_id = str(raw["actor_id"])
            updated_ms = int(raw["updated_ms"])
            evidence_refs = _unique_text(
                raw.get("evidence_refs", ()),
                label="relationship evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            )
            subject_ref = str(raw["subject_ref"])
            actors[actor_id] = ActorRecord(
                actor_id=actor_id,
                actor_label=str(raw.get("actor_label", ""))[:MAX_LABEL_LENGTH],
                state=(
                    "revoked"
                    if str(raw.get("state", "active")) == "revoked"
                    else "active"
                ),
                evidence_refs=evidence_refs,
                first_seen_ms=updated_ms,
                updated_ms=updated_ms,
            )
            subjects[subject_ref] = SubjectHypothesis(
                subject_ref=subject_ref,
                state="active",
                labels=(),
                actor_links=(
                    SubjectActorLink(
                        actor_id=actor_id,
                        confidence=int(raw["subject_confidence"]),
                        evidence_refs=evidence_refs,
                        updated_ms=updated_ms,
                    ),
                ),
                evidence_refs=evidence_refs,
                updated_ms=updated_ms,
            )
            relationships[subject_ref] = RelationshipEstimate(
                subject_ref=subject_ref,
                circle=str(raw["circle"]),
                state=(
                    "ended"
                    if str(raw.get("state", "active")) == "revoked"
                    else "active"
                ),
                relationship_labels=_unique_text(
                    raw.get("relationship_labels", ()),
                    label="relationship label",
                    maximum=MAX_LABEL_LENGTH,
                ),
                relationship_confidence=int(raw["relationship_confidence"]),
                context_trust=(),
                evidence_refs=evidence_refs,
                updated_ms=updated_ms,
            )
        self._validate_model(actors, subjects, relationships, [])
        self._actors = actors
        self._subjects = subjects
        self._relationships = relationships
        self._events = []
        self._interactions = {}

    def _validate_model(
        self,
        actors: dict[str, ActorRecord],
        subjects: dict[str, SubjectHypothesis],
        relationships: dict[str, RelationshipEstimate],
        events: list[RelationshipEvent],
        interactions: dict[str, InteractionEvidence] | None = None,
    ) -> None:
        if self.own_actor_id in actors:
            raise ValueError("relationship book contains the local Actor")
        linked_actors: set[str] = set()
        for subject in subjects.values():
            for link in subject.actor_links:
                if link.actor_id not in actors:
                    raise ValueError("Subject hypothesis references an unknown Actor")
                linked_actors.add(link.actor_id)
        if linked_actors != set(actors):
            raise ValueError("every observed Actor must belong to a Subject hypothesis")
        if set(relationships) - set(subjects):
            raise ValueError("relationship references an unknown Subject hypothesis")
        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("relationship book contains a duplicate event")
        for interaction in (interactions or {}).values():
            if interaction.actor_id not in actors:
                raise ValueError("interaction references an unknown Actor")
            subject = subjects.get(interaction.subject_ref)
            if subject is None:
                raise ValueError("interaction references an unknown Subject")
            if interaction.actor_id not in {
                link.actor_id for link in subject.actor_links
            }:
                raise ValueError("interaction Actor is not linked to its Subject")

    def save(self) -> None:
        atomic_json(
            self.path,
            {
                "version": RELATION_BOOK_VERSION,
                "observer_actor_id": self.own_actor_id,
                "actors": [self._actors[key].to_dict() for key in sorted(self._actors)],
                "subjects": [
                    self._subjects[key].to_dict() for key in sorted(self._subjects)
                ],
                "relationships": [
                    self._relationships[key].to_dict()
                    for key in sorted(self._relationships)
                ],
                "events": [event.to_dict() for event in self._events],
                "interactions": [
                    self._interactions[key].to_dict()
                    for key in sorted(self._interactions)
                ],
            },
            private=True,
        )

    def _append_event(
        self,
        event_type: str,
        *,
        evidence_ref: str,
        actor_id: str = "",
        subject_ref: str = "",
        now: int,
    ) -> RelationshipEvent:
        event = RelationshipEvent(
            event_id=f"revt_{secrets.token_hex(12)}",
            event_type=event_type,
            actor_id=actor_id,
            subject_ref=subject_ref,
            evidence_ref=evidence_ref,
            observed_ms=now,
        )
        self._events.append(event)
        return event

    def observe_actor(
        self,
        card: PeerCard,
        *,
        evidence_ref: str,
        subject_confidence: int = 50,
        now: int | None = None,
    ) -> SubjectHypothesis:
        card.verify()
        if card.node_id == self.own_actor_id:
            raise ValueError("cannot observe the local Actor as a peer")
        evidence = _bounded_text(
            evidence_ref,
            label="Actor evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        confidence = _confidence(
            subject_confidence,
            label="initial Subject confidence",
        )
        current = _now_ms(now)
        actor = self._actors.get(card.node_id)
        if actor is None:
            actor = ActorRecord(
                actor_id=card.node_id,
                actor_label=card.label[:MAX_LABEL_LENGTH],
                state="active",
                evidence_refs=(evidence,),
                first_seen_ms=current,
                updated_ms=current,
            )
            self._actors[card.node_id] = actor
            subject_ref = f"subj_{secrets.token_hex(8)}"
            subject = SubjectHypothesis(
                subject_ref=subject_ref,
                state="active",
                labels=(),
                actor_links=(
                    SubjectActorLink(
                        actor_id=card.node_id,
                        confidence=confidence,
                        evidence_refs=(evidence,),
                        updated_ms=current,
                    ),
                ),
                evidence_refs=(evidence,),
                updated_ms=current,
            )
            self._subjects[subject_ref] = subject
            self._relationships[subject_ref] = RelationshipEstimate(
                subject_ref=subject_ref,
                circle="public",
                state="active",
                relationship_labels=(),
                relationship_confidence=0,
                context_trust=(),
                evidence_refs=(evidence,),
                updated_ms=current,
            )
            self._append_event(
                "actor.observed",
                actor_id=card.node_id,
                subject_ref=subject_ref,
                evidence_ref=evidence,
                now=current,
            )
            self.save()
            return subject

        self._actors[card.node_id] = ActorRecord(
            actor_id=actor.actor_id,
            actor_label=card.label[:MAX_LABEL_LENGTH],
            state=actor.state,
            evidence_refs=_unique_text(
                (*actor.evidence_refs, evidence),
                label="Actor evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            first_seen_ms=actor.first_seen_ms,
            updated_ms=current,
        )
        subject = self.primary_subject(card.node_id)
        if subject is None:
            raise ValueError("observed Actor has no Subject hypothesis")
        self._append_event(
            "actor.observation-refreshed",
            actor_id=card.node_id,
            subject_ref=subject.subject_ref,
            evidence_ref=evidence,
            now=current,
        )
        self.save()
        return subject

    def has_interaction(self, interaction_id: str) -> bool:
        return str(interaction_id) in self._interactions

    def record_interaction(
        self,
        evidence: InteractionEvidence,
    ) -> bool:
        """Persist one idempotent, content-free interaction observation."""

        if evidence.interaction_id in self._interactions:
            return False
        if evidence.actor_id not in self._actors:
            raise KeyError(f"unknown Actor: {evidence.actor_id}")
        subject = self._subjects.get(evidence.subject_ref)
        if subject is None:
            raise KeyError(f"unknown Subject hypothesis: {evidence.subject_ref}")
        if evidence.actor_id not in {
            link.actor_id for link in subject.actor_links
        }:
            raise ValueError("interaction Actor is not linked to its Subject")
        self._interactions[evidence.interaction_id] = evidence
        self._append_event(
            "interaction.observed",
            actor_id=evidence.actor_id,
            subject_ref=evidence.subject_ref,
            evidence_ref=evidence.evidence_ref,
            now=evidence.occurred_ms,
        )
        self.save()
        return True

    def link_actor(
        self,
        actor_id: str,
        subject_ref: str,
        *,
        confidence: int,
        evidence_ref: str,
        now: int | None = None,
    ) -> SubjectHypothesis:
        actor = _bounded_text(
            actor_id,
            label="Actor ID",
            maximum=256,
        )
        subject_key = _bounded_text(
            subject_ref,
            label="Subject reference",
            maximum=128,
        )
        if actor not in self._actors:
            raise KeyError(f"unknown Actor: {actor}")
        subject = self._subjects.get(subject_key)
        if subject is None:
            raise KeyError(f"unknown Subject hypothesis: {subject_key}")
        if subject.state != "active":
            raise ValueError("cannot link an Actor to a superseded Subject")
        link_confidence = _confidence(
            confidence,
            label="Subject link confidence",
        )
        evidence = _bounded_text(
            evidence_ref,
            label="Subject link evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        current = _now_ms(now)
        links = {link.actor_id: link for link in subject.actor_links}
        previous = links.get(actor)
        links[actor] = SubjectActorLink(
            actor_id=actor,
            confidence=link_confidence,
            evidence_refs=_unique_text(
                (
                    *(previous.evidence_refs if previous is not None else ()),
                    evidence,
                ),
                label="Subject link evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=current,
        )
        updated = SubjectHypothesis(
            subject_ref=subject.subject_ref,
            state=subject.state,
            labels=subject.labels,
            actor_links=tuple(links.values()),
            evidence_refs=_unique_text(
                (*subject.evidence_refs, evidence),
                label="Subject evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=current,
        )
        self._subjects[subject_key] = updated
        self._append_event(
            "subject.actor-linked",
            actor_id=actor,
            subject_ref=subject_key,
            evidence_ref=evidence,
            now=current,
        )
        self.save()
        return updated

    def set_circle(
        self,
        subject_ref: str,
        circle: str,
        *,
        confidence: int,
        evidence_ref: str,
        labels: Iterable[str] = (),
        now: int | None = None,
    ) -> RelationshipEstimate:
        subject_key = _bounded_text(
            subject_ref,
            label="Subject reference",
            maximum=128,
        )
        if subject_key not in self._subjects:
            raise KeyError(f"unknown Subject hypothesis: {subject_key}")
        if circle not in RELATION_CIRCLES:
            raise ValueError("invalid relationship circle")
        relationship_confidence = _confidence(
            confidence,
            label="relationship confidence",
        )
        evidence = _bounded_text(
            evidence_ref,
            label="relationship evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        current = _now_ms(now)
        existing = self._relationships[subject_key]
        updated = RelationshipEstimate(
            subject_ref=subject_key,
            circle=circle,
            state="active",
            relationship_labels=_unique_text(
                (*existing.relationship_labels, *labels),
                label="relationship label",
                maximum=MAX_LABEL_LENGTH,
            ),
            relationship_confidence=relationship_confidence,
            context_trust=existing.context_trust,
            evidence_refs=_unique_text(
                (*existing.evidence_refs, evidence),
                label="relationship evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=current,
        )
        self._relationships[subject_key] = updated
        self._append_event(
            "relationship.circle-set",
            subject_ref=subject_key,
            evidence_ref=evidence,
            now=current,
        )
        self.save()
        return updated

    def set_context_trust(
        self,
        subject_ref: str,
        context: str,
        *,
        estimate: int,
        confidence: int,
        evidence_ref: str,
        now: int | None = None,
    ) -> RelationshipEstimate:
        subject_key = _bounded_text(
            subject_ref,
            label="Subject reference",
            maximum=128,
        )
        relationship = self._relationships.get(subject_key)
        if relationship is None:
            raise KeyError(f"unknown Subject hypothesis: {subject_key}")
        context_name = _bounded_text(
            context,
            label="trust context",
            maximum=MAX_CONTEXT_LENGTH,
        )
        evidence = _bounded_text(
            evidence_ref,
            label="context trust evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        current = _now_ms(now)
        contexts = {item.context: item for item in relationship.context_trust}
        previous = contexts.get(context_name)
        contexts[context_name] = ContextTrust(
            context=context_name,
            estimate=_confidence(
                estimate,
                label="context trust estimate",
            ),
            confidence=_confidence(
                confidence,
                label="context trust confidence",
            ),
            evidence_refs=_unique_text(
                (
                    *(previous.evidence_refs if previous is not None else ()),
                    evidence,
                ),
                label="context trust evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=current,
        )
        updated = RelationshipEstimate(
            subject_ref=relationship.subject_ref,
            circle=relationship.circle,
            state=relationship.state,
            relationship_labels=relationship.relationship_labels,
            relationship_confidence=relationship.relationship_confidence,
            context_trust=tuple(contexts.values()),
            evidence_refs=_unique_text(
                (*relationship.evidence_refs, evidence),
                label="relationship evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=current,
        )
        self._relationships[subject_key] = updated
        self._append_event(
            "relationship.context-trust-set",
            subject_ref=subject_key,
            evidence_ref=evidence,
            now=current,
        )
        self.save()
        return updated

    def confirm_friend(
        self,
        card: PeerCard,
        *,
        evidence_ref: str,
        now: int | None = None,
    ) -> RelationshipRecord:
        current = _now_ms(now)
        subject = self.observe_actor(
            card,
            evidence_ref=evidence_ref,
            now=current,
        )
        existing = self._relationships[subject.subject_ref]
        circle = (
            existing.circle
            if RELATION_CIRCLES.index(existing.circle)
            >= RELATION_CIRCLES.index("friend")
            else "friend"
        )
        self.set_circle(
            subject.subject_ref,
            circle,
            confidence=100,
            labels=("relationship:friend",),
            evidence_ref=evidence_ref,
            now=current,
        )
        result = self.get(card.node_id)
        if result is None:
            raise RuntimeError("friend confirmation did not create a relationship")
        return result

    def revoke_actor(
        self,
        actor_id: str,
        *,
        evidence_ref: str,
        now: int | None = None,
    ) -> RelationshipRecord | None:
        actor_key = str(actor_id).strip()
        actor = self._actors.get(actor_key)
        if actor is None:
            return None
        evidence = _bounded_text(
            evidence_ref,
            label="Actor revocation evidence reference",
            maximum=MAX_EVIDENCE_LENGTH,
        )
        current = _now_ms(now)
        self._actors[actor_key] = ActorRecord(
            actor_id=actor.actor_id,
            actor_label=actor.actor_label,
            state="revoked",
            evidence_refs=_unique_text(
                (*actor.evidence_refs, evidence),
                label="Actor evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            first_seen_ms=actor.first_seen_ms,
            updated_ms=current,
        )
        subject = self.primary_subject(actor_key)
        self._append_event(
            "actor.revoked",
            actor_id=actor_key,
            subject_ref=subject.subject_ref if subject is not None else "",
            evidence_ref=evidence,
            now=current,
        )
        self.save()
        return self.get(actor_key)

    def primary_subject(self, actor_id: str) -> SubjectHypothesis | None:
        actor_key = str(actor_id)
        candidates = [
            (link.confidence, subject.updated_ms, subject.subject_ref, subject)
            for subject in self._subjects.values()
            if subject.state == "active"
            for link in subject.actor_links
            if link.actor_id == actor_key
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]

    def subject(self, subject_ref: str) -> SubjectHypothesis | None:
        return self._subjects.get(str(subject_ref))

    def relationship(self, subject_ref: str) -> RelationshipEstimate | None:
        return self._relationships.get(str(subject_ref))

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": RELATION_BOOK_VERSION,
            "observer_actor_id": self.own_actor_id,
            "actors": [self._actors[key].to_dict() for key in sorted(self._actors)],
            "subjects": [
                self._subjects[key].to_dict() for key in sorted(self._subjects)
            ],
            "relationships": [
                self._relationships[key].to_dict()
                for key in sorted(self._relationships)
            ],
            "events": [event.to_dict() for event in self._events],
            "interactions": [
                self._interactions[key].to_dict()
                for key in sorted(self._interactions)
            ],
            "interaction_stats": self.interaction_stats(),
        }

    def interaction_stats(self) -> list[dict[str, Any]]:
        """Return a derived summary; counts are evidence, never trust scores."""

        grouped: dict[
            tuple[str, str, str],
            dict[str, Any],
        ] = {}
        for evidence in self._interactions.values():
            for facet in evidence.facets:
                key = (evidence.subject_ref, evidence.context, facet)
                item = grouped.setdefault(
                    key,
                    {
                        "subject_ref": evidence.subject_ref,
                        "context": evidence.context,
                        "facet": facet,
                        "incoming": 0,
                        "outgoing": 0,
                        "outcomes": {},
                        "first_ms": evidence.occurred_ms,
                        "last_ms": evidence.occurred_ms,
                    },
                )
                item[evidence.direction] += 1
                outcomes = item["outcomes"]
                outcomes[evidence.outcome] = outcomes.get(evidence.outcome, 0) + 1
                item["first_ms"] = min(item["first_ms"], evidence.occurred_ms)
                item["last_ms"] = max(item["last_ms"], evidence.occurred_ms)
        return [
            {
                **item,
                "outcomes": dict(sorted(item["outcomes"].items())),
            }
            for _, item in sorted(grouped.items())
        ]

    def all(self) -> tuple[RelationshipRecord, ...]:
        result = []
        for actor_id in sorted(self._actors):
            record = self.get(actor_id)
            if record is not None:
                result.append(record)
        return tuple(result)

    def get(self, actor_id: str) -> RelationshipRecord | None:
        actor = self._actors.get(str(actor_id))
        if actor is None:
            return None
        subject = self.primary_subject(actor.actor_id)
        if subject is None:
            return None
        link = next(
            item for item in subject.actor_links if item.actor_id == actor.actor_id
        )
        relationship = self._relationships[subject.subject_ref]
        return RelationshipRecord(
            subject_ref=subject.subject_ref,
            actor_id=actor.actor_id,
            actor_label=actor.actor_label,
            circle=relationship.circle,
            state=relationship.state,
            relationship_labels=relationship.relationship_labels,
            subject_confidence=link.confidence,
            relationship_confidence=relationship.relationship_confidence,
            evidence_refs=_unique_text(
                (
                    *actor.evidence_refs,
                    *subject.evidence_refs,
                    *relationship.evidence_refs,
                ),
                label="relationship evidence reference",
                maximum=MAX_EVIDENCE_LENGTH,
            ),
            updated_ms=max(
                actor.updated_ms,
                subject.updated_ms,
                relationship.updated_ms,
            ),
        )
