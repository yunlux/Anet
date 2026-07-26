from __future__ import annotations

import json

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relation_advisor import RelationshipAdvisor
from anet.relations import InteractionEvidence, RelationshipBook


def _book_with_peer(tmp_path):
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
    return observer, peer, book, subject


def _task_cycle(
    book: RelationshipBook,
    peer: Identity,
    subject_ref: str,
    index: int,
    *,
    result: str = "completed",
) -> None:
    base = 1_800_000_001_000 + index * 10
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject_ref,
            direction="outgoing",
            facets=("skill", "task"),
            context="task",
            outcome="submitted",
            evidence_ref=f"packet:request-{index}",
            occurred_ms=base,
        )
    )
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject_ref,
            direction="incoming",
            facets=("artifact", "task"),
            context="task",
            outcome=result,
            evidence_ref=f"packet:result-{index}",
            occurred_ms=base + 1,
        )
    )


def test_repeated_tasks_suggest_collab_but_do_not_mutate(tmp_path) -> None:
    _observer, peer, book, subject = _book_with_peer(tmp_path)
    _task_cycle(book, peer, subject.subject_ref, 1)
    _task_cycle(book, peer, subject.subject_ref, 2)
    before = book.snapshot()

    suggestions = RelationshipAdvisor.advise(before)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.suggestion_type == "circle.advance"
    assert suggestion.proposed_circle == "collab"
    assert suggestion.metrics == (
        ("balanced_task_events", 2),
        ("completed", 2),
        ("directions", 2),
        ("submitted", 2),
        ("task_interactions", 4),
    )
    assert suggestion.to_dict()["requires_explicit_action"] is True
    assert suggestion.to_dict()["authorization_effect"] == "none"
    assert book.snapshot() == before
    assert RelationshipAdvisor.advise(before) == suggestions


def test_task_delivery_trust_is_narrow_and_shrunk_for_small_samples(
    tmp_path,
) -> None:
    _observer, peer, book, subject = _book_with_peer(tmp_path)
    _task_cycle(book, peer, subject.subject_ref, 1)
    _task_cycle(book, peer, subject.subject_ref, 2)
    _task_cycle(book, peer, subject.subject_ref, 3, result="failed")

    suggestions = RelationshipAdvisor.advise(book.snapshot())
    trust = next(
        item
        for item in suggestions
        if item.suggestion_type == "context-trust.review"
    )
    assert trust.context == "task.delivery"
    assert trust.confidence == 50
    assert trust.proposed_estimate == 54
    assert dict(trust.metrics) == {
        "completed": 2,
        "failed": 1,
        "posterior_success": 57,
        "sample_size": 3,
    }
    assert "trust.context-specific" in trust.rationale_codes


def test_message_volume_and_discord_activity_never_suggest_closer_circle(
    tmp_path,
) -> None:
    _observer, peer, book, subject = _book_with_peer(tmp_path)
    for index in range(50):
        book.record_interaction(
            InteractionEvidence.create(
                actor_id=peer.node_id,
                subject_ref=subject.subject_ref,
                direction="incoming" if index % 2 else "outgoing",
                facets=("message",),
                context="social.discord",
                outcome="received" if index % 2 else "queued",
                evidence_ref=f"discord-event:{index:032x}",
                occurred_ms=1_800_000_010_000 + index,
            )
        )
    assert RelationshipAdvisor.advise(book.snapshot()) == ()


def test_advisor_never_suggests_friend_close_family_or_subject_link(
    tmp_path,
) -> None:
    _observer, peer, book, subject = _book_with_peer(tmp_path)
    for index in range(8):
        _task_cycle(book, peer, subject.subject_ref, index)
    suggestions = RelationshipAdvisor.advise(book.snapshot())
    assert {
        item.suggestion_type for item in suggestions
    } <= {"circle.advance", "context-trust.review"}
    assert {
        item.proposed_circle for item in suggestions if item.proposed_circle
    } == {"collab"}


def test_relation_suggest_cli_returns_separate_explicit_commands(
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
            "48401",
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
    _task_cycle(book, peer, subject.subject_ref, 1)
    _task_cycle(book, peer, subject.subject_ref, 2)

    assert main(
        [
            "--home",
            str(home),
            "relation-suggest",
            "--subject",
            subject.subject_ref,
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["suggestions"]) == 1
    suggestion = result["suggestions"][0]
    assert suggestion["explicit_command"][:3] == [
        "relation-circle",
        subject.subject_ref,
        "collab",
    ]
    assert suggestion["authorization_effect"] == "none"

    assert main(
        [
            "--home",
            str(home),
            "relation-list",
            "--model",
        ]
    ) == 0
    model = json.loads(capsys.readouterr().out)
    assert model["relationship_suggestions"][0]["suggestion_id"] == (
        suggestion["suggestion_id"]
    )
