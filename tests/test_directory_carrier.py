from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from anet.carriers.directory import (
    CHANNEL_EPOCH_MS,
    DIRECTORY_CARRIER_VERSION,
    LEGACY_DIRECTORY_CARRIER_VERSION,
    DirectoryCarrier,
    sync_directory_once,
)
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


def make_node(root: Path, label: str, port: int) -> tuple[AnetNode, Identity]:
    config = initialize_node(root, label=label, listen_port=port)
    identity = Identity.load(config.identity_path)
    return AnetNode(config), identity


def trust_each_other(a: AnetNode, b: AnetNode) -> None:
    a_card = a.identity.card(addresses=(), capabilities=a.config.capabilities)
    b_card = b.identity.card(addresses=(), capabilities=b.config.capabilities)
    PeerBook(a.config.peers_path, own_node_id=a.node_id).add(b_card)
    PeerBook(b.config.peers_path, own_node_id=b.node_id).add(a_card)
    a.peers.reload()
    b.peers.reload()


def test_directory_carrier_delivers_without_a_listener(tmp_path) -> None:
    a, _ = make_node(tmp_path / "a", "a", 46101)
    b, b_identity = make_node(tmp_path / "b", "b", 46102)
    trust_each_other(a, b)
    drop = tmp_path / "drop"
    try:
        packet_id = a.queue(
            b_identity.node_id,
            kind="intent",
            body={"objective": "directory-secret"},
        )

        first = sync_directory_once(a, drop, retry_after_ms=0)
        assert first["pushed_packets"] == 1
        files = [path for path in drop.rglob("*") if path.is_file()]
        assert len(files) == 1
        opaque = files[0].read_bytes()
        assert b"directory-secret" not in opaque
        assert packet_id.encode("ascii") not in opaque
        assert a.node_id.encode("ascii") not in opaque
        assert b.node_id.encode("ascii") not in opaque

        second = sync_directory_once(b, drop, retry_after_ms=0)
        assert second["pulled_packets"] == 1
        message = next(
            item for item in b.store.list_inbox() if item["packet_id"] == packet_id
        )
        assert message["body"] == {"objective": "directory-secret"}
        assert message["trusted"] is True

        third = sync_directory_once(a, drop, retry_after_ms=0)
        assert third["pulled_acks"] == 1
        assert third["pulled_packets"] == 1
        assert a.store.status()["receipts"] == 1

        fourth = sync_directory_once(b, drop, retry_after_ms=0)
        assert fourth["pulled_acks"] == 1
        assert a.store.status()["pending"] == 0
        assert b.store.status()["pending"] == 0
        assert all(
            a.node_id not in part.name and b.node_id not in part.name
            for part in drop.iterdir()
        )
    finally:
        a.close()
        b.close()


def test_directory_carrier_quarantines_tampering(tmp_path) -> None:
    a, _ = make_node(tmp_path / "a", "a", 46201)
    b, b_identity = make_node(tmp_path / "b", "b", 46202)
    trust_each_other(a, b)
    drop = tmp_path / "drop"
    try:
        a.queue(b_identity.node_id, kind="message", body="tamper-test")
        sync_directory_once(a, drop, retry_after_ms=0)
        frame = next(path for path in drop.rglob("*") if path.is_file())
        damaged = bytearray(frame.read_bytes())
        damaged[-1] ^= 0x01
        frame.write_bytes(damaged)

        result = sync_directory_once(b, drop, retry_after_ms=0)
        assert result["rejected"] == 1
        assert b.store.list_inbox() == []
        assert len(list((drop / ".quarantine").iterdir())) == 1
    finally:
        a.close()
        b.close()


def test_v2_mailboxes_rotate_and_scan_historical_and_legacy_frames(tmp_path) -> None:
    a, a_identity = make_node(tmp_path / "a", "a", 46301)
    b, b_identity = make_node(tmp_path / "b", "b", 46302)
    trust_each_other(a, b)
    a_card = a_identity.card(addresses=(), capabilities=a.config.capabilities)
    b_card = b_identity.card(addresses=(), capabilities=b.config.capabilities)
    drop = tmp_path / "drop"
    sender = DirectoryCarrier(drop, a_identity)
    receiver = DirectoryCarrier(drop, b_identity)
    now_ms = int(time.time() * 1000)
    current_epoch = now_ms // CHANNEL_EPOCH_MS
    try:
        current = sender.outbound_channel(b_card, epoch=current_epoch)
        following = sender.outbound_channel(b_card, epoch=current_epoch + 1)
        assert current.version == DIRECTORY_CARRIER_VERSION
        assert current.token != following.token
        assert not current.locator.startswith("ch-")

        current_id = "00" * 16
        current_channel, current_name, current_wire = sender.encode_frame(
            b_card,
            kind="ack",
            packet_id=current_id,
            created_ms=now_ms,
        )
        tiny_id = a.queue(b.node_id, kind="message", body="tiny")
        tiny_raw = next(
            item["raw"]
            for item in a.store.pending_for_peer(b.node_id, retry_after_ms=0)
            if item["packet_id"] == tiny_id
        )
        _, _, tiny_wire = sender.encode_frame(
            b_card,
            kind="packet",
            packet_id=tiny_id,
            raw=tiny_raw,
            depth=1,
            created_ms=now_ms,
        )
        assert len(current_wire) == len(tiny_wire)
        assert len(current_wire) >= 4096
        wrong_epoch = receiver.inbound_channel(
            a_card,
            epoch=current_channel.epoch + 1,
        )
        with pytest.raises(InvalidTag):
            receiver.decode_frame(
                Path(current_name),
                current_wire,
                a_card,
                channel=wrong_epoch,
            )
        with pytest.raises(ValueError, match="filename"):
            receiver.decode_frame(
                Path("0" * 48),
                current_wire,
                a_card,
                channel=receiver.inbound_channel(a_card, epoch=current_channel.epoch),
            )

        stale_ms = now_ms - 8 * 24 * 60 * 60 * 1000
        stale_id = "11" * 16
        stale_channel, stale_name, stale_wire = sender.encode_frame(
            b_card,
            kind="ack",
            packet_id=stale_id,
            created_ms=stale_ms,
        )
        assert stale_channel.version == DIRECTORY_CARRIER_VERSION
        assert len(stale_name) == 48 and stale_name.isalnum()
        stale_dir = drop / stale_channel.locator
        stale_dir.mkdir(parents=True)
        (stale_dir / stale_name).write_bytes(stale_wire)

        legacy_id = "22" * 16
        legacy_channel, legacy_name, legacy_wire = sender.encode_frame(
            b_card,
            kind="ack",
            packet_id=legacy_id,
            created_ms=now_ms,
            version=LEGACY_DIRECTORY_CARRIER_VERSION,
        )
        assert legacy_channel.locator.startswith("ch-")
        assert legacy_name.endswith(".drop")
        legacy_dir = drop / legacy_channel.locator
        legacy_dir.mkdir(parents=True)
        (legacy_dir / legacy_name).write_bytes(legacy_wire)

        scan = receiver.scan(a_card)
        assert {frame.packet_id for frame in scan.frames} == {stale_id, legacy_id}
        assert scan.rejected == ()

        empty = tmp_path / "empty"
        DirectoryCarrier(empty, b_identity).scan(a_card)
        assert list(empty.iterdir()) == []
    finally:
        a.close()
        b.close()


def test_v2_publish_negotiates_down_for_a_v1_peer_card(tmp_path) -> None:
    a, a_identity = make_node(tmp_path / "a", "a", 46401)
    b, b_identity = make_node(tmp_path / "b", "b", 46402)
    legacy_b_card = b_identity.card(
        addresses=(),
        capabilities=("directory-carrier-v1",),
    )
    carrier = DirectoryCarrier(tmp_path / "drop", a_identity)
    try:
        channel, filename, _ = carrier.encode_frame(
            legacy_b_card,
            kind="ack",
            packet_id="33" * 16,
        )
        assert channel.version == LEGACY_DIRECTORY_CARRIER_VERSION
        assert channel.locator.startswith("ch-")
        assert filename.endswith(".drop")
    finally:
        a.close()
        b.close()
