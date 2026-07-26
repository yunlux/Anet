from __future__ import annotations

import json

import pytest

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relations import RelationshipBook


def test_relationship_book_keeps_actor_facts_and_subject_hypotheses_separate(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    first = Identity.generate("first")
    second = Identity.generate("second")
    path = tmp_path / "relationships.json"
    book = RelationshipBook(path, own_actor_id=observer.node_id)

    first_subject = book.observe_actor(
        first.card(),
        evidence_ref="packet:first",
        now=1_800_000_000_001,
    )
    second_subject = book.observe_actor(
        second.card(),
        evidence_ref="packet:second",
        now=1_800_000_000_002,
    )
    assert first_subject.subject_ref != second_subject.subject_ref

    linked = book.link_actor(
        second.node_id,
        first_subject.subject_ref,
        confidence=84,
        evidence_ref="claim:same-controller",
        now=1_800_000_000_003,
    )
    assert {item.actor_id for item in linked.actor_links} == {
        first.node_id,
        second.node_id,
    }
    assert book.primary_subject(second.node_id) == linked
    assert book.subject(second_subject.subject_ref) is not None

    relation = book.set_circle(
        first_subject.subject_ref,
        "close",
        confidence=72,
        evidence_ref="relationship:confirmed",
        labels=("research-partner",),
        now=1_800_000_000_004,
    )
    assert relation.circle == "close"
    relation = book.set_context_trust(
        first_subject.subject_ref,
        "code.review",
        estimate=88,
        confidence=76,
        evidence_ref="task:review-42",
        now=1_800_000_000_005,
    )
    assert relation.context_trust[0].estimate == 88

    snapshot = book.snapshot()
    assert len(snapshot["actors"]) == 2
    assert len(snapshot["subjects"]) == 2
    assert len(snapshot["relationships"]) == 2
    assert [item["event_type"] for item in snapshot["events"]] == [
        "actor.observed",
        "actor.observed",
        "subject.actor-linked",
        "relationship.circle-set",
        "relationship.context-trust-set",
    ]

    reloaded = RelationshipBook(path, own_actor_id=observer.node_id)
    assert reloaded.snapshot() == snapshot
    assert reloaded.get(second.node_id).subject_ref == first_subject.subject_ref


def test_actor_revocation_does_not_rewrite_the_social_relationship(tmp_path) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    book = RelationshipBook(path, own_actor_id=observer.node_id)
    relation = book.confirm_friend(
        peer.card(),
        evidence_ref="friend:accepted",
        now=1_800_000_000_001,
    )

    revoked = book.revoke_actor(
        peer.node_id,
        evidence_ref="peer-key:revoked",
        now=1_800_000_000_002,
    )
    assert revoked is not None
    assert revoked.subject_ref == relation.subject_ref
    assert revoked.circle == "friend"
    assert revoked.state == "active"
    assert book.snapshot()["actors"][0]["state"] == "revoked"


def test_version_one_relationship_book_migrates_without_claiming_identity(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "relationships": [
                    {
                        "subject_ref": "subj_0011223344556677",
                        "actor_id": peer.node_id,
                        "actor_label": "peer",
                        "circle": "friend",
                        "state": "active",
                        "relationship_labels": ["relationship:friend"],
                        "subject_confidence": 50,
                        "relationship_confidence": 100,
                        "evidence_refs": ["friend:legacy"],
                        "updated_ms": 1_800_000_000_001,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    book = RelationshipBook(path, own_actor_id=observer.node_id)
    snapshot = book.snapshot()
    assert snapshot["version"] == 3
    assert snapshot["actors"][0]["actor_id"] == peer.node_id
    assert snapshot["subjects"][0]["confidence"] == 50
    assert snapshot["relationships"][0]["circle"] == "friend"
    assert snapshot["events"] == []

    book.set_context_trust(
        "subj_0011223344556677",
        "message",
        estimate=70,
        confidence=60,
        evidence_ref="message:legacy-upgrade",
        now=1_800_000_000_002,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3


def test_version_two_relationship_book_loads_with_empty_interactions(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    original = RelationshipBook(path, own_actor_id=observer.node_id)
    original.observe_actor(
        peer.card(),
        evidence_ref="packet:legacy-v2",
        now=1_800_000_000_001,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = 2
    value.pop("interactions", None)
    path.write_text(json.dumps(value), encoding="utf-8")

    migrated = RelationshipBook(path, own_actor_id=observer.node_id)
    snapshot = migrated.snapshot()
    assert snapshot["version"] == 3
    assert snapshot["interactions"] == []
    assert snapshot["interaction_stats"] == []
    assert migrated.get(peer.node_id) is not None


def test_relationship_book_rejects_unknown_links_and_invalid_scores(tmp_path) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    subject = book.observe_actor(
        peer.card(),
        evidence_ref="packet:peer",
        now=1_800_000_000_001,
    )

    with pytest.raises(KeyError, match="unknown Actor"):
        book.link_actor(
            "an1missing",
            subject.subject_ref,
            confidence=50,
            evidence_ref="claim:invalid",
        )
    with pytest.raises(ValueError, match="context trust estimate"):
        book.set_context_trust(
            subject.subject_ref,
            "code",
            estimate=101,
            confidence=50,
            evidence_ref="task:invalid",
        )


def test_relation_cli_edits_and_exports_the_full_local_model(
    tmp_path,
    capsys,
) -> None:
    home = tmp_path / "observer"
    assert (
        main(
            [
                "--home",
                str(home),
                "init",
                "--label",
                "observer",
                "--port",
                "48301",
            ]
        )
        == 0
    )
    capsys.readouterr()
    config = NodeConfig.load(home)
    observer = Identity.load(config.identity_path)
    first = Identity.generate("first")
    second = Identity.generate("second")
    book = RelationshipBook(
        config.relationships_path,
        own_actor_id=observer.node_id,
    )
    first_subject = book.observe_actor(
        first.card(),
        evidence_ref="packet:first",
        now=1_800_000_000_001,
    )
    book.observe_actor(
        second.card(),
        evidence_ref="packet:second",
        now=1_800_000_000_002,
    )

    assert (
        main(
            [
                "--home",
                str(home),
                "relation-link",
                second.node_id,
                first_subject.subject_ref,
                "--confidence",
                "82",
                "--evidence",
                "claim:same-controller",
            ]
        )
        == 0
    )
    linked = json.loads(capsys.readouterr().out)
    assert len(linked["actor_links"]) == 2

    assert (
        main(
            [
                "--home",
                str(home),
                "relation-circle",
                first_subject.subject_ref,
                "close",
                "--confidence",
                "74",
                "--evidence",
                "relationship:confirmed",
                "--label",
                "research-partner",
            ]
        )
        == 0
    )
    circle = json.loads(capsys.readouterr().out)
    assert circle["circle"] == "close"

    assert (
        main(
            [
                "--home",
                str(home),
                "relation-trust",
                first_subject.subject_ref,
                "code.review",
                "--estimate",
                "88",
                "--confidence",
                "76",
                "--evidence",
                "task:review-42",
            ]
        )
        == 0
    )
    trust = json.loads(capsys.readouterr().out)
    assert trust["context_trust"][0]["context"] == "code.review"

    assert main(["--home", str(home), "relation-list", "--model"]) == 0
    model = json.loads(capsys.readouterr().out)
    assert model["version"] == 3
    assert len(model["actors"]) == 2
    assert len(model["subjects"]) == 2
    assert model["events"][-1]["event_type"] == "relationship.context-trust-set"
