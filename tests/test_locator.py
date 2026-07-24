from __future__ import annotations

import asyncio
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from anet.config import NodeConfig, initialize_node
from anet.identity import Identity
from anet.locator import parse_locator, usable_locators, validate_locator_context
from anet.node import AnetNode
from anet.peers import PeerBook


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_scoped_locator_parsing_and_selection() -> None:
    zone = "n4K0mW8p2Qx7"
    host = f"tls://127.0.0.1:4242?scope=host&zone={zone}&priority=10"
    lan = f"tls://192.0.2.10:4242?scope=lan&zone={zone}&priority=20"
    wan = "tls://example.invalid:4242?scope=wan&priority=40"
    legacy = "tls://198.51.100.10:4242"

    parsed = parse_locator(host)
    assert parsed.scope == "host"
    assert parsed.context == f"host:{zone}"
    assert parsed.priority == 10
    assert [item.raw for item in usable_locators(
        (wan, legacy, lan, host), {f"host:{zone}"}
    )] == [host, wan, legacy]
    assert [item.raw for item in usable_locators(
        (wan, legacy, lan, host), {f"lan:{zone}"}
    )] == [lan, wan, legacy]


@pytest.mark.parametrize(
    "value",
    [
        "tls://127.0.0.1:4242?scope=host",
        "tls://127.0.0.1:4242?scope=lan&zone=short",
        "tls://127.0.0.1:4242?scope=wan&zone=abcdefgh",
        "tls://127.0.0.1:4242?scope=planet",
        "tls://127.0.0.1:4242?scope=wan&priority=-1",
        "tls://127.0.0.1:4242?unknown=1",
    ],
)
def test_invalid_scoped_locators_fail_closed(value: str) -> None:
    with pytest.raises(ValueError):
        parse_locator(value)


def test_locator_context_validation() -> None:
    assert validate_locator_context("host:Abcdefgh_12") == "host:Abcdefgh_12"
    assert validate_locator_context("lan:Abcdefgh-12") == "lan:Abcdefgh-12"
    with pytest.raises(ValueError):
        validate_locator_context("wan:Abcdefgh-12")


def test_node_skips_wrong_host_zone_and_uses_matching_lan_locator(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        host_zone = "hostZone1234"
        lan_zone = "sharedLan1234"
        a_config = initialize_node(
            tmp_path / "a",
            label="a",
            listen_port=free_port(),
            locator_contexts=(f"lan:{lan_zone}",),
        )
        b_port = free_port()
        b_config = initialize_node(
            tmp_path / "b",
            label="b",
            listen_port=b_port,
            locator_contexts=(f"host:{host_zone}", f"lan:{lan_zone}"),
        )
        b_config = replace(
            b_config,
            advertise=(
                f"tls://127.0.0.1:1?scope=host&zone={host_zone}&priority=0",
                f"tls://127.0.0.1:{b_port}?scope=lan&zone={lan_zone}&priority=10",
            ),
        )
        b_config.save()
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a_card = a_identity.card(
            addresses=a_config.effective_addresses(),
            capabilities=a_config.capabilities,
        )
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(a_card)
        a = AnetNode(NodeConfig.load(a_config.home))
        b = AnetNode(NodeConfig.load(b_config.home))
        try:
            await b.start()
            assert await a._sync_peer(b_card) is True
            assert a.peer_state[b_identity.node_id]["address"].endswith(
                f"scope=lan&zone={lan_zone}&priority=10"
            )
            metrics = a.store.path_metrics(b_identity.node_id)
            assert all(":1?scope=host" not in item["path_id"] for item in metrics)
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())
