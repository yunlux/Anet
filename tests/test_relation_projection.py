from __future__ import annotations

import json

from anet.agent_protocol import task_request, task_result
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from anet.relation_projection import RelationshipProjector, classify_interaction
from anet.relations import RelationshipBook


def _trust(source: AnetNode, destination: AnetNode) -> None:
    PeerBook(
        source.config.peers_path,
        own_node_id=source.node_id,
    ).add(destination.local_card)
    source.peers.reload()


def test_task_projection_uses_facets_without_copying_content() -> None:
    request = task_request(
        objective="private objective",
        required_capabilities=["files.read"],
        input={"secret": "do not persist"},
    )
    assert classify_interaction(
        "agent.task.request",
        request,
        direction="outgoing",
    ) == (("skill", "task"), "task", "submitted")

    result = task_result(
        task_id=request["task_id"],
        state="completed",
        output={"private": "artifact body"},
    )
    assert classify_interaction(
        "agent.task.result",
        result,
        direction="incoming",
    ) == (("artifact", "task"), "task", "completed")


def test_projector_is_idempotent_and_never_promotes_a_friendship(tmp_path) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    book = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    projector = RelationshipProjector(book)

    first = projector.project_packet(
        peer.card(),
        packet_id="a" * 32,
        kind="message",
        body={"text": "private"},
        direction="incoming",
        occurred_ms=1_800_000_000_001,
    )
    duplicate = projector.project_packet(
        peer.card(),
        packet_id="a" * 32,
        kind="message",
        body={"text": "changed but same packet"},
        direction="incoming",
        occurred_ms=1_800_000_000_001,
    )
    assert first is not None and first.recorded is True
    assert duplicate is not None and duplicate.recorded is False
    assert book.get(peer.node_id).circle == "known"
    assert len(book.snapshot()["interactions"]) == 1

    book.confirm_friend(
        peer.card(),
        evidence_ref="friend:explicit",
        now=1_800_000_000_002,
    )
    projector.project_packet(
        peer.card(),
        packet_id="b" * 32,
        kind="message",
        body="another private message",
        direction="incoming",
        occurred_ms=1_800_000_000_003,
    )
    assert book.get(peer.node_id).circle == "friend"
    serialized = json.dumps(book.snapshot(), ensure_ascii=False)
    assert "private" not in serialized
    assert "another private message" not in serialized


def test_projection_records_activity_without_reactivating_paused_or_ended_relation(
    tmp_path,
) -> None:
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
    projector = RelationshipProjector(book)

    book.pause_relationship(
        subject.subject_ref,
        evidence_ref="operator:relationship-inactive",
        now=1_800_000_000_002,
    )
    paused = projector.project_packet(
        peer.card(),
        packet_id="c" * 32,
        kind="file.share",
        body={"filename": "private-name.txt"},
        direction="incoming",
        occurred_ms=1_800_000_000_003,
    )
    assert paused is not None and paused.recorded is True
    relationship = book.relationship(subject.subject_ref)
    assert relationship is not None
    assert relationship.state == "dormant"
    assert relationship.circle == "public"
    assert book.snapshot()["interactions"][0]["facets"] == ["artifact", "message"]

    book.end_relationship(
        subject.subject_ref,
        evidence_ref="operator:relationship-ended",
        now=1_800_000_000_004,
    )
    ended = projector.project_packet(
        peer.card(),
        packet_id="d" * 32,
        kind="skill.offer",
        body={"private": "body"},
        direction="incoming",
        occurred_ms=1_800_000_000_005,
    )
    assert ended is not None and ended.recorded is True
    relationship = book.relationship(subject.subject_ref)
    assert relationship is not None
    assert relationship.state == "ended"
    assert relationship.circle == "public"
    assert len(book.snapshot()["interactions"]) == 2


def test_node_projects_sent_and_received_metadata_but_not_receipts(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="a"))
    b = AnetNode(initialize_node(tmp_path / "b", label="b"))
    try:
        _trust(a, b)
        _trust(b, a)
        packet_id = a.queue(
            b.node_id,
            kind="agent.task.request",
            body=task_request(
                objective="do not persist this objective",
                required_capabilities=["code.review"],
            ),
        )
        raw = a.store.get_packet(packet_id)
        assert raw is not None
        b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id)

        sent = RelationshipBook(
            a.config.relationships_path,
            own_actor_id=a.node_id,
        ).snapshot()
        received = RelationshipBook(
            b.config.relationships_path,
            own_actor_id=b.node_id,
        ).snapshot()
        assert len(sent["interactions"]) == 1
        assert len(received["interactions"]) == 1
        assert sent["interactions"][0]["direction"] == "outgoing"
        assert received["interactions"][0]["direction"] == "incoming"
        assert sent["interactions"][0]["facets"] == ["skill", "task"]
        assert sent["relationships"][0]["circle"] == "known"
        assert "do not persist this objective" not in json.dumps(sent)
    finally:
        a.close()
        b.close()


def test_relation_projection_failure_does_not_fail_packet_queue(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="a"))
    b = AnetNode(initialize_node(tmp_path / "b", label="b"))
    try:
        _trust(a, b)
        a.config.relationships_path.write_text("{broken", encoding="utf-8")
        packet_id = a.queue(b.node_id, kind="message", body="still queued")
        assert a.store.get_packet(packet_id) is not None
    finally:
        a.close()
        b.close()
