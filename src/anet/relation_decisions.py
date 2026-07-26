from __future__ import annotations

from .relation_advisor import RelationshipAdvisor
from .relations import RelationshipBook, SuggestionDecision


class RelationshipDecisionManager:
    """Resolve and atomically decide one currently reproducible suggestion."""

    @staticmethod
    def decide(
        book: RelationshipBook,
        suggestion_id: str,
        decision: str,
        *,
        rationale: str,
    ) -> SuggestionDecision:
        existing = book.suggestion_decision(suggestion_id)
        if existing is not None:
            if existing.decision != decision:
                raise ValueError(
                    "relationship suggestion already has another decision"
                )
            return existing

        current = {
            item.suggestion_id: item
            for item in RelationshipAdvisor.advise(book.snapshot())
        }
        suggestion = current.get(str(suggestion_id))
        if suggestion is None:
            raise ValueError(
                "relationship suggestion is unknown, stale, or no longer applicable"
            )
        return book.decide_suggestion(
            suggestion.to_dict(),
            decision,
            rationale=rationale,
        )
