from __future__ import annotations

import os
from pathlib import Path

from anet.bundle import BUNDLE_MAGIC, LEGACY_BUNDLE_MAGIC, create_bundle, import_bundle
from anet.config import NodeConfig, initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from anet.packet import inspect_packet, open_packet, seal_packet
from anet.store import PacketStore


def test_packet_store_hardens_posix_database_permissions(tmp_path) -> None:
    store = PacketStore(tmp_path / "private" / "store.sqlite3")
    try:
        if os.name != "nt":
            assert (store.path.parent.stat().st_mode & 0o777) == 0o700
            assert (store.path.stat().st_mode & 0o777) == 0o600
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{store.path}{suffix}")
                if sidecar.exists():
                    assert (sidecar.stat().st_mode & 0o777) == 0o600
    finally:
        store.close()


def test_store_deduplicates_and_bundle_round_trips(tmp_path) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = seal_packet(sender, recipient.card(), kind="task", body={"objective": "test"})
    packet_id = inspect_packet(raw).packet_id

    source = PacketStore(tmp_path / "source.sqlite3")
    target = PacketStore(tmp_path / "target.sqlite3")
    try:
        assert source.add_packet(raw) is True
        assert source.add_packet(raw) is False
        bundle_path = tmp_path / "carry.anet"
        result = create_bundle(source, bundle_path, destination_id=recipient.node_id)
        assert result["packets"] == 1

        imported = import_bundle(target, bundle_path)
        assert imported["imported"] == 1
        assert target.has_packet(packet_id)
        imported_again = import_bundle(target, bundle_path)
        assert imported_again["duplicates"] == 1
    finally:
        source.close()
        target.close()


def test_import_accepts_pre_rename_bundle_magic(tmp_path) -> None:
    source = PacketStore(tmp_path / "source.sqlite3")
    target = PacketStore(tmp_path / "target.sqlite3")
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    try:
        raw = seal_packet(sender, recipient.card(), kind="message", body="legacy")
        source.add_packet(raw)
        bundle_path = tmp_path / "legacy.anet"
        create_bundle(source, bundle_path)
        bundle_path.write_bytes(
            bundle_path.read_bytes().replace(BUNDLE_MAGIC, LEGACY_BUNDLE_MAGIC, 1)
        )
        result = import_bundle(target, bundle_path)
        assert result["imported"] == 1
    finally:
        source.close()
        target.close()


def test_node_config_migrates_pre_rename_database(tmp_path) -> None:
    config = initialize_node(tmp_path / "node", label="node")
    store = PacketStore(config.database_path)
    store.close()
    legacy_path = config.home / "ainet.sqlite3"
    config.database_path.replace(legacy_path)

    loaded = NodeConfig.load(config.home)
    assert loaded.database_path.name == "anet.sqlite3"
    assert loaded.database_path.exists()
    assert not legacy_path.exists()


def test_store_custody_ack_is_per_peer(tmp_path) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    relay = Identity.generate("relay")
    raw = seal_packet(sender, recipient.card(), kind="message", body="payload")
    packet_id = inspect_packet(raw).packet_id
    store = PacketStore(tmp_path / "store.sqlite3")
    try:
        store.add_packet(raw)
        assert [item["packet_id"] for item in store.pending_for_peer(relay.node_id)] == [packet_id]
        store.mark_attempt([packet_id], relay.node_id)
        store.mark_acked([packet_id], relay.node_id)
        assert store.pending_for_peer(relay.node_id) == []
        assert store.status()["pending"] == 1
        assert [item["packet_id"] for item in store.pending_for_peer(recipient.node_id)] == [packet_id]
        store.mark_acked([packet_id], recipient.node_id)
        assert store.status()["pending"] == 0
    finally:
        store.close()


def test_bundle_import_at_destination_decrypts_local_message(tmp_path) -> None:
    sender_config = initialize_node(tmp_path / "sender", label="sender", listen_port=45201)
    recipient_config = initialize_node(tmp_path / "recipient", label="recipient", listen_port=45202)
    sender = Identity.load(sender_config.identity_path)
    recipient = Identity.load(recipient_config.identity_path)
    sender_card = sender.card(addresses=sender_config.effective_addresses(), capabilities=sender_config.capabilities)
    recipient_card = recipient.card(
        addresses=recipient_config.effective_addresses(),
        capabilities=recipient_config.capabilities,
    )
    PeerBook(sender_config.peers_path, own_node_id=sender.node_id).add(recipient_card)
    PeerBook(recipient_config.peers_path, own_node_id=recipient.node_id).add(sender_card)

    source_node = AnetNode(sender_config)
    destination_node = AnetNode(recipient_config)
    try:
        packet_id = source_node.queue(recipient.node_id, kind="message", body="offline")
        bundle = tmp_path / "offline.anet"
        create_bundle(source_node.store, bundle, destination_id=recipient.node_id)
        import_bundle(destination_node.store, bundle)
        assert destination_node.process_local_spool() == 1
        message = destination_node.store.list_inbox()[0]
        assert message["packet_id"] == packet_id
        assert message["body"] == "offline"
        assert destination_node.store.status()["pending"] == 1  # the generated return receipt
    finally:
        source_node.close()
        destination_node.close()


def test_purge_removes_old_transient_inbox_and_orphan_receipts(tmp_path) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = seal_packet(sender, recipient.card(), kind="network.probe", body={"payload": b""})
    message = open_packet(recipient, raw)
    store = PacketStore(tmp_path / "maintenance.sqlite3")
    try:
        assert store.add_inbox(message, trusted=True, visible=False) is True
        store.record_receipt("f" * 32, recipient.node_id)
        with store._lock:  # Test-only aging of otherwise immutable receive time.
            store._conn.execute("UPDATE inbox SET received_ms = 0 WHERE packet_id = ?", (message.packet_id,))
        result = store.purge(transient_retention_ms=60_000)
        assert result["old_transient_inbox"] == 1
        assert result["orphan_receipts"] == 1
        assert store.status()["transient"] == 0
        assert store.status()["receipts"] == 0
    finally:
        store.close()
