from __future__ import annotations

import json
import time

from anet.cli import main
from anet.config import initialize_node
from anet.identity import Identity
from anet.relation_activity import RelationshipActivityFeed
from anet.relationship_disclosures import (
    RelationshipDisclosure,
    RelationshipDisclosureBook,
)
from anet.relations import InteractionEvidence, RelationshipBook
from anet.reported_relationship_views import (
    ReportedRelationshipViewProjector,
)


NOW = int(time.time() * 1000) - 10_000


def _reported_book(tmp_path):
    observer = Identity.generate("observer-private-label")
    audience = Identity.generate("audience")
    observed = Identity.generate("observed-private-label")
    relations = RelationshipBook(
        tmp_path / "source-relationships.json",
        own_actor_id=observer.node_id,
    )
    subject = relations.observe_actor(
        observed.card(),
        evidence_ref="packet:private-actor-proof",
        now=NOW + 10,
    )
    relations.set_circle(
        subject.subject_ref,
        "friend",
        confidence=72,
        evidence_ref="relationship:private-circle-proof",
        now=NOW + 20,
    )
    relations.set_context_trust(
        subject.subject_ref,
        "skill.exchange",
        estimate=64,
        confidence=58,
        evidence_ref="relationship:private-trust-proof",
        now=NOW + 30,
    )
    relations.record_interaction(
        InteractionEvidence.create(
            actor_id=observed.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("message", "skill"),
            context="social.discord",
            outcome="received",
            evidence_ref="discord:private-message",
            occurred_ms=NOW + 40,
        )
    )
    disclosure = RelationshipDisclosure.create(
        RelationshipActivityFeed.read(relations.snapshot()),
        audience_actor_id=audience.node_id,
        now=NOW + 50,
    )
    received = RelationshipDisclosureBook(
        tmp_path / "relationship-disclosures.json",
        own_actor_id=audience.node_id,
    )
    received.add(
        disclosure,
        packet_id="11" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 60,
    )
    return observer, audience, observed, subject, disclosure, received


def test_reported_view_replays_sender_attributed_relationship_state(
    tmp_path,
) -> None:
    observer, audience, observed, subject, _disclosure, received = (
        _reported_book(tmp_path)
    )
    view = ReportedRelationshipViewProjector.project(
        received,
        sender_actor_id=observer.node_id,
        include_activities=True,
        now=NOW + 100,
    )

    assert view["observer_actor_id"] == observer.node_id
    assert view["audience_actor_id"] == audience.node_id
    assert view["viewpoint"] == "sender-reported"
    assert view["completeness"] == "partial-unknown"
    assert view["projection_into_local_relations"] is False
    assert view["authorization_effect"] == "none"
    assert "history-baseline-unknown" in view["warnings"]
    assert (
        "cross-packet-append-continuity-unproven"
        in view["warnings"]
    )

    projected = view["subjects"][0]
    assert projected["subject_ref"] == subject.subject_ref
    assert projected["reported_state"] == "active"
    assert projected["actor_links"][0]["actor_id"] == observed.node_id
    assert projected["reported_circle"]["circle"] == "friend"
    assert projected["reported_circle"]["confidence"] == 72
    assert projected["reported_context_trust"][0]["context"] == (
        "skill.exchange"
    )
    stats = projected["interaction_stats"]
    assert {(item["facet"], item["incoming"]) for item in stats} == {
        ("message", 1),
        ("skill", 1),
    }
    assert view["provenance"]["source_proof"] == (
        "authenticated-encrypted-packet"
    )
    assert view["provenance"]["age_since_receive_ms"] == 40
    rendered = json.dumps(view)
    assert "private-label" not in rendered
    assert "private-message" not in rendered
    assert "private-circle-proof" not in rendered


def test_reported_view_deduplicates_overlapping_activity_pages(
    tmp_path,
) -> None:
    observer, _audience, _observed, _subject, disclosure, received = (
        _reported_book(tmp_path)
    )
    repeated = RelationshipDisclosure.create(
        RelationshipActivityFeed.read(
            RelationshipBook(
                tmp_path / "source-relationships.json",
                own_actor_id=observer.node_id,
            ).snapshot()
        ),
        audience_actor_id=received.own_actor_id,
        now=NOW + 70,
    )
    assert repeated.disclosure_id != disclosure.disclosure_id
    received.add(
        repeated,
        packet_id="22" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 80,
    )

    view = ReportedRelationshipViewProjector.project(
        received,
        sender_actor_id=observer.node_id,
    )
    assert view["provenance"]["authenticated_disclosures"] == 2
    assert view["provenance"]["unique_activities"] == len(
        disclosure.activities
    )
    assert view["subjects"][0]["interaction_stats"][0]["incoming"] == 1


def test_reported_view_subject_filter_and_cli(tmp_path, capsys) -> None:
    config = initialize_node(
        tmp_path / "audience-home",
        label="audience",
        listen_port=0,
    )
    audience = Identity.load(config.identity_path)
    observer = Identity.generate("observer")
    observed = Identity.generate("observed")
    relations = RelationshipBook(
        tmp_path / "source.json",
        own_actor_id=observer.node_id,
    )
    subject = relations.observe_actor(
        observed.card(),
        evidence_ref="packet:source",
        now=NOW + 10,
    )
    disclosure = RelationshipDisclosure.create(
        RelationshipActivityFeed.read(relations.snapshot()),
        audience_actor_id=audience.node_id,
        now=NOW + 20,
    )
    RelationshipDisclosureBook(
        config.relationship_disclosures_path,
        own_actor_id=audience.node_id,
    ).add(
        disclosure,
        packet_id="33" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 30,
    )

    assert main(
        [
            "--home",
            str(config.home),
            "relation-reported-view",
            observer.node_id,
            "--subject",
            subject.subject_ref,
            "--include-activities",
            "--limit",
            "1",
        ]
    ) == 0
    value = json.loads(capsys.readouterr().out)
    assert len(value["subjects"]) == 1
    assert value["subjects"][0]["subject_ref"] == subject.subject_ref
    assert len(value["activities"]) == 1
    assert value["activities_truncated"] is False
    assert value["viewpoint"] == "sender-reported"


def test_v2_series_proves_continuity_despite_out_of_order_arrival(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    audience = Identity.generate("audience")
    observed = Identity.generate("observed")
    relations = RelationshipBook(
        tmp_path / "series-source.json",
        own_actor_id=observer.node_id,
    )
    subject = relations.observe_actor(
        observed.card(),
        evidence_ref="packet:actor",
        now=NOW + 10,
    )
    relations.set_circle(
        subject.subject_ref,
        "friend",
        confidence=70,
        evidence_ref="relationship:circle",
        now=NOW + 20,
    )
    first_page = RelationshipActivityFeed.read(
        relations.snapshot(),
        limit=1,
    )
    second_page = RelationshipActivityFeed.read(
        relations.snapshot(),
        after=first_page.next_cursor,
        limit=10,
    )
    series_id = "rdsr_" + ("a" * 32)
    first = RelationshipDisclosure.create_series(
        first_page,
        audience_actor_id=audience.node_id,
        series_id=series_id,
        sequence=0,
        starts_after="",
        baseline="history-start",
        now=NOW + 30,
    )
    second = RelationshipDisclosure.create_series(
        second_page,
        audience_actor_id=audience.node_id,
        series_id=series_id,
        sequence=1,
        starts_after=first.next_cursor,
        baseline="history-start",
        now=NOW + 40,
    )
    received = RelationshipDisclosureBook(
        tmp_path / "series-received.json",
        own_actor_id=audience.node_id,
    )
    # Transport arrival order is deliberately opposite to observer sequence.
    received.add(
        second,
        packet_id="44" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 50,
    )
    received.add(
        first,
        packet_id="55" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 60,
    )

    view = ReportedRelationshipViewProjector.project(
        received,
        sender_actor_id=observer.node_id,
    )
    assert view["selected_series_id"] == series_id
    assert view["completeness"] == "proven-continuous-segment"
    assert view["subjects"][0]["reported_circle"]["circle"] == "friend"
    series = view["provenance"]["series"][0]
    assert series["continuity"] == "proven-continuous"
    assert series["coverage"] == "history-through-cursor"
    assert series["first_sequence"] == 0
    assert series["last_sequence"] == 1
    assert series["issues"] == []
    assert "cross-packet-append-continuity-unproven" not in view["warnings"]
    assert "current-state-after-last-cursor-not-proven" in view["warnings"]


def test_v2_series_detects_missing_sequence(tmp_path) -> None:
    observer = Identity.generate("observer")
    audience = Identity.generate("audience")
    observed = Identity.generate("observed")
    relations = RelationshipBook(
        tmp_path / "gap-source.json",
        own_actor_id=observer.node_id,
    )
    relations.observe_actor(
        observed.card(),
        evidence_ref="packet:actor",
        now=NOW + 10,
    )
    page = RelationshipActivityFeed.read(relations.snapshot())
    missing_first = RelationshipDisclosure.create_series(
        page,
        audience_actor_id=audience.node_id,
        series_id="rdsr_" + ("b" * 32),
        sequence=1,
        starts_after=page.next_cursor,
        baseline="current-cursor",
        now=NOW + 20,
    )
    received = RelationshipDisclosureBook(
        tmp_path / "gap-received.json",
        own_actor_id=audience.node_id,
    )
    received.add(
        missing_first,
        packet_id="66" * 16,
        sender_actor_id=observer.node_id,
        received_ms=NOW + 30,
    )

    view = ReportedRelationshipViewProjector.project(
        received,
        sender_actor_id=observer.node_id,
    )
    assert view["completeness"] == "gap-detected"
    assert "missing-series-sequence" in view["warnings"]
    assert view["provenance"]["series"][0]["coverage"] == "discontinuous"
