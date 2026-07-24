from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import replace
from pathlib import Path

from anet.config import (
    DirectDialerConfig,
    DirectProxyConfig,
    NodeConfig,
    initialize_node,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


def test_legacy_proxy_and_raw_configs_keep_their_original_behavior(
    tmp_path: Path,
) -> None:
    raw = initialize_node(tmp_path / "raw", label="raw")
    assert [item.name for item in raw.effective_direct_dialers()] == ["raw"]

    proxy = DirectProxyConfig.from_dict({"url": "socks5h://127.0.0.1:1080"})
    legacy = replace(
        initialize_node(tmp_path / "legacy", label="legacy"),
        direct_proxy=proxy,
    )
    legacy.save()
    loaded = NodeConfig.load(legacy.home)
    assert loaded.direct_dialers == ()
    assert loaded.effective_direct_dialers() == (
        DirectDialerConfig(name="legacy-proxy", priority=0, proxy=proxy),
    )


def test_explicit_dialers_round_trip_and_reject_ambiguous_type(tmp_path: Path) -> None:
    proxy = DirectProxyConfig.from_dict({"url": "socks5://127.0.0.1:1080"})
    config = replace(
        initialize_node(tmp_path / "node", label="node"),
        direct_dialers=(
            DirectDialerConfig(name="raw", priority=0),
            DirectDialerConfig(name="local-proxy", priority=20, proxy=proxy),
        ),
    )
    config.save()
    loaded = NodeConfig.load(config.home)
    assert loaded.direct_dialers == config.direct_dialers
    assert [item.kind for item in loaded.effective_direct_dialers()] == [
        "raw",
        "socks5",
    ]


def test_failed_preferred_dialer_cools_down_then_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        config = initialize_node(tmp_path / "node", label="node")
        proxy = DirectProxyConfig.from_dict({"url": "socks5://127.0.0.1:1080"})
        config = replace(
            config,
            direct_dialers=(
                DirectDialerConfig(name="raw", priority=0),
                DirectDialerConfig(name="proxy", priority=100, proxy=proxy),
            ),
            routing=replace(
                config.routing,
                direct_failure_threshold=1,
                direct_retry_interval=0.2,
            ),
        )
        peer = Identity.generate("peer")
        card = peer.card(
            addresses=("tls://127.0.0.1:4242",), capabilities=()
        )
        node = AnetNode(config)
        calls: list[str] = []
        raw_healthy = False

        async def fake_sync(_expected, _address, dialer) -> None:  # noqa: ANN001
            calls.append(dialer.name)
            if dialer.name == "raw" and not raw_healthy:
                raise ConnectionError("simulated raw-path block")

        monkeypatch.setattr(node, "_sync_address", fake_sync)
        try:
            assert await node._sync_peer(card) is True
            assert calls == ["raw", "proxy"]
            calls.clear()

            assert await node._sync_peer(card) is True
            assert calls == ["proxy"]
            assert node.peer_state[card.node_id]["dialer"] == "proxy"

            calls.clear()
            await asyncio.sleep(0.25)
            raw_healthy = True
            assert await node._sync_peer(card) is True
            assert calls == ["raw"]
            assert node.peer_state[card.node_id]["dialer"] == "raw"

            raw_path = "direct:raw:tls://127.0.0.1:4242"
            proxy_path = "direct:proxy:tls://127.0.0.1:4242"
            assert node.store.path_metric(card.node_id, raw_path)["successes"] == 1
            assert node.store.path_metric(card.node_id, raw_path)["failures"] == 1
            assert node.store.path_metric(card.node_id, proxy_path)["successes"] == 2
        finally:
            node.close()

    asyncio.run(scenario())


def test_hedged_sync_uses_fast_authenticated_path_and_cancels_loser(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        config = initialize_node(tmp_path / "node", label="node")
        proxy = DirectProxyConfig.from_dict({"url": "socks5://127.0.0.1:1080"})
        config = replace(
            config,
            direct_dialers=(
                DirectDialerConfig(name="slow-raw", priority=0),
                DirectDialerConfig(name="fast-proxy", priority=100, proxy=proxy),
            ),
            routing=replace(
                config.routing,
                direct_race_width=2,
                direct_race_delay=0.0,
            ),
        )
        peer = Identity.generate("peer")
        card = peer.card(addresses=("tls://127.0.0.1:4242",), capabilities=())
        node = AnetNode(config)
        calls: list[str] = []
        slow_cancelled = asyncio.Event()

        async def fake_sync(_expected, _address, dialer) -> None:  # noqa: ANN001
            calls.append(dialer.name)
            if dialer.name == "fast-proxy":
                await asyncio.sleep(0.01)
                return
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                slow_cancelled.set()
                raise

        monkeypatch.setattr(node, "_sync_address", fake_sync)
        try:
            started = time.perf_counter()
            assert await node._sync_peer(card) is True
            elapsed = time.perf_counter() - started
            assert elapsed < 0.25
            assert calls == ["slow-raw", "fast-proxy"]
            assert slow_cancelled.is_set()
            assert node.peer_state[card.node_id]["dialer"] == "fast-proxy"
            assert node.store.path_metric(
                card.node_id, "direct:fast-proxy:tls://127.0.0.1:4242"
            )["successes"] == 1
            assert node.store.path_metric(
                card.node_id, "direct:slow-raw:tls://127.0.0.1:4242"
            ) is None
        finally:
            node.close()

    asyncio.run(scenario())


def test_two_real_racing_paths_do_not_duplicate_inbox(tmp_path: Path) -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
        b_config = initialize_node(
            tmp_path / "b",
            label="b",
            listen_host="127.0.0.1",
            listen_port=port,
        )
        a_config = replace(
            a_config,
            direct_dialers=(
                DirectDialerConfig(name="raw-a", priority=0),
                DirectDialerConfig(name="raw-b", priority=0),
            ),
            routing=replace(
                a_config.routing,
                direct_race_width=2,
                direct_race_delay=0.0,
            ),
        )
        a_config.save()
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(
            a_config.peers_path, own_node_id=a_identity.node_id
        ).add(b_card)
        PeerBook(
            b_config.peers_path, own_node_id=b_identity.node_id
        ).add(a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            packet_id = a.queue(
                b_identity.node_id,
                kind="evidence",
                body={"race": "idempotent"},
            )
            assert await a._sync_peer(b_card) is True
            matching = [
                item for item in b.store.list_inbox()
                if item["packet_id"] == packet_id
            ]
            assert len(matching) == 1
            assert matching[0]["trusted"] is True
            assert any(
                item["state"] == "acked"
                for item in a.store.delivery_paths(packet_id)
            )
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_authenticated_dialer_probe_does_not_exchange_business_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
        b_config = initialize_node(
            tmp_path / "b", label="b", listen_port=port
        )
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(
            a_config.peers_path, own_node_id=a_identity.node_id
        ).add(b_card)
        PeerBook(
            b_config.peers_path, own_node_id=b_identity.node_id
        ).add(a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            before_a = a.store.status()
            before_b = b.store.status()
            result = await a.probe_dialers(b_identity.node_id)
            assert result["ok"] is True
            assert result["all_healthy"] is True
            assert result["tested"] == 1
            assert result["results"][0]["category"] == "authenticated"
            assert result["results"][0]["path_id"].startswith("health:raw:")
            assert a.store.status()["packets"] == before_a["packets"]
            assert a.store.status()["inbox"] == before_a["inbox"]
            assert b.store.status()["packets"] == before_b["packets"]
            assert b.store.status()["inbox"] == before_b["inbox"]
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_dialer_probe_classifies_unreachable_proxy(tmp_path: Path) -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = int(sock.getsockname()[1])
        config = initialize_node(tmp_path / "node", label="node")
        proxy = DirectProxyConfig.from_dict(
            {"url": f"socks5h://127.0.0.1:{closed_port}"}
        )
        config = replace(
            config,
            direct_dialers=(
                DirectDialerConfig(name="closed-proxy", priority=0, proxy=proxy),
            ),
        )
        config.save()
        peer = Identity.generate("peer")
        card = peer.card(addresses=("tls://127.0.0.1:4242",), capabilities=())
        PeerBook(
            config.peers_path,
            own_node_id=Identity.load(config.identity_path).node_id,
        ).add(card)
        node = AnetNode(config)
        try:
            result = await node.probe_dialers(peer.node_id)
            assert result["ok"] is False
            assert result["tested"] == 1
            assert result["results"][0]["category"] == "proxy_unreachable"
            metric = node.store.path_metric(
                peer.node_id,
                "health:closed-proxy:tls://127.0.0.1:4242",
            )
            assert metric["failures"] == 1
            assert metric["last_error"].startswith("proxy_unreachable:")
        finally:
            node.close()

    asyncio.run(scenario())


def test_dialer_probe_distinguishes_identity_rejection(tmp_path: Path) -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
        b_config = initialize_node(tmp_path / "b", label="b", listen_port=port)
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(
            a_config.peers_path, own_node_id=a_identity.node_id
        ).add(b_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            result = await a.probe_dialers(b_identity.node_id)
            assert result["ok"] is False
            assert result["results"][0]["category"] == "identity_handshake"
            assert b.store.status()["inbox"] == 0
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_dialer_probe_fails_cleanly_for_pre_health_peer(tmp_path: Path) -> None:
    async def scenario() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
        b_config = initialize_node(tmp_path / "b", label="b", listen_port=port)
        b_config = replace(
            b_config,
            capabilities=tuple(
                item for item in b_config.capabilities
                if item != "link-health-v1"
            ),
        )
        b_config.save()
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(
            a_config.peers_path, own_node_id=a_identity.node_id
        ).add(b_card)
        PeerBook(
            b_config.peers_path, own_node_id=b_identity.node_id
        ).add(a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            result = await a.probe_dialers(b_identity.node_id)
            assert result["ok"] is False
            assert result["results"][0]["category"] == "health_unsupported"
            assert b.store.status()["inbox"] == 0
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())
