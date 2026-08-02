from __future__ import annotations

import json

import pytest

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relations import InteractionEvidence, RelationshipBook


def test_cli_observes_an_opaque_operator_attested_external_actor(
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
                "48601",
            ]
        )
        == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    actor_id = "act_local_" + "a" * 32
    command = [
        "--home",
        str(home),
        "relation-observe-actor",
        actor_id,
        "--kind",
        "human.local",
        "--label",
        "local human observer",
        "--confidence",
        "35",
        "--evidence",
        "operator:relationship-bootstrap",
    ]
    assert main(command) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["actor"]["actor_id"] == actor_id
    assert observed["actor"]["actor_kind"] == "human.local"
    assert observed["actor"]["proofs"][0]["scope"] == "operator-attested"
    assert observed["subject"]["actor_links"][0]["confidence"] == 35
    assert observed["relationship"]["circle"] == "public"
    assert observed["identity_assertion"] == "none"
    assert observed["authorization_effect"] == "none"

    assert main(command) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["subject"]["subject_ref"] == observed["subject"]["subject_ref"]
    model_home = NodeConfig.load(home)
    snapshot = RelationshipBook(
        model_home.relationships_path,
        own_actor_id=initialized["node_id"],
    ).snapshot()
    assert [item["event_type"] for item in snapshot["events"]] == ["actor.observed"]
    assert NodeConfig.load(home).peers_path.exists()
    assert json.loads(NodeConfig.load(home).peers_path.read_text(encoding="utf-8"))["peers"] == []

    pause = [
        "--home",
        str(home),
        "relation-pause",
        observed["subject"]["subject_ref"],
        "--confirm",
        observed["subject"]["subject_ref"],
        "--reason",
        "operator:relationship-inactive",
    ]
    assert main(pause) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["relationship"]["state"] == "dormant"
    assert paused["already_paused"] is False
    assert paused["subject_changed"] is False
    assert paused["actors_changed"] is False
    assert paused["claims_changed"] is False
    assert paused["trust_changed"] is False
    assert paused["peerbook_changed"] is False
    assert paused["authorization_effect"] == "none"

    assert main(pause) == 0
    repeated_pause = json.loads(capsys.readouterr().out)
    assert repeated_pause["already_paused"] is True

    end = [
        "--home",
        str(home),
        "relation-end",
        observed["subject"]["subject_ref"],
        "--confirm",
        observed["subject"]["subject_ref"],
        "--reason",
        "operator:relationship-ended",
    ]
    assert main(end) == 0
    ended = json.loads(capsys.readouterr().out)
    assert ended["relationship"]["state"] == "ended"
    assert ended["relationship"]["circle"] == "public"
    assert ended["subject_changed"] is False
    assert ended["actors_changed"] is False
    assert ended["claims_changed"] is False
    assert ended["trust_changed"] is False
    assert ended["peerbook_changed"] is False
    assert ended["authorization_effect"] == "none"

    assert main(end) == 0
    repeated_end = json.loads(capsys.readouterr().out)
    assert repeated_end["already_ended"] is True

    revoke = [
        "--home",
        str(home),
        "relation-actor-revoke",
        actor_id,
        "--confirm",
        actor_id,
        "--reason",
        "operator:source-retired",
    ]
    assert main(revoke) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["actor"]["state"] == "revoked"
    assert revoked["relationship"]["subject_ref"] == observed["subject"]["subject_ref"]
    assert revoked["relationship"]["circle"] == "public"
    assert revoked["subject_changed"] is False
    assert revoked["peerbook_changed"] is False
    assert revoked["authorization_effect"] == "none"

    assert main(revoke) == 0
    repeated_revoke = json.loads(capsys.readouterr().out)
    assert repeated_revoke["already_revoked"] is True
    snapshot = RelationshipBook(
        model_home.relationships_path,
        own_actor_id=initialized["node_id"],
    ).snapshot()
    assert [item["event_type"] for item in snapshot["events"]] == [
        "actor.observed",
        "relationship.paused",
        "relationship.ended",
        "actor.revoked",
    ]


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


def test_ended_relationship_keeps_local_history_and_circle_reopens_it(tmp_path) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    book = RelationshipBook(path, own_actor_id=observer.node_id)
    subject = book.observe_actor(
        peer.card(),
        evidence_ref="packet:peer",
        now=1_800_000_000_001,
    )
    book.set_circle(
        subject.subject_ref,
        "close",
        confidence=72,
        evidence_ref="relationship:confirmed",
        labels=("research-partner",),
        now=1_800_000_000_002,
    )
    trusted = book.set_context_trust(
        subject.subject_ref,
        "code.review",
        estimate=88,
        confidence=76,
        evidence_ref="task:review-42",
        now=1_800_000_000_003,
    )

    paused = book.pause_relationship(
        subject.subject_ref,
        evidence_ref="operator:relationship-inactive",
        now=1_800_000_000_004,
    )
    assert paused.state == "dormant"
    assert paused.circle == "close"
    assert paused.context_trust == trusted.context_trust
    assert (
        book.pause_relationship(
            subject.subject_ref,
            evidence_ref="operator:relationship-inactive",
            now=1_800_000_000_005,
        )
        == paused
    )

    ended = book.end_relationship(
        subject.subject_ref,
        evidence_ref="operator:relationship-ended",
        now=1_800_000_000_006,
    )
    assert ended.state == "ended"
    assert ended.circle == "close"
    assert ended.context_trust == trusted.context_trust
    assert book.subject(subject.subject_ref) is not None
    assert book.actor(peer.node_id) is not None

    assert book.end_relationship(
        subject.subject_ref,
        evidence_ref="operator:relationship-ended",
        now=1_800_000_000_007,
    ) == ended
    with pytest.raises(ValueError, match="cannot pause an ended relationship"):
        book.pause_relationship(
            subject.subject_ref,
            evidence_ref="operator:relationship-inactive",
            now=1_800_000_000_008,
        )
    reopened = book.set_circle(
        subject.subject_ref,
        "known",
        confidence=60,
        evidence_ref="operator:relationship-reopened",
        now=1_800_000_000_009,
    )
    assert reopened.state == "active"
    assert reopened.circle == "known"
    assert [item["event_type"] for item in book.snapshot()["events"]] == [
        "actor.observed",
        "relationship.circle-set",
        "relationship.context-trust-set",
        "relationship.paused",
        "relationship.ended",
        "relationship.circle-set",
    ]


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
    assert snapshot["version"] == 7
    assert snapshot["actors"][0]["actor_id"] == peer.node_id
    assert snapshot["actors"][0]["actor_kind"] == "anet.node"
    assert snapshot["actors"][0]["proofs"][0]["scope"] == "cryptographic"
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
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 7


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
    assert snapshot["version"] == 7
    assert snapshot["interactions"] == []
    assert snapshot["interaction_stats"] == []
    assert migrated.get(peer.node_id) is not None


def test_version_three_relationship_book_preserves_interaction_evidence(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    original = RelationshipBook(path, own_actor_id=observer.node_id)
    subject = original.observe_actor(
        peer.card(),
        evidence_ref="packet:legacy-v3",
        now=1_800_000_000_001,
    )
    original.record_interaction(
        InteractionEvidence.create(
            actor_id=peer.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("message",),
            context="communication",
            outcome="received",
            evidence_ref="packet:" + "c" * 32,
            occurred_ms=1_800_000_000_002,
        )
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = 3
    value.pop("subject_transitions", None)
    path.write_text(json.dumps(value), encoding="utf-8")

    migrated = RelationshipBook(path, own_actor_id=observer.node_id)
    snapshot = migrated.snapshot()
    assert snapshot["version"] == 7
    assert len(snapshot["interactions"]) == 1
    assert snapshot["subject_transitions"] == []


def test_version_four_relationship_book_adds_node_proof_without_new_claims(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    original = RelationshipBook(path, own_actor_id=observer.node_id)
    original.observe_actor(
        peer.card(),
        evidence_ref="packet:legacy-v4",
        now=1_800_000_000_001,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = 4
    for actor in value["actors"]:
        actor.pop("actor_kind")
        actor.pop("proofs")
    path.write_text(json.dumps(value), encoding="utf-8")

    migrated = RelationshipBook(path, own_actor_id=observer.node_id)
    actor = migrated.snapshot()["actors"][0]
    assert actor["actor_kind"] == "anet.node"
    assert actor["proofs"][0]["proof_type"] == "anet.peer-card"
    assert actor["proofs"][0]["scope"] == "cryptographic"
    assert migrated.snapshot()["events"] == value["events"]


def test_version_five_relationship_book_loads_without_inventing_decisions(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    original = RelationshipBook(path, own_actor_id=observer.node_id)
    original.observe_actor(
        peer.card(),
        evidence_ref="packet:legacy-v5",
        now=1_800_000_000_001,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = 5
    value.pop("suggestion_decisions", None)
    path.write_text(json.dumps(value), encoding="utf-8")

    migrated = RelationshipBook(path, own_actor_id=observer.node_id)
    snapshot = migrated.snapshot()
    assert snapshot["version"] == 7
    assert snapshot["suggestion_decisions"] == []
    assert snapshot["actors"] == value["actors"]
    assert snapshot["subjects"] == value["subjects"]
    assert snapshot["relationships"] == value["relationships"]
    assert snapshot["events"] == value["events"]


def test_version_six_relationship_book_loads_events_without_details(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    path = tmp_path / "relationships.json"
    original = RelationshipBook(path, own_actor_id=observer.node_id)
    subject = original.observe_actor(
        peer.card(),
        evidence_ref="packet:legacy-v6",
        now=1_800_000_000_001,
    )
    original.set_circle(
        subject.subject_ref,
        "known",
        confidence=44,
        evidence_ref="relationship:legacy-v6",
        now=1_800_000_000_002,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = 6
    for event in value["events"]:
        event.pop("details", None)
    path.write_text(json.dumps(value), encoding="utf-8")

    migrated = RelationshipBook(path, own_actor_id=observer.node_id)
    snapshot = migrated.snapshot()
    assert snapshot["version"] == 7
    assert all(event["details"] == {} for event in snapshot["events"])
    assert snapshot["relationships"] == value["relationships"]


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


def test_subject_transition_lineage_preserves_history_without_multiplying_trust(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    first = Identity.generate("first")
    second = Identity.generate("second")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
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
    book.set_circle(
        first_subject.subject_ref,
        "close",
        confidence=80,
        evidence_ref="relationship:first-close",
        now=1_800_000_000_003,
    )
    book.set_context_trust(
        first_subject.subject_ref,
        "code.review",
        estimate=90,
        confidence=75,
        evidence_ref="task:review",
        now=1_800_000_000_004,
    )
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=first.node_id,
            subject_ref=first_subject.subject_ref,
            direction="incoming",
            facets=("task",),
            context="task",
            outcome="completed",
            evidence_ref="packet:" + "a" * 32,
            occurred_ms=1_800_000_000_005,
        )
    )

    merged = book.merge_subjects(
        (first_subject.subject_ref, second_subject.subject_ref),
        confidence=78,
        evidence_ref="claim:same-subject",
        inherit_subject_ref=first_subject.subject_ref,
        now=1_800_000_000_006,
    )
    merged_ref = merged.replacement_subject_refs[0]
    assert book.subject(first_subject.subject_ref).state == "superseded"
    assert book.relationship(first_subject.subject_ref).state == "dormant"
    assert book.relationship(merged_ref).circle == "close"
    assert book.relationship(merged_ref).context_trust[0].estimate == 90

    split = book.split_subject(
        merged_ref,
        ((first.node_id,), (second.node_id,)),
        confidence=83,
        evidence_ref="claim:controllers-diverged",
        inherit_group=0,
        now=1_800_000_000_007,
    )
    inherited_ref, fresh_ref = split.replacement_subject_refs
    assert book.relationship(inherited_ref).circle == "close"
    assert book.relationship(fresh_ref).circle == "known"
    assert book.relationship(fresh_ref).context_trust == ()
    assert book.primary_subject(first.node_id).subject_ref == inherited_ref
    assert book.primary_subject(second.node_id).subject_ref == fresh_ref

    superseded = book.supersede_subject(
        fresh_ref,
        confidence=88,
        evidence_ref="claim:revised-explanation",
        labels=("controller:unknown",),
        now=1_800_000_000_008,
    )
    revised_ref = superseded.replacement_subject_refs[0]
    assert book.relationship(revised_ref).circle == "known"
    assert book.subject(revised_ref).labels == ("controller:unknown",)

    snapshot = book.snapshot()
    assert [
        item["transition_type"] for item in snapshot["subject_transitions"]
    ] == ["merge", "split", "supersede"]
    assert snapshot["interactions"][0]["subject_ref"] == first_subject.subject_ref
    assert snapshot["interaction_stats"][0]["subject_ref"] == (
        first_subject.subject_ref
    )
    assert RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    ).snapshot() == snapshot


def test_merge_without_explicit_inheritance_starts_known_and_split_is_atomic(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    first = Identity.generate("first")
    second = Identity.generate("second")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
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
    book.set_circle(
        first_subject.subject_ref,
        "family",
        confidence=95,
        evidence_ref="relationship:family",
        now=1_800_000_000_003,
    )
    transition = book.merge_subjects(
        (first_subject.subject_ref, second_subject.subject_ref),
        confidence=60,
        evidence_ref="claim:possible-merge",
        now=1_800_000_000_004,
    )
    merged_ref = transition.replacement_subject_refs[0]
    assert book.relationship(merged_ref).circle == "known"
    assert book.relationship(merged_ref).context_trust == ()

    before = book.snapshot()
    with pytest.raises(ValueError, match="exactly partition"):
        book.split_subject(
            merged_ref,
            ((first.node_id,), ("an1missing",)),
            confidence=50,
            evidence_ref="claim:invalid-split",
            now=1_800_000_000_005,
        )
    assert book.snapshot() == before


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
    assert model["version"] == 7
    assert len(model["actors"]) == 2
    assert len(model["subjects"]) == 2
    assert model["events"][-1]["event_type"] == "relationship.context-trust-set"


def test_subject_transition_cli_exposes_explicit_inheritance(tmp_path, capsys) -> None:
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
                "48302",
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
    second_subject = book.observe_actor(
        second.card(),
        evidence_ref="packet:second",
        now=1_800_000_000_002,
    )
    book.set_circle(
        first_subject.subject_ref,
        "friend",
        confidence=90,
        evidence_ref="friend:explicit",
        now=1_800_000_000_003,
    )

    assert (
        main(
            [
                "--home",
                str(home),
                "subject-merge",
                first_subject.subject_ref,
                second_subject.subject_ref,
                "--confidence",
                "80",
                "--evidence",
                "claim:same-subject",
                "--inherit",
                first_subject.subject_ref,
            ]
        )
        == 0
    )
    merged = json.loads(capsys.readouterr().out)
    merged_ref = merged["transition"]["replacement_subject_refs"][0]
    assert merged["replacements"][0]["relationship"]["circle"] == "friend"

    assert (
        main(
            [
                "--home",
                str(home),
                "subject-split",
                merged_ref,
                "--group",
                first.node_id,
                "--group",
                second.node_id,
                "--confidence",
                "85",
                "--evidence",
                "claim:split",
                "--inherit-group",
                "1",
            ]
        )
        == 0
    )
    split = json.loads(capsys.readouterr().out)
    assert [
        item["relationship"]["circle"] for item in split["replacements"]
    ] == ["friend", "known"]

    second_replacement = split["transition"]["replacement_subject_refs"][1]
    assert (
        main(
            [
                "--home",
                str(home),
                "subject-supersede",
                second_replacement,
                "--confidence",
                "70",
                "--evidence",
                "claim:revision",
                "--label",
                "controller:unknown",
            ]
        )
        == 0
    )
    superseded = json.loads(capsys.readouterr().out)
    assert superseded["transition"]["transition_type"] == "supersede"
    assert superseded["replacements"][0]["subject"]["labels"] == [
        "controller:unknown"
    ]
