from __future__ import annotations

import json

import pytest

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relation_advisor import RelationshipAdvisor
from anet.relation_decisions import RelationshipDecisionManager
from anet.relations import InteractionEvidence, RelationshipBook


def _book_with_suggestion(tmp_path):
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    subject = book.observe_actor(
        peer.card(),
        evidence_ref="packet:initial",
        now=1_800_000_000_001,
    )
    book.set_circle(
        subject.subject_ref,
        "known",
        confidence=25,
        evidence_ref="packet:known",
        now=1_800_000_000_002,
    )
    for index in range(2):
        base = 1_800_000_001_000 + index * 10
        book.record_interaction(
            InteractionEvidence.create(
                actor_id=peer.node_id,
                subject_ref=subject.subject_ref,
                direction="outgoing",
                facets=("task",),
                context="task",
                outcome="submitted",
                evidence_ref=f"packet:request-{index}",
                occurred_ms=base,
            )
        )
        book.record_interaction(
            InteractionEvidence.create(
                actor_id=peer.node_id,
                subject_ref=subject.subject_ref,
                direction="incoming",
                facets=("task",),
                context="task",
                outcome="completed",
                evidence_ref=f"packet:result-{index}",
                occurred_ms=base + 1,
            )
        )
    suggestion = RelationshipAdvisor.advise(book.snapshot())[0]
    return observer, peer, book, subject, suggestion


def test_accept_is_atomic_auditable_and_idempotent(tmp_path) -> None:
    observer, _peer, book, subject, suggestion = _book_with_suggestion(tmp_path)

    decision = RelationshipDecisionManager.decide(
        book,
        suggestion.suggestion_id,
        "accepted",
        rationale="agent:bounded-collaboration-confirmed",
    )

    assert decision.decision == "accepted"
    assert decision.applied is True
    assert decision.to_dict()["authorization_effect"] == "none"
    assert book.relationship(subject.subject_ref).circle == "collab"
    assert RelationshipAdvisor.advise(book.snapshot()) == ()
    assert book.snapshot()["suggestion_decisions"] == [decision.to_dict()]
    assert [item["event_type"] for item in book.snapshot()["events"][-2:]] == [
        "relationship.circle-set",
        "relationship.suggestion-accepted",
    ]

    reloaded = RelationshipBook(book.path, own_actor_id=observer.node_id)
    again = RelationshipDecisionManager.decide(
        reloaded,
        suggestion.suggestion_id,
        "accepted",
        rationale="agent:retry",
    )
    assert again == decision
    assert len(reloaded.suggestion_decisions()) == 1


def test_reject_records_basis_without_mutating_relationship(tmp_path) -> None:
    observer, _peer, book, subject, suggestion = _book_with_suggestion(tmp_path)
    before = book.relationship(subject.subject_ref)

    decision = RelationshipDecisionManager.decide(
        book,
        suggestion.suggestion_id,
        "rejected",
        rationale="agent:insufficient-social-context",
    )

    assert decision.decision == "rejected"
    assert decision.applied is False
    assert decision.basis_hash == suggestion.basis_hash
    assert book.relationship(subject.subject_ref) == before
    assert RelationshipAdvisor.advise(book.snapshot()) == ()
    assert book.snapshot()["events"][-1]["event_type"] == (
        "relationship.suggestion-rejected"
    )
    reloaded = RelationshipBook(book.path, own_actor_id=observer.node_id)
    assert reloaded.suggestion_decision(suggestion.suggestion_id) == decision


def test_stale_suggestion_cannot_be_decided_after_basis_changes(tmp_path) -> None:
    _observer, peer, book, subject, suggestion = _book_with_suggestion(tmp_path)
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("task",),
            context="task",
            outcome="completed",
            evidence_ref="packet:new-result",
            occurred_ms=1_800_000_002_000,
        )
    )
    current_ids = {
        item.suggestion_id
        for item in RelationshipAdvisor.advise(book.snapshot())
    }
    assert suggestion.suggestion_id not in current_ids

    with pytest.raises(ValueError, match="stale"):
        RelationshipDecisionManager.decide(
            book,
            suggestion.suggestion_id,
            "accepted",
            rationale="agent:late-decision",
        )
    assert book.relationship(subject.subject_ref).circle == "known"
    assert book.suggestion_decisions() == ()


def test_decision_cannot_be_reversed(tmp_path) -> None:
    _observer, _peer, book, _subject, suggestion = _book_with_suggestion(tmp_path)
    RelationshipDecisionManager.decide(
        book,
        suggestion.suggestion_id,
        "rejected",
        rationale="agent:not-now",
    )
    with pytest.raises(ValueError, match="another decision"):
        RelationshipDecisionManager.decide(
            book,
            suggestion.suggestion_id,
            "accepted",
            rationale="agent:changed-mind",
        )


def test_accepting_task_delivery_review_changes_only_that_context(
    tmp_path,
) -> None:
    _observer, peer, book, subject, _circle = _book_with_suggestion(tmp_path)
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("task",),
            context="task",
            outcome="failed",
            evidence_ref="packet:third-result",
            occurred_ms=1_800_000_002_000,
        )
    )
    trust = next(
        item
        for item in RelationshipAdvisor.advise(book.snapshot())
        if item.suggestion_type == "context-trust.review"
    )
    before_circle = book.relationship(subject.subject_ref).circle

    decision = RelationshipDecisionManager.decide(
        book,
        trust.suggestion_id,
        "accepted",
        rationale="agent:delivery-sample-reviewed",
    )

    relationship = book.relationship(subject.subject_ref)
    assert decision.context == "task.delivery"
    assert relationship.circle == before_circle
    assert [(item.context, item.estimate, item.confidence) for item in (
        relationship.context_trust
    )] == [("task.delivery", trust.proposed_estimate, trust.confidence)]


def test_relation_decide_cli_applies_and_lists_history(
    tmp_path,
    capsys,
) -> None:
    home = tmp_path / "observer"
    assert main(
        [
            "--home",
            str(home),
            "init",
            "--label",
            "observer",
            "--port",
            "48411",
        ]
    ) == 0
    capsys.readouterr()
    config = NodeConfig.load(home)
    observer = Identity.load(config.identity_path)
    peer = Identity.generate("peer")
    book = RelationshipBook(
        config.relationships_path,
        own_actor_id=observer.node_id,
    )
    subject = book.observe_actor(
        peer.card(),
        evidence_ref="packet:peer",
        now=1_800_000_000_001,
    )
    book.set_circle(
        subject.subject_ref,
        "known",
        confidence=25,
        evidence_ref="packet:known",
        now=1_800_000_000_002,
    )
    for index in range(2):
        base = 1_800_000_001_000 + index * 10
        for direction, outcome, suffix in (
            ("outgoing", "submitted", "request"),
            ("incoming", "completed", "result"),
        ):
            book.record_interaction(
                InteractionEvidence.create(
                    actor_id=peer.node_id,
                    subject_ref=subject.subject_ref,
                    direction=direction,
                    facets=("task",),
                    context="task",
                    outcome=outcome,
                    evidence_ref=f"packet:{suffix}-{index}",
                    occurred_ms=base + int(direction == "incoming"),
                )
            )

    suggestion = RelationshipAdvisor.advise(book.snapshot())[0]
    assert main(
        [
            "--home",
            str(home),
            "relation-decide",
            suggestion.suggestion_id,
            "accepted",
            "--reason",
            "agent:reviewed-evidence",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "accepted"
    assert result["authorization_effect"] == "none"

    assert main(
        [
            "--home",
            str(home),
            "relation-decision-list",
            "--subject",
            subject.subject_ref,
        ]
    ) == 0
    history = json.loads(capsys.readouterr().out)
    assert [item["suggestion_id"] for item in history] == [
        suggestion.suggestion_id
    ]

    assert main(["--home", str(home), "relation-list", "--model"]) == 0
    model = json.loads(capsys.readouterr().out)
    assert model["version"] == 6
    assert model["relationship_suggestions"] == []
    assert model["suggestion_decisions"] == history
