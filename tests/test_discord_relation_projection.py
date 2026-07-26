from __future__ import annotations

import json

from anet.actors import platform_actor_id
from anet.config import initialize_node
from anet.discord_relation_projection import DiscordRelationshipProjector
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import now_ms
from anet.peers import PeerBook
from anet.relations import RelationshipBook
from anet.social import (
    DISCORD_SIGNAL_KIND,
    SocialPolicy,
    build_discord_signal,
)


def _signal(*, actor_key: str, created_ms: int) -> dict:
    evaluation = SocialPolicy().evaluate(
        {
            "account_age_days": 900,
            "mention_count": 1,
            "reply_count": 0,
            "reaction_count": 2,
            "pinned_count": 0,
        },
        set(),
        {"interaction:mention", "platform:discord"},
    )
    return build_discord_signal(
        source_event_id="1" * 32,
        actor_key=actor_key,
        created_ms=created_ms,
        expires_ms=created_ms + 86_400_000,
        content_level="mention",
        content="private Discord mention",
        labels=("interaction:mention", "platform:discord"),
        evaluation=evaluation,
        guild_key="2" * 64,
        channel_key="3" * 64,
        message_revision="create",
    )


def _trust(source: AnetNode, destination: AnetNode) -> None:
    PeerBook(
        source.config.peers_path,
        own_node_id=source.node_id,
    ).add(destination.local_card)
    source.peers.reload()


def test_local_discord_actor_is_typed_private_and_idempotent(tmp_path) -> None:
    observer = Identity.generate("observer")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    projector = DiscordRelationshipProjector(book)
    event = {
        "actor_key": "a" * 64,
        "event_key": "b" * 32,
        "created_ms": 1_800_000_000_001,
        "event_labels": [
            "content:attachment",
            "interaction:mention",
            "platform:discord",
        ],
        "content": "must never enter relationships.json",
    }

    first = projector.project_local_event(event)
    before = book.snapshot()
    duplicate = projector.project_local_event(event)
    assert first.recorded is True
    assert duplicate.recorded is False
    assert book.snapshot() == before

    actor = before["actors"][0]
    assert actor["actor_kind"] == "account.discord"
    assert actor["proofs"] == [
        {
            "proof_type": "discord.rest.v10",
            "scope": "platform-observed",
            "issuer_actor_id": observer.node_id,
            "evidence_ref": "discord-event:" + "b" * 32,
            "observed_ms": 1_800_000_000_001,
        }
    ]
    assert before["relationships"][0]["circle"] == "known"
    assert before["relationships"][0]["context_trust"] == []
    assert before["interactions"][0]["facets"] == ["artifact", "message"]
    serialized = json.dumps(before)
    assert "a" * 64 not in serialized
    assert "must never enter" not in serialized


def test_bridge_attestation_does_not_transfer_bridge_relationship(tmp_path) -> None:
    observer = Identity.generate("observer")
    bridge = Identity.generate("bridge")
    other_bridge = Identity.generate("other-bridge")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    bridge_subject = book.observe_actor(
        bridge.card(),
        evidence_ref="packet:bridge",
        now=1_800_000_000_001,
    )
    book.set_circle(
        bridge_subject.subject_ref,
        "family",
        confidence=95,
        evidence_ref="relationship:bridge-family",
        now=1_800_000_000_002,
    )
    book.set_context_trust(
        bridge_subject.subject_ref,
        "code",
        estimate=90,
        confidence=80,
        evidence_ref="task:bridge-code",
        now=1_800_000_000_003,
    )
    projector = DiscordRelationshipProjector(book)
    signal = _signal(actor_key="c" * 64, created_ms=1_800_000_000_004)
    projected = projector.project_signal(bridge.card(), signal)

    actor = next(
        item for item in book.snapshot()["actors"]
        if item["actor_id"] == projected.actor_id
    )
    relationship = book.relationship(projected.subject_ref)
    assert actor["proofs"][0]["scope"] == "bridge-attested"
    assert actor["proofs"][0]["issuer_actor_id"] == bridge.node_id
    assert relationship is not None
    assert relationship.circle == "known"
    assert relationship.context_trust == ()
    assert platform_actor_id(
        "discord",
        namespace_actor_id=bridge.node_id,
        platform_actor_key="c" * 64,
    ) != platform_actor_id(
        "discord",
        namespace_actor_id=other_bridge.node_id,
        platform_actor_key="c" * 64,
    )


def test_undirected_discord_observation_remains_public(tmp_path) -> None:
    observer = Identity.generate("observer")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    projected = DiscordRelationshipProjector(book).project_local_event(
        {
            "actor_key": "e" * 64,
            "event_key": "f" * 32,
            "created_ms": 1_800_000_000_001,
            "event_labels": ["platform:discord"],
            "evaluation": {
                "action": "connect_candidate",
                "reputation": {"score": 100, "confidence": 100},
            },
        }
    )
    relationship = book.relationship(projected.subject_ref)
    assert relationship is not None
    assert relationship.circle == "public"
    assert relationship.relationship_confidence == 0
    assert relationship.context_trust == ()


def test_node_projects_signed_discord_signal_as_separate_actor(tmp_path) -> None:
    bridge = AnetNode(initialize_node(tmp_path / "bridge", label="bridge"))
    observer = AnetNode(initialize_node(tmp_path / "observer", label="observer"))
    try:
        _trust(bridge, observer)
        _trust(observer, bridge)
        created_ms = now_ms()
        signal = _signal(actor_key="d" * 64, created_ms=created_ms)
        packet_id = bridge.queue(
            observer.node_id,
            kind=DISCORD_SIGNAL_KIND,
            body=signal,
        )
        raw = bridge.store.get_packet(packet_id)
        assert raw is not None
        observer.accept_carrier_packet(raw, depth=1, peer_id=bridge.node_id)

        snapshot = RelationshipBook(
            observer.config.relationships_path,
            own_actor_id=observer.node_id,
        ).snapshot()
        assert {item["actor_kind"] for item in snapshot["actors"]} == {
            "anet.node",
            "account.discord",
        }
        discord_actor = next(
            item for item in snapshot["actors"]
            if item["actor_kind"] == "account.discord"
        )
        assert discord_actor["proofs"][0]["scope"] == "bridge-attested"
        assert discord_actor["proofs"][0]["issuer_actor_id"] == bridge.node_id
        discord_subject = next(
            item for item in snapshot["subjects"]
            if any(
                link["actor_id"] == discord_actor["actor_id"]
                for link in item["actor_links"]
            )
        )
        discord_relation = next(
            item for item in snapshot["relationships"]
            if item["subject_ref"] == discord_subject["subject_ref"]
        )
        assert discord_relation["circle"] == "known"
        assert discord_relation["context_trust"] == []
        serialized = json.dumps(snapshot)
        assert "d" * 64 not in serialized
        assert "private Discord mention" not in serialized
    finally:
        bridge.close()
        observer.close()
