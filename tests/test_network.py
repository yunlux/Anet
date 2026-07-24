from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from anet.config import NodeConfig, initialize_node
from anet.identity import Identity, PeerCard
from anet.node import AnetNode
from anet.peers import PeerBook


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_config(root: Path, label: str) -> NodeConfig:
    return initialize_node(
        root,
        label=label,
        listen_host="127.0.0.1",
        listen_port=free_port(),
    )


def identity_and_card(config: NodeConfig, *, addresses: bool = True) -> tuple[Identity, PeerCard]:
    identity = Identity.load(config.identity_path)
    card = identity.card(
        addresses=config.effective_addresses() if addresses else (),
        capabilities=config.capabilities,
    )
    return identity, card


def trust(config: NodeConfig, *cards: PeerCard) -> None:
    identity = Identity.load(config.identity_path)
    book = PeerBook(config.peers_path, own_node_id=identity.node_id)
    for card in cards:
        book.add(card)


async def wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition was not reached before timeout")


def test_two_nodes_exchange_encrypted_message_and_receipt(tmp_path) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "node_a")
        b_config = make_config(tmp_path / "b", "ahub")
        a_identity, a_card = identity_and_card(a_config)
        b_identity, b_card = identity_and_card(b_config)
        trust(a_config, b_card)
        trust(b_config, a_card)

        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await a.start()
            await b.start()
            packet_id = a.queue(
                b_identity.node_id,
                kind="intent",
                body={"objective": "cross-check", "budget": {"seconds": 30}},
            )
            await wait_until(
                lambda: any(item["packet_id"] == packet_id for item in b.store.list_inbox()),
            )
            message = next(item for item in b.store.list_inbox() if item["packet_id"] == packet_id)
            assert message["trusted"] is True
            assert message["body"]["objective"] == "cross-check"
            await wait_until(lambda: a.store.status()["receipts"] == 1)
            await wait_until(
                lambda: a.store.status()["pending"] == 0
                and b.store.status()["pending"] == 0,
            )
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_active_probe_reports_acknowledged_path_without_polluting_inbox(tmp_path) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "a")
        b_config = make_config(tmp_path / "b", "b")
        _, a_card = identity_and_card(a_config)
        b_identity, b_card = identity_and_card(b_config)
        trust(a_config, b_card)
        trust(b_config, a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await a.start()
            await b.start()
            result = await a.probe(
                b_identity.node_id,
                timeout=5,
                interval=0.2,
                payload_bytes=64 * 1024,
            )
            assert result["ok"] is True
            assert result["qos"] == "control"
            assert result["payload_bytes"] == 64 * 1024
            assert any(
                item["path_id"] == "direct" and item["state"] == "acked"
                for item in result["delivery_paths"]
            )
            assert all(item["kind"] != "network.probe" for item in b.store.list_inbox())
            assert b.store.status()["transient"] >= 1
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_three_node_store_forward_survives_offline_destination(tmp_path) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "a")
        b_config = make_config(tmp_path / "b", "b")
        c_config = make_config(tmp_path / "c", "c")
        a_identity, a_full = identity_and_card(a_config)
        b_identity, b_full = identity_and_card(b_config)
        c_identity, c_full = identity_and_card(c_config)
        _, a_key_only = identity_and_card(a_config, addresses=False)
        _, c_key_only = identity_and_card(c_config, addresses=False)

        trust(a_config, b_full, c_key_only)
        trust(b_config, a_full, c_full)
        trust(c_config, a_key_only, b_full)

        a = AnetNode(a_config)
        b = AnetNode(b_config)
        c = AnetNode(c_config)
        try:
            await a.start()
            await b.start()
            packet_id = a.queue(c_identity.node_id, kind="evidence", body={"hash": "b3:demo"})
            await wait_until(lambda: b.store.has_packet(packet_id))
            assert c.store.list_inbox() == []

            await c.start()
            await wait_until(
                lambda: any(item["packet_id"] == packet_id for item in c.store.list_inbox()),
            )
            message = next(item for item in c.store.list_inbox() if item["packet_id"] == packet_id)
            assert message["body"] == {"hash": "b3:demo"}
            await wait_until(lambda: a.store.status()["receipts"] == 1)
        finally:
            await a.stop()
            await b.stop()
            await c.stop()
            a.close()
            b.close()
            c.close()

    asyncio.run(scenario())


def test_unpinned_peer_cannot_complete_link_handshake(tmp_path) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "a")
        b_config = make_config(tmp_path / "b", "b")
        _, b_card = identity_and_card(b_config)
        trust(a_config, b_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            result = await a.sync_once()
            assert result["connected"] == 0
            assert b.identity.node_id in result["errors"]
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())
