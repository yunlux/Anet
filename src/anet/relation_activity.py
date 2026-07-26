from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping


_CURSOR_RE = re.compile(
    r"^rac_([0-9a-f]{16})_(revt_[0-9a-f]{24})$"
)


def _observer_scope(observer_actor_id: str) -> str:
    return hashlib.blake2s(
        observer_actor_id.encode("utf-8"),
        digest_size=8,
        person=b"anet-rac",
    ).hexdigest()


def _evidence_digest(evidence_ref: str) -> str:
    return hashlib.blake2s(
        evidence_ref.encode("utf-8"),
        digest_size=16,
        person=b"anet-rav",
    ).hexdigest()


@dataclass(frozen=True)
class RelationshipActivity:
    activity_id: str
    cursor: str
    activity_type: str
    category: str
    fact_level: str
    actor_id: str
    subject_ref: str
    occurred_ms: int
    details: tuple[tuple[str, Any], ...]
    evidence_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "cursor": self.cursor,
            "activity_type": self.activity_type,
            "category": self.category,
            "fact_level": self.fact_level,
            "actor_id": self.actor_id,
            "subject_ref": self.subject_ref,
            "occurred_ms": self.occurred_ms,
            "details": dict(self.details),
            "evidence_digest": self.evidence_digest,
            "privacy": "content-free",
            "authorization_effect": "none",
        }


@dataclass(frozen=True)
class RelationshipActivityPage:
    observer_actor_id: str
    activities: tuple[RelationshipActivity, ...]
    next_cursor: str
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "observer_actor_id": self.observer_actor_id,
            "activities": [item.to_dict() for item in self.activities],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "ordering": "observer-local-append",
            "privacy": "content-free",
            "authorization_effect": "none",
        }


class RelationshipActivityFeed:
    """Project one relationship book into an incremental content-free feed."""

    @classmethod
    def read(
        cls,
        model: Mapping[str, Any],
        *,
        after: str = "",
        limit: int = 100,
        subject_ref: str = "",
        tail: bool = False,
    ) -> RelationshipActivityPage:
        observer = str(model.get("observer_actor_id", ""))
        if not observer:
            raise ValueError("relationship activity requires an observer Actor")
        page_limit = int(limit)
        if not 1 <= page_limit <= 500:
            raise ValueError("relationship activity limit must be 1-500")
        selected_subject = str(subject_ref).strip()
        if tail and (after or selected_subject):
            raise ValueError(
                "relationship activity tail cannot use after or subject filters"
            )
        subjects = {
            str(item.get("subject_ref", ""))
            for item in model.get("subjects", ())
            if isinstance(item, Mapping)
        }
        if selected_subject and selected_subject not in subjects:
            raise KeyError(
                f"unknown Subject hypothesis: {selected_subject}"
            )

        events = [
            item
            for item in model.get("events", ())
            if isinstance(item, Mapping)
        ]
        start = (
            max(0, len(events) - page_limit)
            if tail
            else cls._start_index(
                events,
                after=str(after).strip(),
                observer_actor_id=observer,
            )
        )
        interaction_lookup = {
            (
                str(item.get("actor_id", "")),
                str(item.get("subject_ref", "")),
                str(item.get("evidence_ref", "")),
                int(item.get("occurred_ms", 0)),
            ): item
            for item in model.get("interactions", ())
            if isinstance(item, Mapping)
        }
        transitions = [
            item
            for item in model.get("subject_transitions", ())
            if isinstance(item, Mapping)
        ]
        decisions = {
            str(item.get("suggestion_id", "")): item
            for item in model.get("suggestion_decisions", ())
            if isinstance(item, Mapping)
        }
        actors = {
            str(item.get("actor_id", "")): item
            for item in model.get("actors", ())
            if isinstance(item, Mapping)
        }

        result: list[RelationshipActivity] = []
        index = start
        while index < len(events) and len(result) < page_limit:
            event = events[index]
            index += 1
            if (
                selected_subject
                and str(event.get("subject_ref", "")) != selected_subject
            ):
                continue
            result.append(
                cls._project(
                    event,
                    observer_actor_id=observer,
                    interactions=interaction_lookup,
                    transitions=transitions,
                    decisions=decisions,
                    actors=actors,
                )
            )

        last_event_id = (
            str(events[index - 1].get("event_id", ""))
            if index > start
            else ""
        )
        next_cursor = (
            cls._cursor(observer, last_event_id)
            if last_event_id
            else str(after).strip()
        )
        return RelationshipActivityPage(
            observer_actor_id=observer,
            activities=tuple(result),
            next_cursor=next_cursor,
            has_more=index < len(events),
        )

    @staticmethod
    def _start_index(
        events: list[Mapping[str, Any]],
        *,
        after: str,
        observer_actor_id: str,
    ) -> int:
        if not after:
            return 0
        match = _CURSOR_RE.fullmatch(after)
        if match is None:
            raise ValueError("invalid relationship activity cursor")
        if match.group(1) != _observer_scope(observer_actor_id):
            raise ValueError(
                "relationship activity cursor belongs to another observer"
            )
        event_id = match.group(2)
        for index, event in enumerate(events):
            if event.get("event_id") == event_id:
                return index + 1
        raise ValueError("relationship activity cursor is unknown or stale")

    @staticmethod
    def _cursor(observer_actor_id: str, event_id: str) -> str:
        if not re.fullmatch(r"revt_[0-9a-f]{24}", event_id):
            raise ValueError("invalid relationship event ID for cursor")
        return f"rac_{_observer_scope(observer_actor_id)}_{event_id}"

    @classmethod
    def _project(
        cls,
        event: Mapping[str, Any],
        *,
        observer_actor_id: str,
        interactions: Mapping[tuple[str, str, str, int], Mapping[str, Any]],
        transitions: list[Mapping[str, Any]],
        decisions: Mapping[str, Mapping[str, Any]],
        actors: Mapping[str, Mapping[str, Any]],
    ) -> RelationshipActivity:
        event_id = str(event.get("event_id", ""))
        event_type = str(event.get("event_type", ""))
        actor_id = str(event.get("actor_id", ""))
        subject_ref = str(event.get("subject_ref", ""))
        occurred_ms = int(event.get("observed_ms", 0))
        evidence_ref = str(event.get("evidence_ref", ""))
        details: dict[str, Any] = dict(event.get("details", {}))

        if event_type.startswith("actor."):
            category = "actor"
            fact_level = "verified"
            actor = actors.get(actor_id)
            if actor is not None:
                details.setdefault(
                    "actor_kind",
                    str(actor.get("actor_kind", "")),
                )
            details["actor_state"] = (
                "revoked"
                if event_type == "actor.revoked"
                else "observed"
            )
        elif event_type == "interaction.observed":
            category = "interaction"
            fact_level = "verified"
            interaction = interactions.get(
                (actor_id, subject_ref, evidence_ref, occurred_ms)
            )
            if interaction is not None:
                details.update(
                    {
                        "interaction_id": str(
                            interaction.get("interaction_id", "")
                        ),
                        "direction": str(
                            interaction.get("direction", "")
                        ),
                        "facets": list(interaction.get("facets", ())),
                        "context": str(interaction.get("context", "")),
                        "outcome": str(interaction.get("outcome", "")),
                    }
                )
        elif event_type.startswith("subject."):
            category = "subject"
            fact_level = "inference"
            transition = cls._transition_for_event(
                event,
                transitions=transitions,
            )
            if transition is not None:
                details.update(
                    {
                        "transition_id": str(
                            transition.get("transition_id", "")
                        ),
                        "transition_type": str(
                            transition.get("transition_type", "")
                        ),
                        "source_subject_refs": list(
                            transition.get("source_subject_refs", ())
                        ),
                        "replacement_subject_refs": list(
                            transition.get(
                                "replacement_subject_refs",
                                (),
                            )
                        ),
                        "confidence": int(
                            transition.get("confidence", 0)
                        ),
                    }
                )
        elif event_type.startswith("relationship.suggestion-"):
            category = "decision"
            fact_level = "decision"
            suggestion_id = (
                evidence_ref.removeprefix("suggestion:")
                if evidence_ref.startswith("suggestion:")
                else ""
            )
            decision = decisions.get(suggestion_id)
            if decision is not None:
                details.update(
                    {
                        "decision_id": str(
                            decision.get("decision_id", "")
                        ),
                        "suggestion_id": suggestion_id,
                        "suggestion_type": str(
                            decision.get("suggestion_type", "")
                        ),
                        "decision": str(decision.get("decision", "")),
                        "proposed_circle": str(
                            decision.get("proposed_circle", "")
                        ),
                        "context": str(decision.get("context", "")),
                        "proposed_estimate": decision.get(
                            "proposed_estimate"
                        ),
                        "applied": bool(decision.get("applied", False)),
                        "rationale_digest": _evidence_digest(
                            str(decision.get("rationale", ""))
                        ),
                    }
                )
        else:
            category = "relationship"
            fact_level = "estimate"

        return RelationshipActivity(
            activity_id=event_id,
            cursor=cls._cursor(observer_actor_id, event_id),
            activity_type=event_type,
            category=category,
            fact_level=fact_level,
            actor_id=actor_id,
            subject_ref=subject_ref,
            occurred_ms=occurred_ms,
            details=tuple(sorted(details.items())),
            evidence_digest=_evidence_digest(evidence_ref),
        )

    @staticmethod
    def _transition_for_event(
        event: Mapping[str, Any],
        *,
        transitions: list[Mapping[str, Any]],
    ) -> Mapping[str, Any] | None:
        event_type = str(event.get("event_type", ""))
        transition_type = event_type.removeprefix("subject.")
        subject_ref = str(event.get("subject_ref", ""))
        evidence_ref = str(event.get("evidence_ref", ""))
        observed_ms = int(event.get("observed_ms", 0))
        return next(
            (
                item
                for item in transitions
                if item.get("transition_type") == transition_type
                and subject_ref in item.get("source_subject_refs", ())
                and item.get("evidence_ref") == evidence_ref
                and int(item.get("observed_ms", 0)) == observed_ms
            ),
            None,
        )
