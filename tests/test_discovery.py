from __future__ import annotations

import time
from dataclasses import replace

import pytest

from anet.config import initialize_node
from anet.discovery import (
    DISCOVERY_SIGNAL_KIND,
    DiscoveryStore,
    build_discovery_signal,
    discovery_database_path,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


def test_discovery_signal_is_validated_at_anet_packet_boundary(tmp_path) -> None:
    sender_base = initialize_node(tmp_path / "sender", label="sender", listen_port=0)
    receiver_base = initialize_node(
        tmp_path / "receiver", label="receiver", listen_port=0
    )
    sender_config = replace(
        sender_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    receiver_config = replace(
        receiver_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    sender_identity = Identity.load(sender_config.identity_path)
    receiver_identity = Identity.load(receiver_config.identity_path)
    sender_card = sender_identity.card(addresses=())
    receiver_card = receiver_identity.card(addresses=())
    PeerBook(
        sender_config.peers_path,
        own_node_id=sender_identity.node_id,
    ).add(receiver_card)
    PeerBook(
        receiver_config.peers_path,
        own_node_id=receiver_identity.node_id,
    ).add(sender_card)

    receiver_store = DiscoveryStore(discovery_database_path(receiver_config.home))
    receiver_store.set_profile("default", topics=["research"])
    receiver_store.add_subscription(
        "research",
        profile_id="default",
        topics=["research"],
        min_score=40,
    )
    receiver_store.close()

    sender = AnetNode(sender_config)
    receiver = AnetNode(receiver_config)
    now = int(time.time() * 1000)
    signal = build_discovery_signal(
        published_ms=now,
        expires_ms=now + 3_600_000,
        intent="know",
        summary="A public protocol interoperability note.",
        topics=["research"],
        provenance={"source": "test", "adapter": "fixture"},
    )
    try:
        packet_id = sender.queue(
            receiver.node_id,
            kind=DISCOVERY_SIGNAL_KIND,
            body=signal,
        )
        raw = sender.store.get_packet(packet_id)
        assert raw is not None
        assert receiver.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=sender.node_id,
        ) == packet_id

        inbox = [
            item
            for item in receiver.store.list_inbox()
            if item["packet_id"] == packet_id
        ]
        assert inbox[0]["kind"] == DISCOVERY_SIGNAL_KIND
        assert inbox[0]["body"] == signal
        feed = DiscoveryStore(discovery_database_path(receiver_config.home))
        try:
            page = feed.feed("research")
            assert len(page["items"]) == 1
            assert page["items"][0]["signal_id"] == signal["signal_id"]
        finally:
            feed.close()

        malformed = {**signal, "summary": "tampered"}
        with pytest.raises(ValueError, match="digest"):
            sender.queue(
                receiver.node_id,
                kind=DISCOVERY_SIGNAL_KIND,
                body=malformed,
            )
    finally:
        sender.close()
        receiver.close()
