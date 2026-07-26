from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .encoding import canonical_pack
from .relations import RELATION_CIRCLES


SUGGESTION_TYPES = frozenset(
    {
        "circle.advance",
        "context-trust.review",
    }
)


@dataclass(frozen=True)
class RelationshipSuggestion:
    suggestion_id: str
    suggestion_type: str
    subject_ref: str
    confidence: int
    proposed_circle: str
    context: str
    proposed_estimate: int | None
    evidence_tags: tuple[str, ...]
    rationale_codes: tuple[str, ...]
    metrics: tuple[tuple[str, int], ...]
    basis_hash: str

    def __post_init__(self) -> None:
        if not self.suggestion_id.startswith("rsg_"):
            raise ValueError("invalid relationship suggestion ID")
        if self.suggestion_type not in SUGGESTION_TYPES:
            raise ValueError("invalid relationship suggestion type")
        if not self.subject_ref.startswith("subj_"):
            raise ValueError("invalid relationship suggestion Subject")
        if not 0 <= self.confidence <= 100:
            raise ValueError("invalid relationship suggestion confidence")
        if self.proposed_circle and self.proposed_circle not in RELATION_CIRCLES:
            raise ValueError("invalid suggested relationship circle")
        if self.proposed_estimate is not None and not (
            0 <= self.proposed_estimate <= 100
        ):
            raise ValueError("invalid suggested contextual trust")
        if len(self.basis_hash) != 32:
            raise ValueError("invalid relationship suggestion basis")

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "suggestion_type": self.suggestion_type,
            "subject_ref": self.subject_ref,
            "confidence": self.confidence,
            "proposed_circle": self.proposed_circle,
            "context": self.context,
            "proposed_estimate": self.proposed_estimate,
            "evidence_tags": list(self.evidence_tags),
            "rationale_codes": list(self.rationale_codes),
            "metrics": dict(self.metrics),
            "basis_hash": self.basis_hash,
            "requires_explicit_action": True,
            "authorization_effect": "none",
        }


class RelationshipAdvisor:
    """Derive explainable suggestions without mutating the relationship model."""

    @classmethod
    def advise(
        cls,
        model: Mapping[str, Any],
        *,
        subject_ref: str = "",
    ) -> tuple[RelationshipSuggestion, ...]:
        subjects = {
            str(item["subject_ref"]): item
            for item in model.get("subjects", ())
            if isinstance(item, Mapping) and item.get("state") == "active"
        }
        relationships = {
            str(item["subject_ref"]): item
            for item in model.get("relationships", ())
            if isinstance(item, Mapping) and item.get("state") == "active"
        }
        interactions: dict[str, list[Mapping[str, Any]]] = {
            key: [] for key in subjects
        }
        for item in model.get("interactions", ()):
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("subject_ref", ""))
            if key in interactions:
                interactions[key].append(item)

        selected = str(subject_ref).strip()
        if selected and selected not in subjects:
            raise KeyError(f"unknown active Subject hypothesis: {selected}")

        decided = {
            str(item.get("suggestion_id", ""))
            for item in model.get("suggestion_decisions", ())
            if isinstance(item, Mapping)
        }
        suggestions: list[RelationshipSuggestion] = []
        for key in sorted(subjects):
            if selected and key != selected:
                continue
            relationship = relationships.get(key)
            if relationship is None:
                continue
            evidence = interactions[key]
            circle = cls._circle_suggestion(key, relationship, evidence)
            if circle is not None:
                suggestions.append(circle)
            trust = cls._task_delivery_suggestion(key, relationship, evidence)
            if trust is not None:
                suggestions.append(trust)
        return tuple(
            sorted(
                (
                    item
                    for item in suggestions
                    if item.suggestion_id not in decided
                ),
                key=lambda item: (item.subject_ref, item.suggestion_type),
            )
        )

    @classmethod
    def _circle_suggestion(
        cls,
        subject_ref: str,
        relationship: Mapping[str, Any],
        interactions: list[Mapping[str, Any]],
    ) -> RelationshipSuggestion | None:
        if str(relationship.get("circle", "")) != "known":
            return None
        tasks = [
            item
            for item in interactions
            if "task" in set(item.get("facets", ()))
        ]
        submitted = sum(
            1 for item in tasks if item.get("outcome") == "submitted"
        )
        completed = sum(
            1 for item in tasks if item.get("outcome") == "completed"
        )
        directions = {
            str(item.get("direction", ""))
            for item in tasks
            if item.get("direction") in {"incoming", "outgoing"}
        }
        balanced_events = min(submitted, completed)
        if balanced_events < 2 or len(directions) < 2:
            return None
        confidence = min(
            85,
            38 + balanced_events * 8 + min(len(tasks), 8),
        )
        return cls._build(
            suggestion_type="circle.advance",
            subject_ref=subject_ref,
            confidence=confidence,
            proposed_circle="collab",
            context="",
            proposed_estimate=None,
            evidence_tags=(
                "activity:reciprocal",
                "activity:task-collaboration",
            ),
            rationale_codes=(
                "task.repeated-submission-completion",
                "task.bidirectional",
                "circle.explicit-review-required",
            ),
            metrics={
                "task_interactions": len(tasks),
                "submitted": submitted,
                "completed": completed,
                "balanced_task_events": balanced_events,
                "directions": len(directions),
            },
            basis=tasks,
        )

    @classmethod
    def _task_delivery_suggestion(
        cls,
        subject_ref: str,
        relationship: Mapping[str, Any],
        interactions: list[Mapping[str, Any]],
    ) -> RelationshipSuggestion | None:
        terminal = [
            item
            for item in interactions
            if item.get("direction") == "incoming"
            and "task" in set(item.get("facets", ()))
            and item.get("outcome") in {"completed", "failed"}
        ]
        sample_size = len(terminal)
        if sample_size < 3:
            return None
        completed = sum(
            1 for item in terminal if item.get("outcome") == "completed"
        )
        failed = sample_size - completed
        # A symmetric Beta(2, 2) prior avoids turning three outcomes into
        # apparent certainty. Confidence grows separately with evidence count.
        posterior = round(100 * (completed + 2) / (sample_size + 4))
        confidence = min(90, 20 + sample_size * 10)
        estimate = round(50 + (posterior - 50) * confidence / 100)

        existing = next(
            (
                item
                for item in relationship.get("context_trust", ())
                if isinstance(item, Mapping)
                and item.get("context") == "task.delivery"
            ),
            None,
        )
        if existing is not None and not (
            sample_size >= 5
            and confidence >= int(existing.get("confidence", 0)) + 10
            and abs(estimate - int(existing.get("estimate", 0))) >= 10
        ):
            return None

        return cls._build(
            suggestion_type="context-trust.review",
            subject_ref=subject_ref,
            confidence=confidence,
            proposed_circle="",
            context="task.delivery",
            proposed_estimate=estimate,
            evidence_tags=("evidence:task-delivery",),
            rationale_codes=(
                "task.incoming-terminal-results",
                "trust.context-specific",
                "trust.explicit-review-required",
            ),
            metrics={
                "sample_size": sample_size,
                "completed": completed,
                "failed": failed,
                "posterior_success": posterior,
            },
            basis=terminal,
        )

    @staticmethod
    def _build(
        *,
        suggestion_type: str,
        subject_ref: str,
        confidence: int,
        proposed_circle: str,
        context: str,
        proposed_estimate: int | None,
        evidence_tags: tuple[str, ...],
        rationale_codes: tuple[str, ...],
        metrics: dict[str, int],
        basis: list[Mapping[str, Any]],
    ) -> RelationshipSuggestion:
        source = {
            "suggestion_type": suggestion_type,
            "subject_ref": subject_ref,
            "proposed_circle": proposed_circle,
            "context": context,
            "proposed_estimate": proposed_estimate,
            "interaction_ids": sorted(
                str(item.get("interaction_id", "")) for item in basis
            ),
        }
        basis_hash = hashlib.blake2s(
            canonical_pack(source),
            digest_size=16,
            person=b"anet-rsg",
        ).hexdigest()
        suggestion_id = "rsg_" + hashlib.blake2s(
            canonical_pack(
                {
                    "basis_hash": basis_hash,
                    "confidence": confidence,
                    "metrics": metrics,
                }
            ),
            digest_size=16,
            person=b"anetrsgi",
        ).hexdigest()
        return RelationshipSuggestion(
            suggestion_id=suggestion_id,
            suggestion_type=suggestion_type,
            subject_ref=subject_ref,
            confidence=confidence,
            proposed_circle=proposed_circle,
            context=context,
            proposed_estimate=proposed_estimate,
            evidence_tags=tuple(sorted(evidence_tags)),
            rationale_codes=rationale_codes,
            metrics=tuple(sorted(metrics.items())),
            basis_hash=basis_hash,
        )
