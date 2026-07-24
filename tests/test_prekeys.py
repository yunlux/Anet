from __future__ import annotations

import asyncio
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidSignature, InvalidTag

from anet.config import NodeConfig, initialize_node
from anet.carriers.directory import sync_directory_once
from anet.encoding import canonical_pack, pack, unpack
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import (
    MAX_CLOCK_SKEW_MS,
    MAX_TTL_SECONDS,
    inspect_packet,
    now_ms,
    open_packet,
    seal_packet,
)
from anet.peers import PeerBook
from anet.prekeys import (
    PreKeyBundle,
    generate_prekey_bundle,
    import_prekey_bundle,
)
from anet.store import PacketStore


def _trust(node: AnetNode, other: AnetNode) -> None:
    book = PeerBook(node.config.peers_path, own_node_id=node.node_id)
    book.add(other.local_card)
    node.peers.reload()


async def _wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition was not reached before timeout")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_signed_prekey_bundle_round_trip_and_tamper_detection(tmp_path) -> None:
    identity = Identity.generate("recipient")
    consumer = Identity.generate("consumer")
    store = PacketStore(tmp_path / "recipient.db")
    bundle = generate_prekey_bundle(
        identity, store, peer_id=consumer.node_id, count=3
    )
    bundle.verify(identity.card(), recipient_node_id=consumer.node_id)
    assert bundle.intended_peer_id == consumer.node_id
    assert store.prekey_status()["local"]["counts"] == {"available": 3}

    path = tmp_path / "recipient.prekeys.json"
    bundle.save(path)
    loaded = PreKeyBundle.load(path)
    loaded.verify(identity.card(), recipient_node_id=consumer.node_id)
    assert loaded.bundle_hash == bundle.bundle_hash

    with pytest.raises(InvalidSignature):
        replace(loaded, expires_ms=loaded.expires_ms + 1).verify(
            identity.card(), recipient_node_id=consumer.node_id
        )
    with pytest.raises(ValueError, match="different peer"):
        loaded.verify(Identity.generate("other").card())
    store.close()


def test_peer_bundle_import_is_idempotent_and_rejects_rollback(tmp_path) -> None:
    identity = Identity.generate("recipient")
    consumer = Identity.generate("consumer")
    source = PacketStore(tmp_path / "source.db")
    target = PacketStore(tmp_path / "target.db")
    first = generate_prekey_bundle(
        identity, source, peer_id=consumer.node_id, count=2
    )
    second = generate_prekey_bundle(
        identity, source, peer_id=consumer.node_id, count=2
    )

    result = import_prekey_bundle(
        first,
        identity.card(),
        target,
        recipient_node_id=consumer.node_id,
    )
    assert result["inserted"] == 2
    duplicate = import_prekey_bundle(
        first,
        identity.card(),
        target,
        recipient_node_id=consumer.node_id,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["inserted"] == 0
    import_prekey_bundle(
        second,
        identity.card(),
        target,
        recipient_node_id=consumer.node_id,
    )
    with pytest.raises(ValueError, match="rollback"):
        import_prekey_bundle(
            first,
            identity.card(),
            target,
            recipient_node_id=consumer.node_id,
        )

    status = target.prekey_status(identity.node_id)
    assert status["peers"][identity.node_id]["counts"] == {"available": 4}
    source.close()
    target.close()


def test_same_generation_with_different_hash_is_equivocation(tmp_path) -> None:
    identity = Identity.generate("recipient")
    consumer = Identity.generate("consumer")
    source = PacketStore(tmp_path / "source.db")
    target = PacketStore(tmp_path / "target.db")
    bundle = generate_prekey_bundle(
        identity, source, peer_id=consumer.node_id, count=1
    )
    import_prekey_bundle(
        bundle,
        identity.card(),
        target,
        recipient_node_id=consumer.node_id,
    )

    with pytest.raises(ValueError, match="equivocation"):
        target.import_peer_prekey_bundle(
            identity.node_id,
            [
                {
                    "prekey_id": key.prekey_id,
                    "public_key": key.public_key,
                }
                for key in bundle.keys
            ],
            bundle_version=2,
            intended_peer_id=consumer.node_id,
            generation=bundle.generation,
            bundle_hash="f" * 64,
            created_ms=bundle.created_ms,
            expires_ms=bundle.expires_ms,
        )
    source.close()
    target.close()


def test_peer_prekey_reservation_is_atomic_across_connections(tmp_path) -> None:
    identity = Identity.generate("recipient")
    consumer = Identity.generate("consumer")
    source = PacketStore(tmp_path / "source.db")
    database = tmp_path / "target.db"
    target = PacketStore(database)
    bundle = generate_prekey_bundle(
        identity, source, peer_id=consumer.node_id, count=12
    )
    import_prekey_bundle(
        bundle,
        identity.card(),
        target,
        recipient_node_id=consumer.node_id,
    )
    target.close()

    def reserve(_: int) -> dict[str, object] | None:
        store = PacketStore(database)
        try:
            return store.reserve_peer_prekey(identity.node_id)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(reserve, range(20)))
    claimed = [item for item in reservations if item is not None]
    assert len(claimed) == 12
    assert len({str(item["prekey_id"]) for item in claimed}) == 12

    verifier = PacketStore(database)
    status = verifier.prekey_status(identity.node_id)
    assert status["peers"][identity.node_id]["counts"] == {"reserved": 12}
    verifier.close()
    source.close()


def test_node_uses_and_erases_one_time_prekey_with_duplicate_safe_ack(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="a"))
    b = AnetNode(initialize_node(tmp_path / "b", label="b"))
    try:
        _trust(a, b)
        _trust(b, a)
        b_bundle = generate_prekey_bundle(
            b.identity, b.store, peer_id=a.node_id, count=2
        )
        a_bundle = generate_prekey_bundle(
            a.identity, a.store, peer_id=b.node_id, count=2
        )
        import_prekey_bundle(
            b_bundle,
            b.local_card,
            a.store,
            recipient_node_id=a.node_id,
        )
        import_prekey_bundle(
            a_bundle,
            a.local_card,
            b.store,
            recipient_node_id=b.node_id,
        )

        packet_id = a.queue(b.node_id, kind="message", body="erase-after-open")
        raw = a.store.get_packet(packet_id)
        assert raw is not None
        info = inspect_packet(raw)
        assert info.key_mode == "opk"
        assert b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id) == packet_id
        # A replay is acknowledged from durable state without needing the erased key.
        assert b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id) == packet_id
        assert [item["body"] for item in b.store.list_inbox()] == [
            "erase-after-open"
        ]
        assert b.store.local_prekey_material(info.prekey_id) is None
        with pytest.raises(ValueError, match="requires a one-time prekey"):
            open_packet(b.identity, raw)

        with sqlite3.connect(b.config.database_path) as connection:
            private_key, state, bound_packet = connection.execute(
                """
                SELECT private_key, state, packet_id FROM local_prekeys
                WHERE prekey_id = ?
                """,
                (info.prekey_id,),
            ).fetchone()
        assert private_key is None
        assert state == "consumed"
        assert bound_packet == packet_id

        receipts = b.store.export_packets(destination_id=a.node_id)
        assert len(receipts) == 1
        assert inspect_packet(receipts[0]).key_mode == "opk"
    finally:
        a.close()
        b.close()


def test_reusing_one_time_prekey_for_a_second_packet_is_rejected(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="a"))
    b = AnetNode(initialize_node(tmp_path / "b", label="b"))
    try:
        _trust(a, b)
        _trust(b, a)
        bundle = generate_prekey_bundle(
            b.identity, b.store, peer_id=a.node_id, count=1
        )
        key = bundle.keys[0]
        first = seal_packet(
            a.identity,
            b.local_card,
            kind="message",
            body="first",
            recipient_prekey_public=key.public_key,
            recipient_prekey_id=key.prekey_id,
        )
        second = seal_packet(
            a.identity,
            b.local_card,
            kind="message",
            body="second",
            recipient_prekey_public=key.public_key,
            recipient_prekey_id=key.prekey_id,
        )
        b.accept_carrier_packet(first, depth=1, peer_id=a.node_id)
        with pytest.raises(ValueError, match="unavailable"):
            b.accept_carrier_packet(second, depth=1, peer_id=a.node_id)
        assert [item["body"] for item in b.store.list_inbox()] == ["first"]
    finally:
        a.close()
        b.close()


def test_prekey_survives_crash_before_inbox_commit(tmp_path) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    a = AnetNode(a_config)
    b = AnetNode(b_config)
    _trust(a, b)
    _trust(b, a)
    bundle = generate_prekey_bundle(
        b.identity, b.store, peer_id=a.node_id, count=1
    )
    import_prekey_bundle(
        bundle,
        b.local_card,
        a.store,
        recipient_node_id=a.node_id,
    )
    packet_id = a.queue(b.node_id, kind="message", body="recover-after-crash")
    raw = a.store.get_packet(packet_id)
    assert raw is not None
    info = inspect_packet(raw)
    b.store.add_packet(raw, depth=1, origin="relay", received_from=a.node_id)
    a.close()
    b.close()

    recovered = AnetNode(b_config)
    try:
        assert recovered.store.local_prekey_material(info.prekey_id) is not None
        assert recovered.process_local_spool() == 1
        assert recovered.store.local_prekey_material(info.prekey_id) is None
        assert recovered.store.list_inbox()[0]["body"] == "recover-after-crash"
    finally:
        recovered.close()


def test_expired_local_prekey_is_erased_only_after_inflight_packet_window(
    tmp_path,
) -> None:
    identity = Identity.generate("recipient")
    consumer = Identity.generate("consumer")
    store = PacketStore(tmp_path / "prekeys.db")
    current = now_ms()
    within_window = generate_prekey_bundle(
        identity,
        store,
        peer_id=consumer.node_id,
        count=1,
        ttl_ms=60_000,
        created_ms=current - 86400 * 1000 - 60_000,
    )
    beyond_window = generate_prekey_bundle(
        identity,
        store,
        peer_id=consumer.node_id,
        count=1,
        ttl_ms=60_000,
        created_ms=(
            current
            - MAX_TTL_SECONDS * 1000
            - MAX_CLOCK_SKEW_MS
            - 120_000
        ),
    )
    result = store.purge()
    assert result["expired_local_prekeys"] == 1
    assert (
        store.local_prekey_material(within_window.keys[0].prekey_id) is not None
    )
    assert store.local_prekey_material(beyond_window.keys[0].prekey_id) is None
    store.close()


def test_require_policy_refuses_missing_inventory_and_legacy_peer(tmp_path) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    replace(a_config, prekey_policy="require").save()
    a = AnetNode(NodeConfig.load(a_config.home))
    b = AnetNode(b_config)
    try:
        _trust(a, b)
        legacy_material = generate_prekey_bundle(
            b.identity,
            b.store,
            peer_id=a.node_id,
            count=1,
        )
        a.store.import_peer_prekey_bundle(
            b.node_id,
            [
                {
                    "prekey_id": key.prekey_id,
                    "public_key": key.public_key,
                }
                for key in legacy_material.keys
            ],
            bundle_version=1,
            intended_peer_id="",
            generation=legacy_material.generation,
            bundle_hash=legacy_material.bundle_hash,
            created_ms=legacy_material.created_ms,
            expires_ms=legacy_material.expires_ms,
        )
        assert a.store.peer_prekey_inventory(
            b.node_id, min_bundle_version=1
        )["available"] == 1
        assert a.store.peer_prekey_inventory(
            b.node_id, min_bundle_version=2
        )["available"] == 0
        with pytest.raises(RuntimeError, match="no unexpired"):
            a.queue(b.node_id, kind="message", body="must-not-downgrade")

        legacy_card = b.identity.card(
            addresses=b.config.effective_addresses(),
            capabilities=("agent-message-v0",),
        )
        book = PeerBook(a.config.peers_path, own_node_id=a.node_id)
        book.add(legacy_card)
        a.peers.reload()
        with pytest.raises(RuntimeError, match="does not advertise"):
            a.queue(b.node_id, kind="message", body="legacy")
    finally:
        a.close()
        b.close()


def test_peer_scoped_prekey_rejects_a_different_trusted_sender(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="allowed"))
    b = AnetNode(initialize_node(tmp_path / "b", label="recipient"))
    c = AnetNode(initialize_node(tmp_path / "c", label="other"))
    try:
        _trust(b, a)
        _trust(b, c)
        bundle = generate_prekey_bundle(
            b.identity,
            b.store,
            peer_id=a.node_id,
            count=1,
        )
        with pytest.raises(ValueError, match="intended for a different"):
            bundle.verify(b.local_card, recipient_node_id=c.node_id)
        key = bundle.keys[0]
        stolen_use = seal_packet(
            c.identity,
            b.local_card,
            kind="message",
            body="wrong-sender",
            recipient_prekey_public=key.public_key,
            recipient_prekey_id=key.prekey_id,
        )
        with pytest.raises(ValueError, match="not authorized"):
            b.accept_carrier_packet(stolen_use, depth=1, peer_id=c.node_id)
        stolen_id = inspect_packet(stolen_use).packet_id
        rejection = b.store.packet_rejection(stolen_id)
        assert rejection is not None
        assert "not authorized" in rejection["reason"]
        assert b.process_local_spool() == 0
        assert b.store.local_prekey_material(key.prekey_id) is not None

        authorized_use = seal_packet(
            a.identity,
            b.local_card,
            kind="message",
            body="allowed-sender",
            recipient_prekey_public=key.public_key,
            recipient_prekey_id=key.prekey_id,
        )
        b.accept_carrier_packet(authorized_use, depth=1, peer_id=a.node_id)
        assert b.store.list_inbox()[0]["body"] == "allowed-sender"
        assert b.store.local_prekey_material(key.prekey_id) is None
    finally:
        a.close()
        b.close()
        c.close()


def test_automatic_in_band_replenishment_recovers_from_zero_stock(tmp_path) -> None:
    async def scenario() -> None:
        a_config = initialize_node(
            tmp_path / "a", label="a", listen_port=_free_port()
        )
        b_config = initialize_node(
            tmp_path / "b", label="b", listen_port=_free_port()
        )
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(
            b_identity.card(
                addresses=b_config.effective_addresses(),
                capabilities=b_config.capabilities,
            )
        )
        PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(
            a_identity.card(
                addresses=a_config.effective_addresses(),
                capabilities=a_config.capabilities,
            )
        )
        for config in (a_config, b_config):
            replace(
                config,
                prekey_policy="require",
                prekey_auto_enabled=True,
                prekey_low_watermark=2,
                prekey_batch_size=4,
                prekey_request_interval=30,
                sync_interval=0.2,
                sync_jitter=0,
            ).save()
        a = AnetNode(NodeConfig.load(a_config.home))
        b = AnetNode(NodeConfig.load(b_config.home))
        try:
            assert a.store.peer_prekey_inventory(
                b.node_id, min_bundle_version=2
            )["available"] == 0
            await a.start()
            await b.start()
            await _wait_until(
                lambda: a.store.peer_prekey_inventory(
                    b.node_id, min_bundle_version=2
                )["available"]
                >= 4
                and b.store.peer_prekey_inventory(
                    a.node_id, min_bundle_version=2
                )["available"]
                >= 4
            )
            assert a.store.list_inbox() == []
            assert b.store.list_inbox() == []
            packet_id = a.queue(
                b.node_id,
                kind="message",
                body="zero-stock-recovered",
            )
            assert inspect_packet(a.store.get_packet(packet_id)).key_mode == "opk"
            await _wait_until(
                lambda: any(
                    item["packet_id"] == packet_id for item in b.store.list_inbox()
                )
            )
            await _wait_until(
                lambda: a.store.status()["pending"] == 0
                and b.store.status()["pending"] == 0
            )
            assert b.store.list_inbox()[0]["body"] == "zero-stock-recovered"
            assert (
                a.store.prekey_status()["peers"][b.node_id]["bundle_version"]
                == 2
            )
            for index in range(2):
                followup = a.queue(
                    b.node_id,
                    kind="message",
                    body=f"trigger-replenishment-{index}",
                )
                await _wait_until(
                    lambda packet_id=followup: any(
                        item["packet_id"] == packet_id
                        for item in b.store.list_inbox()
                    )
                )
            await _wait_until(
                lambda: a.store.peer_prekey_inventory(
                    b.node_id, min_bundle_version=2
                )["generation"]
                >= 2
                and b.store.peer_prekey_inventory(
                    a.node_id, min_bundle_version=2
                )["generation"]
                >= 2
            )
            assert a.store.peer_prekey_inventory(
                b.node_id, min_bundle_version=2
            )["available"] >= 3
            assert b.store.peer_prekey_inventory(
                a.node_id, min_bundle_version=2
            )["available"] >= 3
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_prekey_maintenance_does_not_queue_a_request_storm(tmp_path) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(
        b_identity.card(capabilities=b_config.capabilities)
    )
    replace(
        a_config,
        prekey_auto_enabled=True,
        prekey_low_watermark=8,
        prekey_batch_size=8,
        prekey_request_interval=3600,
    ).save()
    a = AnetNode(NodeConfig.load(a_config.home))
    try:
        first = a.maintain_prekeys()
        assert len(first) == 1
        assert first[0]["key_mode"] == "static"
        assert a.maintain_prekeys() == []
        assert a.maintain_prekeys() == []
        with sqlite3.connect(a.config.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM packets WHERE origin = 'prekey-control'"
            ).fetchone()[0]
        assert count == 1
    finally:
        a.close()


def test_single_peer_upgrade_scopes_v1_inventory_and_preserves_generation(
    tmp_path,
) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(
        b_identity.card(capabilities=b_config.capabilities)
    )
    store = PacketStore(a_config.database_path)
    generate_prekey_bundle(
        a_identity,
        store,
        peer_id=b_identity.node_id,
        count=2,
    )
    store.close()
    with sqlite3.connect(a_config.database_path) as connection:
        connection.execute(
            "UPDATE local_prekeys SET peer_id = '', bundle_version = 1"
        )
        connection.execute(
            "DELETE FROM store_metadata WHERE key = ?",
            (f"local_prekey_generation:{b_identity.node_id}",),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO store_metadata(key, integer_value)
            VALUES ('local_prekey_generation', 1)
            """
        )
    upgraded = AnetNode(NodeConfig.load(a_config.home))
    try:
        assert upgraded.store.unscoped_local_prekey_count() == 0
        assert upgraded.store.next_local_prekey_generation(b_identity.node_id) == 2
        local = upgraded.store.prekey_status()["local"]
        assert local["by_peer"][b_identity.node_id]["counts"] == {
            "available": 2
        }
    finally:
        upgraded.close()


def test_duplicate_replenishment_request_reuses_generation_and_one_response(
    tmp_path,
) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    replace(a_config, prekey_batch_size=3).save()
    replace(b_config, prekey_batch_size=3).save()
    a = AnetNode(NodeConfig.load(a_config.home))
    b = AnetNode(NodeConfig.load(b_config.home))
    try:
        _trust(a, b)
        _trust(b, a)
        first = a.request_prekey_replenishment(b.node_id, force=True)
        second = a.request_prekey_replenishment(b.node_id, force=True)
        assert first["request_id"] == second["request_id"]
        requests = a.store.export_packets(destination_id=b.node_id)
        assert len(requests) == 2
        for raw in requests:
            b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id)
        assert b.store.next_local_prekey_generation(a.node_id) == 2
        responses = b.store.export_packets(destination_id=a.node_id)
        assert len(responses) == 1
        a.accept_carrier_packet(responses[0], depth=1, peer_id=b.node_id)
        assert a.store.peer_prekey_inventory(
            b.node_id, min_bundle_version=2
        ) == {"available": 3, "generation": 1}
    finally:
        a.close()
        b.close()


def test_v037_database_schema_migrates_before_peer_scoping(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE local_prekeys (
                prekey_id TEXT PRIMARY KEY,
                public_key BLOB NOT NULL,
                private_key BLOB,
                generation INTEGER NOT NULL,
                created_ms INTEGER NOT NULL,
                expires_ms INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'available',
                consumed_ms INTEGER NOT NULL DEFAULT 0,
                packet_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO local_prekeys VALUES (
                ?, ?, ?, 3, 1000, 9999999999999, 'available', 0, ''
            )
            """,
            ("ab" * 16, b"p" * 32, b"s" * 32),
        )
    store = PacketStore(database)
    try:
        with sqlite3.connect(database) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(local_prekeys)")
            }
        assert {"peer_id", "bundle_version"} <= columns
        assert store.unscoped_local_prekey_count() == 1
        peer = Identity.generate("peer")
        result = store.scope_legacy_local_prekeys(peer.node_id)
        assert result["scoped"] == 1
        material = store.local_prekey_material("ab" * 16)
        assert material is not None
        assert material["peer_id"] == peer.node_id
        assert material["bundle_version"] == 1
        assert store.next_local_prekey_generation(peer.node_id) == 4
    finally:
        store.close()


def test_v1_prekey_bundle_and_v037_peer_card_remain_readable(tmp_path) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    replace(a_config, prekey_policy="require").save()
    a = AnetNode(NodeConfig.load(a_config.home))
    b = AnetNode(b_config)
    try:
        legacy_b_card = b.identity.card(
            capabilities=("one-time-prekeys-v1",)
        )
        PeerBook(a.config.peers_path, own_node_id=a.node_id).add(legacy_b_card)
        a.peers.reload()
        _trust(b, a)
        current = generate_prekey_bundle(
            b.identity,
            b.store,
            peer_id=a.node_id,
            count=1,
        )
        unsigned_legacy = PreKeyBundle(
            version=1,
            node_id=b.node_id,
            intended_peer_id="",
            generation=current.generation,
            created_ms=current.created_ms,
            expires_ms=current.expires_ms,
            keys=current.keys,
            signature=b"",
        )
        legacy = replace(
            unsigned_legacy,
            signature=b.identity.sign(
                canonical_pack(unsigned_legacy.signing_fields())
            ),
        )
        import_prekey_bundle(
            legacy,
            legacy_b_card,
            a.store,
            recipient_node_id=a.node_id,
        )
        packet_id = a.queue(b.node_id, kind="message", body="v037-compatible")
        raw = a.store.get_packet(packet_id)
        assert raw is not None
        assert inspect_packet(raw).key_mode == "opk"
        b.accept_carrier_packet(raw, depth=1, peer_id=a.node_id)
        assert b.store.list_inbox()[0]["body"] == "v037-compatible"
    finally:
        a.close()
        b.close()


def test_multi_peer_upgrade_retires_ambiguous_v1_private_keys(tmp_path) -> None:
    owner = Identity.generate("owner")
    peer_a = Identity.generate("peer-a")
    peer_b = Identity.generate("peer-b")
    store = PacketStore(tmp_path / "multi.db")
    bundle = generate_prekey_bundle(
        owner,
        store,
        peer_id=peer_a.node_id,
        count=2,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE local_prekeys SET peer_id = '', bundle_version = 1"
        )
        connection.execute("DELETE FROM store_metadata")
    result = store.retire_unscoped_local_prekeys(
        [peer_a.node_id, peer_b.node_id]
    )
    try:
        assert result["retired"] == 2
        assert store.unscoped_local_prekey_count() == 0
        assert store.next_local_prekey_generation(peer_a.node_id) == 2
        assert store.next_local_prekey_generation(peer_b.node_id) == 2
        assert store.local_prekey_material(bundle.keys[0].prekey_id) is None
        with sqlite3.connect(store.path) as connection:
            states = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT state FROM local_prekeys"
                )
            }
        assert states == {"retired-unscoped"}
    finally:
        store.close()


def test_deterministic_ciphertext_failure_is_not_retried_forever(tmp_path) -> None:
    a = AnetNode(initialize_node(tmp_path / "a", label="a"))
    b = AnetNode(initialize_node(tmp_path / "b", label="b"))
    try:
        _trust(b, a)
        raw = seal_packet(
            a.identity,
            b.local_card,
            kind="message",
            body="tamper",
        )
        outer = unpack(raw)
        ciphertext = bytearray(outer["ct"])
        ciphertext[-1] ^= 1
        outer["ct"] = bytes(ciphertext)
        tampered = pack(outer)
        packet_id = inspect_packet(tampered).packet_id
        with pytest.raises(InvalidTag):
            b.accept_carrier_packet(tampered, depth=1, peer_id=a.node_id)
        assert b.store.packet_rejection(packet_id) is not None
        assert b.store.status()["rejections"] == 1
        assert b.process_local_spool() == 0
    finally:
        a.close()
        b.close()


def test_zero_stock_replenishment_and_message_work_over_directory_only(
    tmp_path,
) -> None:
    a_config = initialize_node(tmp_path / "a", label="a")
    b_config = initialize_node(tmp_path / "b", label="b")
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(
        b_identity.card(addresses=(), capabilities=b_config.capabilities)
    )
    PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(
        a_identity.card(addresses=(), capabilities=a_config.capabilities)
    )
    for config in (a_config, b_config):
        replace(
            config,
            listen_enabled=False,
            direct_enabled=False,
            prekey_policy="require",
            prekey_auto_enabled=True,
            prekey_low_watermark=2,
            prekey_batch_size=4,
            prekey_request_interval=30,
        ).save()
    a = AnetNode(NodeConfig.load(a_config.home))
    b = AnetNode(NodeConfig.load(b_config.home))
    drop = tmp_path / "drop"
    try:
        assert len(a.maintain_prekeys()) == 1
        assert len(b.maintain_prekeys()) == 1
        for _ in range(8):
            sync_directory_once(a, drop, retry_after_ms=0)
            sync_directory_once(b, drop, retry_after_ms=0)
            if (
                a.store.peer_prekey_inventory(
                    b.node_id, min_bundle_version=2
                )["available"]
                >= 4
                and b.store.peer_prekey_inventory(
                    a.node_id, min_bundle_version=2
                )["available"]
                >= 4
            ):
                break
        # Simultaneous zero-stock requests can consume one newly imported key
        # while returning the peer's control bundle. The useful invariant is
        # that replenishment leaves enough inventory for the ordinary message.
        assert a.store.peer_prekey_inventory(
            b.node_id, min_bundle_version=2
        )["available"] >= 3
        assert b.store.peer_prekey_inventory(
            a.node_id, min_bundle_version=2
        )["available"] >= 3

        packet_id = a.queue(
            b.node_id,
            kind="message",
            body="directory-only-opk",
        )
        for _ in range(8):
            sync_directory_once(a, drop, retry_after_ms=0)
            sync_directory_once(b, drop, retry_after_ms=0)
            if any(
                item["packet_id"] == packet_id for item in b.store.list_inbox()
            ):
                break
        message = next(
            item for item in b.store.list_inbox() if item["packet_id"] == packet_id
        )
        assert message["body"] == "directory-only-opk"
        assert inspect_packet(a.store.get_packet(packet_id)).key_mode == "opk"
    finally:
        a.close()
        b.close()
