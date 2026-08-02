from __future__ import annotations

import json

import pytest

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relation_activity import RelationshipActivityFeed
from anet.relation_advisor import RelationshipAdvisor
from anet.relation_decisions import RelationshipDecisionManager
from anet.relations import (
    InteractionEvidence,
    RelationshipBook,
    RelationshipEvent,
)


def _observed_book(tmp_path):
    observer = Identity.generate("observer")
    peer = Identity.generate("secret-human-name")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    subject = book.observe_actor(
        peer.card(),
        evidence_ref="packet:actor-secret-reference",
        now=1_800_000_000_100,
    )
    return observer, peer, book, subject


def test_feed_uses_append_order_and_projects_historical_details(tmp_path) -> None:
    _observer, peer, book, subject = _observed_book(tmp_path)
    book.set_circle(
        subject.subject_ref,
        "known",
        confidence=41,
        evidence_ref="relationship:known-private-ref",
        now=1_800_000_000_300,
    )
    # This observation occurred earlier but was appended later. Cursor order
    # must follow the durable book, not wall-clock sorting.
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("message",),
            context="social.discord",
            outcome="received",
            evidence_ref="discord-event:private-source",
            occurred_ms=1_800_000_000_200,
        )
    )

    page = RelationshipActivityFeed.read(book.snapshot())
    values = page.to_dict()
    assert [item["activity_type"] for item in values["activities"]] == [
        "actor.observed",
        "relationship.circle-set",
        "interaction.observed",
    ]
    assert [item["occurred_ms"] for item in values["activities"]] == [
        1_800_000_000_100,
        1_800_000_000_300,
        1_800_000_000_200,
    ]
    assert values["activities"][1]["details"] == {
        "circle": "known",
        "confidence": 41,
    }
    assert values["activities"][2]["details"]["facets"] == ["message"]
    assert values["activities"][2]["details"]["outcome"] == "received"
    rendered = json.dumps(values)
    assert "private-ref" not in rendered
    assert "private-source" not in rendered
    assert "secret-human-name" not in rendered
    assert values["privacy"] == "content-free"
    assert values["authorization_effect"] == "none"


def test_cursor_pages_are_scoped_to_one_observer(tmp_path) -> None:
    observer, _peer, book, subject = _observed_book(tmp_path)
    book.set_circle(
        subject.subject_ref,
        "known",
        confidence=41,
        evidence_ref="relationship:known",
        now=1_800_000_000_200,
    )

    first = RelationshipActivityFeed.read(book.snapshot(), limit=1)
    assert len(first.activities) == 1
    assert first.has_more is True
    second = RelationshipActivityFeed.read(
        book.snapshot(),
        after=first.next_cursor,
        limit=10,
    )
    assert [item.activity_type for item in second.activities] == [
        "relationship.circle-set"
    ]
    assert second.has_more is False

    other_model = dict(book.snapshot())
    other_model["observer_actor_id"] = Identity.generate("other").node_id
    with pytest.raises(ValueError, match="another observer"):
        RelationshipActivityFeed.read(
            other_model,
            after=first.next_cursor,
        )
    assert observer.node_id in first.to_dict()["observer_actor_id"]


def test_subject_filter_advances_over_other_subjects(tmp_path) -> None:
    observer, _peer, book, first_subject = _observed_book(tmp_path)
    other = Identity.generate("other")
    second_subject = book.observe_actor(
        other.card(),
        evidence_ref="packet:other",
        now=1_800_000_000_200,
    )
    book.set_circle(
        first_subject.subject_ref,
        "known",
        confidence=32,
        evidence_ref="relationship:first",
        now=1_800_000_000_300,
    )

    page = RelationshipActivityFeed.read(
        book.snapshot(),
        subject_ref=first_subject.subject_ref,
    )
    assert [item.subject_ref for item in page.activities] == [
        first_subject.subject_ref,
        first_subject.subject_ref,
    ]
    assert page.has_more is False
    assert page.next_cursor
    assert second_subject.subject_ref != first_subject.subject_ref
    assert page.observer_actor_id == observer.node_id


def test_decision_activity_redacts_rationale_and_keeps_effect(tmp_path) -> None:
    _observer, peer, book, subject = _observed_book(tmp_path)
    book.set_circle(
        subject.subject_ref,
        "known",
        confidence=25,
        evidence_ref="relationship:known",
        now=1_800_000_000_200,
    )
    for index in range(2):
        for offset, direction, outcome in (
            (0, "outgoing", "submitted"),
            (1, "incoming", "completed"),
        ):
            book.record_interaction(
                InteractionEvidence.create(
                    actor_id=peer.node_id,
                    subject_ref=subject.subject_ref,
                    direction=direction,
                    facets=("task",),
                    context="task",
                    outcome=outcome,
                    evidence_ref=f"packet:task-{index}-{offset}",
                    occurred_ms=1_800_000_001_000 + index * 10 + offset,
                )
            )
    suggestion = RelationshipAdvisor.advise(book.snapshot())[0]
    RelationshipDecisionManager.decide(
        book,
        suggestion.suggestion_id,
        "accepted",
        rationale="private rationale words must not leave the book",
    )

    page = RelationshipActivityFeed.read(book.snapshot())
    decision = next(
        item.to_dict()
        for item in page.activities
        if item.activity_type == "relationship.suggestion-accepted"
    )
    assert decision["category"] == "decision"
    assert decision["fact_level"] == "decision"
    assert decision["details"]["decision"] == "accepted"
    assert decision["details"]["proposed_circle"] == "collab"
    assert decision["details"]["applied"] is True
    assert "rationale_digest" in decision["details"]
    assert "private rationale" not in json.dumps(decision)


def test_relation_activity_cli_and_model_projection(tmp_path, capsys) -> None:
    home = tmp_path / "observer"
    assert main(
        [
            "--home",
            str(home),
            "init",
            "--label",
            "observer",
            "--port",
            "48421",
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
    book.observe_actor(
        peer.card(),
        evidence_ref="packet:peer",
        now=1_800_000_000_100,
    )

    assert main(
        [
            "--home",
            str(home),
            "relation-activity",
            "--limit",
            "1",
        ]
    ) == 0
    page = json.loads(capsys.readouterr().out)
    assert len(page["activities"]) == 1
    assert page["next_cursor"].startswith("rac_")

    assert main(
        [
            "--home",
            str(home),
            "relation-activity",
            "--after",
            page["next_cursor"],
            "--wait",
            "0.01",
        ]
    ) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["activities"] == []
    assert empty["next_cursor"] == page["next_cursor"]

    assert main(["--home", str(home), "relation-list", "--model"]) == 0
    model = json.loads(capsys.readouterr().out)
    assert model["version"] == 7
    assert model["relationship_activity"]["activities"][0][
        "activity_type"
    ] == "actor.observed"


def test_tail_projection_keeps_latest_bounded_window() -> None:
    observer = Identity.generate("observer")
    events = [
        {
            "event_id": f"revt_{index:024x}",
            "event_type": "relationship.circle-set",
            "actor_id": "",
            "subject_ref": "subj_0011223344556677",
            "evidence_ref": f"event:{index}",
            "observed_ms": 1_800_000_000_000 + index,
            "details": {"circle": "known", "confidence": 20},
        }
        for index in range(600)
    ]
    model = {
        "observer_actor_id": observer.node_id,
        "subjects": [{"subject_ref": "subj_0011223344556677"}],
        "events": events,
    }

    page = RelationshipActivityFeed.read(
        model,
        limit=500,
        tail=True,
    )

    assert len(page.activities) == 500
    assert page.activities[0].activity_id == "revt_" + f"{100:024x}"
    assert page.activities[-1].activity_id == "revt_" + f"{599:024x}"
    assert page.has_more is False


def test_relationship_end_projects_as_a_local_estimate_without_evidence() -> None:
    observer = Identity.generate("observer")
    model = {
        "observer_actor_id": observer.node_id,
        "subjects": [{"subject_ref": "subj_0011223344556677"}],
        "events": [
            {
                "event_id": "revt_" + "4" * 24,
                "event_type": "relationship.ended",
                "actor_id": "",
                "subject_ref": "subj_0011223344556677",
                "evidence_ref": "operator:relationship-ended",
                "observed_ms": 1_800_000_000_001,
                "details": {"state": "ended"},
            }
        ],
    }

    item = RelationshipActivityFeed.read(model).activities[0].to_dict()

    assert item["activity_type"] == "relationship.ended"
    assert item["category"] == "relationship"
    assert item["fact_level"] == "estimate"
    assert item["details"] == {"state": "ended"}
    assert "operator:relationship-ended" not in json.dumps(item)
    assert item["authorization_effect"] == "none"


def test_event_details_reject_unbounded_or_wrong_domain_fields() -> None:
    with pytest.raises(ValueError, match="details"):
        RelationshipEvent(
            event_id="revt_" + "1" * 24,
            event_type="relationship.circle-set",
            actor_id="",
            subject_ref="subj_0011223344556677",
            evidence_ref="relationship:test",
            observed_ms=1_800_000_000_001,
            details=(("raw_message", "secret"),),
        )
    with pytest.raises(ValueError, match="circle"):
        RelationshipEvent(
            event_id="revt_" + "2" * 24,
            event_type="relationship.circle-set",
            actor_id="",
            subject_ref="subj_0011223344556677",
            evidence_ref="relationship:test",
            observed_ms=1_800_000_000_001,
            details=(("circle", "trusted-superuser"),),
        )
    with pytest.raises(ValueError, match="details"):
        RelationshipEvent.from_dict(
            {
                "event_id": "revt_" + "3" * 24,
                "event_type": "relationship.context-trust-set",
                "actor_id": "",
                "subject_ref": "subj_0011223344556677",
                "evidence_ref": "relationship:test",
                "observed_ms": 1_800_000_000_001,
                "details": {"context": {"raw": "private"}},
            }
        )
