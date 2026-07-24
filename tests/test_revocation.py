from __future__ import annotations

import json
import os
import sqlite3

import pytest

from anet.cli import main
from anet.identity import Identity
from anet.packet import OpenedMessage, seal_packet
from anet.peers import PeerBook
from anet.prekeys import generate_prekey_bundle, import_prekey_bundle
from anet.store import PacketStore


def _message(sender: Identity) -> OpenedMessage:
    return OpenedMessage(
        packet_id="a" * 32,
        sender_id=sender.node_id,
        sender_sign_public=sender.sign_public,
        sender_box_public=sender.box_public,
        kind="agent.task",
        created_ms=1_000,
        body={"task": "must stop after revocation"},
        causal=(),
        codec="application/msgpack",
        reply_to="",
        qos="control",
    )


def test_peer_revocation_is_private_fail_closed_and_immediate(tmp_path) -> None:
    local = Identity.generate("local")
    peer = Identity.generate("peer")
    path = tmp_path / "peers.json"
    live = PeerBook(path, own_node_id=local.node_id)
    live.add(peer.card())

    admin = PeerBook(path, own_node_id=local.node_id)
    record = admin.revoke(peer.node_id, reason="device lost")
    assert record["node_id"] == peer.node_id
    assert record["reason"] == "device lost"
    assert len(record["key_fingerprint"]) == 64

    # The already-running trust view reloads at the next trust boundary.
    with pytest.raises(KeyError, match="unknown peer"):
        live.require(peer.node_id)
    assert not live.is_trusted(
        peer.node_id,
        peer.sign_public,
        peer.box_public,
    )

    # Simulate a crash after the deny record was committed but before the old
    # positive peer file was replaced. The deny ledger must still win.
    path.write_text(
        json.dumps({"version": 1, "peers": [peer.card().to_dict()]}),
        encoding="utf-8",
    )
    recovered = PeerBook(path, own_node_id=local.node_id)
    assert recovered.all() == []
    with pytest.raises(ValueError, match="locally revoked"):
        recovered.add(peer.card())

    if os.name == "posix":
        assert recovered.revocations_path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o600


def test_store_revocation_retires_peer_work_keys_claims_and_routes(tmp_path) -> None:
    local = Identity.generate("local")
    peer = Identity.generate("peer")
    store = PacketStore(tmp_path / "local.sqlite3")
    peer_store = PacketStore(tmp_path / "peer.sqlite3")
    try:
        local_bundle = generate_prekey_bundle(
            local,
            store,
            peer_id=peer.node_id,
            count=2,
        )
        peer_bundle = generate_prekey_bundle(
            peer,
            peer_store,
            peer_id=local.node_id,
            count=2,
        )
        imported = import_prekey_bundle(
            peer_bundle,
            peer.card(),
            store,
            recipient_node_id=local.node_id,
        )
        assert imported["inserted"] == 2

        raw = seal_packet(
            local,
            peer.card(),
            kind="agent.task",
            body={"task": "queued before revocation"},
        )
        assert store.add_packet(raw)
        assert store.status()["pending"] == 1

        store.add_inbox(_message(peer), trusted=True)
        store.open_consumer_group("workers", start="earliest")
        claim = store.claim_consumer_messages("workers", "worker-1")[0]
        store.record_path_result(
            peer.node_id,
            "direct",
            success=True,
            latency_ms=1.0,
        )
        store.set_route(peer.node_id, "direct", "healthy")

        cleanup = store.revoke_peer(peer.node_id)
        assert cleanup == {
            "peer_id": peer.node_id,
            "expired_pending_packets": 1,
            "reclassified_inbox": 1,
            "revoked_consumer_deliveries": 1,
            "retired_local_prekeys": 2,
            "retired_peer_prekeys": 2,
            "removed_routes": 1,
            "removed_path_metrics": 1,
        }
        assert store.status()["pending"] == 0
        assert store.list_inbox()[0]["trusted"] is False
        assert store.consumer_group_status("workers")["states"] == {"revoked": 1}
        with pytest.raises(ValueError, match="stale"):
            store.acknowledge_claim("workers", "worker-1", claim["claim_token"])
        for key in local_bundle.keys:
            assert store.local_prekey_material(key.prekey_id) is None
        prekeys = store.prekey_status(peer.node_id)
        assert prekeys["local"]["by_peer"][peer.node_id]["counts"] == {
            "revoked": 2
        }
        assert prekeys["peers"][peer.node_id]["counts"] == {"revoked": 2}
        assert store.route(peer.node_id) is None
        assert store.path_metric(peer.node_id, "direct") is None

        repeated = store.revoke_peer(peer.node_id)
        assert repeated["peer_id"] == peer.node_id
        assert all(
            value == 0
            for key, value in repeated.items()
            if key != "peer_id"
        )
    finally:
        peer_store.close()
        store.close()


def test_store_revocation_rolls_back_all_cleanup_on_failure(tmp_path) -> None:
    local = Identity.generate("local")
    peer = Identity.generate("peer")
    store = PacketStore(tmp_path / "atomic.sqlite3")
    try:
        local_bundle = generate_prekey_bundle(
            local,
            store,
            peer_id=peer.node_id,
            count=1,
        )
        raw = seal_packet(
            local,
            peer.card(),
            kind="agent.task",
            body={"task": "must remain queued after rollback"},
        )
        store.add_packet(raw)
        store.add_inbox(_message(peer), trusted=True)
        with store._lock:
            store._conn.execute(
                """
                CREATE TRIGGER force_revoke_failure
                BEFORE UPDATE OF state ON local_prekeys
                WHEN NEW.state = 'revoked'
                BEGIN
                    SELECT RAISE(ABORT, 'forced revoke failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced revoke failure"):
            store.revoke_peer(peer.node_id)

        assert store.status()["pending"] == 1
        assert store.list_inbox()[0]["trusted"] is True
        assert store.local_prekey_material(local_bundle.keys[0].prekey_id) is not None
    finally:
        store.close()


def test_cli_revocation_requires_exact_confirmation_and_blocks_retrust(
    tmp_path,
    capsys,
) -> None:
    local_home = tmp_path / "local"
    peer_home = tmp_path / "peer"
    peer_card = tmp_path / "peer.card.json"
    assert main(["--home", str(local_home), "init", "--label", "local"]) == 0
    local_init = json.loads(capsys.readouterr().out)
    assert main(["--home", str(peer_home), "init", "--label", "peer"]) == 0
    peer_init = json.loads(capsys.readouterr().out)
    assert main(["--home", str(peer_home), "card", "--out", str(peer_card)]) == 0
    capsys.readouterr()
    assert main(["--home", str(local_home), "peer-add", str(peer_card)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--home",
                str(local_home),
                "peer-revoke",
                peer_init["node_id"],
                "--confirm",
                peer_init["node_id"][:-1],
            ]
        )
        == 1
    )
    assert "exactly match" in capsys.readouterr().err
    assert main(["--home", str(local_home), "peer-list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    assert (
        main(
            [
                "--home",
                str(local_home),
                "peer-revoke",
                peer_init["node_id"],
                "--confirm",
                peer_init["node_id"],
                "--reason",
                "retired device",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["revoked"]["node_id"] == peer_init["node_id"]
    assert result["restart_required"] is False

    assert main(["--home", str(local_home), "peer-list"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["--home", str(local_home), "peer-revocations"]) == 0
    revocations = json.loads(capsys.readouterr().out)
    assert [item["node_id"] for item in revocations] == [peer_init["node_id"]]

    assert main(["--home", str(local_home), "peer-add", str(peer_card)]) == 1
    assert "locally revoked" in capsys.readouterr().err
    assert local_init["node_id"] != peer_init["node_id"]
