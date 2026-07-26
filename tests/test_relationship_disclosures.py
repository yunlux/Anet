from __future__ import annotations

import json

import pytest

from anet.cli import main
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from anet.relation_activity import RelationshipActivityFeed
from anet.relationship_disclosures import (
    RELATIONSHIP_DISCLOSURE_KIND,
    RelationshipDisclosure,
    RelationshipDisclosureBook,
    validate_relationship_disclosure,
)
from anet.relations import InteractionEvidence, RelationshipBook


def _source_page(path, observer: Identity, observed: Identity):
    book = RelationshipBook(path, own_actor_id=observer.node_id)
    subject = book.observe_actor(
        observed.card(),
        evidence_ref="packet:private-actor-evidence",
        now=1_800_000_000_100,
    )
    book.record_interaction(
        InteractionEvidence.create(
            actor_id=observed.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("message", "skill"),
            context="social.discord",
            outcome="received",
            evidence_ref="discord:private-message-reference",
            occurred_ms=1_800_000_000_200,
        )
    )
    return book, RelationshipActivityFeed.read(book.snapshot())


def test_disclosure_is_audience_bound_content_free_and_digest_checked(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    audience = Identity.generate("audience")
    observed = Identity.generate("private-observed-label")
    _book, page = _source_page(
        tmp_path / "relationships.json",
        observer,
        observed,
    )

    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=audience.node_id,
        now=1_800_000_000_300,
    )
    rendered = json.dumps(disclosure.to_dict())

    assert len(disclosure.activities) == 2
    assert disclosure.observer_actor_id == observer.node_id
    assert disclosure.audience_actor_id == audience.node_id
    assert disclosure.to_dict()["visibility"] == "audience-private"
    assert "private-message-reference" not in rendered
    assert "private-actor-evidence" not in rendered
    assert "private-observed-label" not in rendered
    assert "authorization_effect" in rendered

    validated = validate_relationship_disclosure(
        disclosure.to_dict(),
        sender_node_id=observer.node_id,
        destination_node_id=audience.node_id,
        now=1_800_000_000_300,
    )
    assert validated == disclosure.to_dict()

    wrong_audience = dict(disclosure.to_dict())
    wrong_audience["audience_actor_id"] = observed.node_id
    with pytest.raises(ValueError, match="digest"):
        RelationshipDisclosure.from_dict(wrong_audience)

    with pytest.raises(ValueError, match="destination"):
        disclosure.validate_binding(
            sender_node_id=observer.node_id,
            destination_node_id=observed.node_id,
            now=1_800_000_000_300,
        )


def test_disclosure_rejects_raw_or_unknown_activity_details(tmp_path) -> None:
    observer = Identity.generate("observer")
    audience = Identity.generate("audience")
    observed = Identity.generate("observed")
    _book, page = _source_page(
        tmp_path / "relationships.json",
        observer,
        observed,
    )
    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=audience.node_id,
        now=1_800_000_000_300,
    ).to_dict()
    disclosure["activities"][0]["details"]["text"] = "raw content"

    with pytest.raises(ValueError, match="details"):
        RelationshipDisclosure.from_dict(disclosure)

    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=audience.node_id,
        now=1_800_000_000_300,
    ).to_dict()
    disclosure["issued_ms"] = True
    with pytest.raises(ValueError, match="boundary"):
        RelationshipDisclosure.from_dict(disclosure)


def test_trusted_packet_projects_into_separate_disclosure_book(
    tmp_path,
) -> None:
    a_config = initialize_node(
        tmp_path / "a",
        label="a",
        listen_port=0,
    )
    b_config = initialize_node(
        tmp_path / "b",
        label="b",
        listen_port=0,
    )
    observed = Identity.generate("observed")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_identity.card())
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_identity.card())
    source, page = _source_page(
        a_config.relationships_path,
        a_identity,
        observed,
    )
    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=b_identity.node_id,
    )
    a = AnetNode(a_config)
    b = AnetNode(b_config)
    try:
        packet_id = a.queue(
            b.node_id,
            kind=RELATIONSHIP_DISCLOSURE_KIND,
            body=disclosure.to_dict(),
        )
        raw = a.store.get_packet(packet_id)
        assert raw is not None
        b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id)
        # Redelivery is idempotent at the Packet and observation-ledger seams.
        assert b.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=a.node_id,
        ) == packet_id
    finally:
        a.close()
        b.close()

    received = RelationshipDisclosureBook(
        b_config.relationship_disclosures_path,
        own_actor_id=b_identity.node_id,
    ).all()
    assert len(received) == 1
    assert received[0].packet_id == packet_id
    assert received[0].sender_actor_id == a_identity.node_id
    assert (
        received[0].to_dict()["source_proof"]
        == "authenticated-encrypted-packet"
    )
    # A disclosure is an observed remote view, never imported as B's own
    # Actor, Subject, circle, contextual trust, or authorization.
    b_relations = RelationshipBook(
        b_config.relationships_path,
        own_actor_id=b_identity.node_id,
    ).snapshot()
    assert b_relations["actors"] == []
    assert b_relations["subjects"] == []
    assert b_relations["relationships"] == []
    assert source.snapshot()["actors"]


def test_disclosure_projection_survives_crash_before_inbox_commit(
    tmp_path,
    monkeypatch,
) -> None:
    a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
    b_config = initialize_node(tmp_path / "b", label="b", listen_port=0)
    observed = Identity.generate("observed")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_identity.card())
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_identity.card())
    _source, page = _source_page(
        a_config.relationships_path,
        a_identity,
        observed,
    )
    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=b_identity.node_id,
    )
    a = AnetNode(a_config)
    first_b = AnetNode(b_config)
    try:
        packet_id = a.queue(
            b_identity.node_id,
            kind=RELATIONSHIP_DISCLOSURE_KIND,
            body=disclosure.to_dict(),
        )
        raw = a.store.get_packet(packet_id)
        assert raw is not None

        def crash_before_commit(*_args, **_kwargs):
            raise RuntimeError("simulated crash before Inbox commit")

        monkeypatch.setattr(
            first_b.store,
            "commit_local_message",
            crash_before_commit,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            first_b.accept_carrier_packet(
                raw,
                depth=1,
                peer_id=a.node_id,
            )
    finally:
        a.close()
        first_b.close()

    projected = RelationshipDisclosureBook(
        b_config.relationship_disclosures_path,
        own_actor_id=b_identity.node_id,
    )
    assert len(projected.all()) == 1

    restarted_b = AnetNode(b_config)
    try:
        assert restarted_b.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=a_identity.node_id,
        ) == packet_id
        assert len(restarted_b.store.list_inbox()) == 1
    finally:
        restarted_b.close()
    assert (
        len(
            RelationshipDisclosureBook(
                b_config.relationship_disclosures_path,
                own_actor_id=b_identity.node_id,
            ).all()
        )
        == 1
    )


def test_queue_rejects_disclosure_for_another_audience(tmp_path) -> None:
    a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
    b_config = initialize_node(tmp_path / "b", label="b", listen_port=0)
    c = Identity.generate("c")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_identity.card())
    _source, page = _source_page(
        a_config.relationships_path,
        a_identity,
        c,
    )
    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=c.node_id,
    )
    node = AnetNode(a_config)
    try:
        with pytest.raises(ValueError, match="destination"):
            node.queue(
                b_identity.node_id,
                kind=RELATIONSHIP_DISCLOSURE_KIND,
                body=disclosure.to_dict(),
            )
    finally:
        node.close()


def test_relation_disclose_cli_queues_and_lists_received(
    tmp_path,
    capsys,
) -> None:
    a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
    b_config = initialize_node(tmp_path / "b", label="b", listen_port=0)
    observed = Identity.generate("observed")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_identity.card())
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_identity.card())
    _source_page(
        a_config.relationships_path,
        a_identity,
        observed,
    )

    assert main(
        [
            "--home",
            str(a_config.home),
            "relation-disclose",
            b_identity.node_id,
            "--limit",
            "1",
        ]
    ) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["activities"] == 1
    assert queued["privacy"] == "content-free"
    assert queued["authorization_effect"] == "none"

    sender = AnetNode(a_config)
    receiver = AnetNode(b_config)
    try:
        raw = sender.store.get_packet(queued["queued"])
        assert raw is not None
        receiver.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=sender.node_id,
        )
    finally:
        sender.close()
        receiver.close()

    assert main(
        [
            "--home",
            str(b_config.home),
            "relation-disclosure-list",
            "--sender",
            a_identity.node_id,
        ]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["received"]) == 1
    assert listed["projection_into_local_relations"] is False
    assert listed["authorization_effect"] == "none"
