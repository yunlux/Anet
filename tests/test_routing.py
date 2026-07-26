from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import replace
from pathlib import Path

from anet.config import (
    DirectoryCarrierConfig,
    NodeConfig,
    RoutingConfig,
    WebDAVCarrierConfig,
    initialize_node,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import inspect_packet
from anet.peers import PeerBook
from anet.routing import AdaptiveRouter
from anet.store import PacketStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def carrier(path: Path, *, name: str = "drop", priority: int = 100) -> DirectoryCarrierConfig:
    return DirectoryCarrierConfig(
        name=name,
        path=path,
        mode="fallback",
        interval=0.2,
        retry_seconds=0,
        priority=priority,
    )


def test_carrier_replica_count_is_backward_compatible_and_bounded() -> None:
    assert RoutingConfig.from_dict({}).carrier_replica_count == 1
    assert RoutingConfig.from_dict({"carrier_replica_count": 0}).carrier_replica_count == 1
    assert RoutingConfig.from_dict({"carrier_replica_count": 99}).carrier_replica_count == 4


def test_router_fails_fast_and_recovers_with_hysteresis(tmp_path) -> None:
    store = PacketStore(tmp_path / "route.sqlite3")
    fallback = carrier(tmp_path / "drop")
    router = AdaptiveRouter(
        store,
        RoutingConfig(
            direct_failure_threshold=2,
            direct_recovery_threshold=3,
            switch_cooldown=3600,
        ),
    )
    try:
        initial = router.decide("peer", has_direct=True, carriers=[fallback])
        assert initial.selected_path == "direct"
        assert initial.push_qos == {}

        store.record_path_result("peer", "direct", success=False, latency_ms=10, error="down")
        assert store.path_metric("peer", "direct")["ewma_rtt_ms"] == 0
        first_failure = router.decide("peer", has_direct=True, carriers=[fallback])
        assert first_failure.selected_path == "direct"
        assert first_failure.push_qos == {"drop": frozenset({"control", "interactive"})}

        store.record_path_result("peer", "direct", success=False, latency_ms=10, error="down")
        failed_over = router.decide("peer", has_direct=True, carriers=[fallback])
        assert failed_over.selected_path == "directory:drop"
        assert failed_over.push_qos["drop"] == frozenset(
            {"control", "interactive", "normal", "bulk"}
        )

        for _ in range(3):
            store.record_path_result("peer", "direct", success=True, latency_ms=2)
        held = router.decide("peer", has_direct=True, carriers=[fallback])
        assert held.selected_path == "directory:drop"

        immediate_recovery = AdaptiveRouter(
            store,
            RoutingConfig(
                direct_failure_threshold=2,
                direct_recovery_threshold=3,
                switch_cooldown=0,
            ),
        ).decide("peer", has_direct=True, carriers=[fallback])
        assert immediate_recovery.selected_path == "direct"

        dav = WebDAVCarrierConfig.from_dict(
            {
                "name": "dav",
                "base_url": "http://127.0.0.1:48080/dav",
                "allow_insecure_http": True,
                "priority": 10,
            }
        )
        no_direct = router.decide("another-peer", has_direct=False, carriers=[fallback, dav])
        assert no_direct.selected_path == "webdav:dav"
        assert no_direct.push_qos["dav"] == frozenset(
            {"control", "interactive", "normal", "bulk"}
        )
    finally:
        store.close()


def test_router_fails_between_multiple_fallbacks_and_returns_to_preferred(tmp_path) -> None:
    store = PacketStore(tmp_path / "multi.sqlite3")
    primary = carrier(tmp_path / "primary", name="primary", priority=10)
    secondary = carrier(tmp_path / "secondary", name="secondary", priority=20)
    router = AdaptiveRouter(
        store,
        RoutingConfig(
            carrier_failure_threshold=2,
            carrier_recovery_threshold=3,
            switch_cooldown=0,
        ),
    )
    try:
        initial = router.decide("peer", has_direct=False, carriers=[primary, secondary])
        assert initial.selected_path == "directory:primary"

        for _ in range(2):
            store.record_path_result(
                "peer",
                primary.path_id,
                success=False,
                latency_ms=10,
                error="unavailable",
            )
        failed_over = router.decide("peer", has_direct=False, carriers=[primary, secondary])
        assert failed_over.selected_path == "directory:secondary"
        assert failed_over.reason == "fallback directory:primary failed 2 consecutive probes"

        for _ in range(3):
            store.record_path_result("peer", primary.path_id, success=True, latency_ms=2)
        recovered = router.decide("peer", has_direct=False, carriers=[primary, secondary])
        assert recovered.selected_path == "directory:primary"
        assert "preferred fallback" in recovered.reason
    finally:
        store.close()


def test_router_replicates_to_bounded_best_healthy_carriers(tmp_path) -> None:
    store = PacketStore(tmp_path / "replicas.sqlite3")
    primary = carrier(tmp_path / "primary", name="primary", priority=10)
    secondary = carrier(tmp_path / "secondary", name="secondary", priority=20)
    tertiary = carrier(tmp_path / "tertiary", name="tertiary", priority=30)
    router = AdaptiveRouter(
        store,
        RoutingConfig(carrier_replica_count=2, switch_cooldown=0),
    )
    try:
        decision = router.decide(
            "peer", has_direct=False, carriers=[tertiary, secondary, primary]
        )
        assert decision.selected_path == primary.path_id
        assert set(decision.push_qos) == {"primary", "secondary"}
        assert all(
            qos == frozenset({"control", "interactive", "normal", "bulk"})
            for qos in decision.push_qos.values()
        )

        for _ in range(2):
            store.record_path_result(
                "peer",
                primary.path_id,
                success=False,
                latency_ms=5,
                error="blocked",
            )
        failed_over = router.decide(
            "peer", has_direct=False, carriers=[primary, secondary, tertiary]
        )
        assert failed_over.selected_path == secondary.path_id
        assert set(failed_over.push_qos) == {"secondary", "tertiary"}
    finally:
        store.close()


def test_router_fast_failover_replicates_only_urgent_qos(tmp_path) -> None:
    store = PacketStore(tmp_path / "fast-replicas.sqlite3")
    primary = carrier(tmp_path / "primary", name="primary", priority=10)
    secondary = carrier(tmp_path / "secondary", name="secondary", priority=20)
    router = AdaptiveRouter(
        store,
        RoutingConfig(
            direct_failure_threshold=3,
            carrier_replica_count=2,
            switch_cooldown=0,
        ),
    )
    try:
        store.record_path_result("peer", "direct", success=False, latency_ms=5, error="loss")
        decision = router.decide(
            "peer", has_direct=True, carriers=[primary, secondary]
        )
        assert decision.selected_path == "direct"
        assert decision.push_qos == {
            "primary": frozenset({"control", "interactive"}),
            "secondary": frozenset({"control", "interactive"}),
        }
    finally:
        store.close()


def test_adaptive_sync_replicates_same_ciphertext_without_duplicate_inbox(tmp_path) -> None:
    async def scenario() -> None:
        a_base, a_identity, a_card = _node_and_card(tmp_path / "a", "a", 47401)
        b_base, b_identity, b_card = _node_and_card(tmp_path / "b", "b", 47402)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_base.peers_path, own_node_id=b_identity.node_id).add(a_card)
        carriers = (
            carrier(tmp_path / "drop-primary", name="primary", priority=10),
            carrier(tmp_path / "drop-secondary", name="secondary", priority=20),
        )
        routing = RoutingConfig(carrier_replica_count=2, switch_cooldown=0)
        a_config = replace(
            a_base,
            listen_enabled=False,
            direct_enabled=False,
            directory_carriers=carriers,
            routing=routing,
        )
        b_config = replace(
            b_base,
            listen_enabled=False,
            direct_enabled=False,
            directory_carriers=carriers,
            routing=routing,
        )
        a_config.save()
        b_config.save()
        a = AnetNode(NodeConfig.load(a_config.home))
        b = AnetNode(NodeConfig.load(b_config.home))
        try:
            packet_id = a.queue(b_identity.node_id, kind="message", body="replicated")
            sent = await a.adaptive_sync_once(skip_direct=True)
            pushed = {
                item["name"]: item["pushed_packets"]
                for item in sent["carriers"]
                if item["peer_id"] == b_identity.node_id
            }
            assert pushed == {"primary": 1, "secondary": 1}

            await b.adaptive_sync_once(skip_direct=True)
            matching = [
                item for item in b.store.list_inbox() if item["packet_id"] == packet_id
            ]
            assert len(matching) == 1

            await a.adaptive_sync_once(skip_direct=True)
            assert a.store.pending_for_peer(b_identity.node_id, retry_after_ms=0) == []
        finally:
            a.close()
            b.close()

    asyncio.run(scenario())


def _node_and_card(root: Path, label: str, port: int):
    config = initialize_node(root, label=label, listen_port=port)
    identity = Identity.load(config.identity_path)
    return config, identity, identity.card(
        addresses=config.effective_addresses(),
        capabilities=config.capabilities,
    )


def test_adaptive_sync_races_control_before_full_failover(tmp_path) -> None:
    async def scenario() -> None:
        a_base, a_identity, a_card = _node_and_card(tmp_path / "a", "a", 46901)
        b_base, b_identity, b_card = _node_and_card(tmp_path / "b", "b", 46902)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_base.peers_path, own_node_id=b_identity.node_id).add(a_card)
        drop = tmp_path / "shared"
        route = RoutingConfig(
            direct_failure_threshold=3,
            direct_recovery_threshold=2,
            switch_cooldown=0,
        )
        a_config = replace(
            a_base,
            listen_enabled=False,
            sync_interval=60,
            directory_carriers=(carrier(drop),),
            routing=route,
        )
        b_config = replace(
            b_base,
            listen_enabled=False,
            sync_interval=60,
            directory_carriers=(carrier(drop),),
            routing=route,
        )
        a_config.save()
        b_config.save()
        a = AnetNode(NodeConfig.load(a_config.home))
        b = AnetNode(NodeConfig.load(b_config.home))
        try:
            control_id = a.queue(b_identity.node_id, kind="command", body="cancel", qos="control")
            normal_id = a.queue(b_identity.node_id, kind="message", body="later", qos="normal")
            result = await a.adaptive_sync_once()
            assert result["routes"][0]["selected_path"] == "direct"
            pushed = sum(item["pushed_packets"] for item in result["carriers"])
            assert pushed == 1

            await b.adaptive_sync_once()
            inbox_ids = {item["packet_id"] for item in b.store.list_inbox()}
            assert control_id in inbox_ids
            assert normal_id not in inbox_ids

            await a.adaptive_sync_once()
            third = await a.adaptive_sync_once()
            assert third["routes"][0]["selected_path"] == "directory:drop"
            await b.adaptive_sync_once()
            inbox_ids = {item["packet_id"] for item in b.store.list_inbox()}
            assert normal_id in inbox_ids
            raw = a.store.get_packet(normal_id)
            assert raw is not None and inspect_packet(raw).qos == "normal"
        finally:
            a.close()
            b.close()

    asyncio.run(scenario())


def test_selected_fallback_does_not_wait_for_slow_direct_recovery_probe(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        a_base, a_identity, _ = _node_and_card(tmp_path / "a", "a", 47301)
        b_base, b_identity, b_card = _node_and_card(tmp_path / "b", "b", 47302)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_card)
        configured = replace(
            a_base,
            listen_enabled=False,
            directory_carriers=(carrier(tmp_path / "drop"),),
            routing=RoutingConfig(fallback_probe_interval=0.5, switch_cooldown=0),
        )
        configured.save()
        a = AnetNode(NodeConfig.load(configured.home))

        async def slow_probe(_card):  # noqa: ANN001, ANN202
            await asyncio.sleep(2)
            return False

        monkeypatch.setattr(a, "_sync_peer", slow_probe)
        a.store.set_route(b_identity.node_id, "directory:drop", "test fallback")
        try:
            started = asyncio.get_running_loop().time()
            result = await a.adaptive_sync_once()
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 0.5
            assert result["direct"]["background_probes"] == [b_identity.node_id]
            assert result["routes"][0]["selected_path"] == "directory:drop"
        finally:
            await a.stop()
            a.close()

    asyncio.run(scenario())


def test_probe_pulls_inflight_carrier_receipt_without_second_direct_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        a_base, a_identity, _a_full = _node_and_card(tmp_path / "a", "a", 47401)
        b_base, b_identity, b_full = _node_and_card(tmp_path / "b", "b", 47402)
        a_keys = a_identity.card(addresses=(), capabilities=a_base.capabilities)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_full)
        PeerBook(b_base.peers_path, own_node_id=b_identity.node_id).add(a_keys)
        drop = tmp_path / "drop"
        path = carrier(drop)
        a_config = replace(
            a_base,
            listen_enabled=False,
            directory_carriers=(path,),
            routing=RoutingConfig(
                direct_failure_threshold=2,
                fallback_probe_interval=10,
                switch_cooldown=0,
            ),
        )
        b_config = replace(
            b_base,
            direct_enabled=False,
            sync_interval=0.2,
            directory_carriers=(path,),
            routing=RoutingConfig(switch_cooldown=0),
        )
        a_config.save()
        b_config.save()
        a = AnetNode(NodeConfig.load(a_config.home))
        b = AnetNode(NodeConfig.load(b_config.home))
        direct_calls = 0

        async def slow_failure(card):  # noqa: ANN001, ANN202
            nonlocal direct_calls
            direct_calls += 1
            await asyncio.sleep(0.2)
            a.store.record_path_result(
                card.node_id,
                "direct",
                success=False,
                latency_ms=200,
                error="blackhole",
            )
            raise ConnectionError("blackhole")

        monkeypatch.setattr(a, "_sync_peer", slow_failure)
        try:
            await b.start()
            result = await a.probe(
                b_identity.node_id,
                timeout=3,
                interval=0.2,
                carrier_grace=1.5,
            )
            assert result["ok"] is True
            assert direct_calls == 1
            assert result["elapsed_ms"] < 1500
            assert any(
                item["path_id"] == "directory:drop"
                and item["state"] in {"sent", "acked"}
                for item in result["delivery_paths"]
            )
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_background_direct_schedule_idles_and_new_packet_wakes_immediately(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        a_base, a_identity, _ = _node_and_card(tmp_path / "a", "a", 47501)
        b_base, b_identity, b_card = _node_and_card(tmp_path / "b", "b", 47502)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_card)
        configured = replace(
            a_base,
            listen_enabled=False,
            routing=RoutingConfig(
                direct_retry_interval=5,
                direct_idle_probe_interval=60,
                direct_probe_jitter=0,
                direct_idle_backoff_max=4,
                switch_cooldown=0,
            ),
        )
        configured.save()
        a = AnetNode(NodeConfig.load(configured.home))
        calls: list[set[str]] = []

        async def fake_sync_once(*, peer_ids=None):  # noqa: ANN001, ANN202
            selected = set(peer_ids or set())
            calls.append(selected)
            return {
                "attempted": len(selected),
                "connected": len(selected),
                "errors": {},
                "peer_results": {
                    peer_id: {"connected": True, "error": ""} for peer_id in selected
                },
            }

        monkeypatch.setattr(a, "sync_once", fake_sync_once)
        try:
            first = await a.adaptive_sync_once(force_carriers=False)
            assert calls[-1] == {b_identity.node_id}
            assert first["schedules"]["direct"][0]["last_delay_seconds"] == 60

            await a.adaptive_sync_once(force_carriers=False)
            assert calls[-1] == set()

            a.queue(b_identity.node_id, kind="message", body="wake")
            active = await a.adaptive_sync_once(force_carriers=False)
            assert calls[-1] == {b_identity.node_id}
            assert active["schedules"]["direct"][0]["last_delay_seconds"] == 5

            await a.adaptive_sync_once(force_carriers=False)
            assert calls[-1] == set()
        finally:
            a.close()

    asyncio.run(scenario())


def test_background_carrier_backs_off_but_new_packet_bypasses_wait(tmp_path) -> None:
    async def scenario() -> None:
        a_base, a_identity, _ = _node_and_card(tmp_path / "a", "a", 47601)
        b_base, b_identity, _ = _node_and_card(tmp_path / "b", "b", 47602)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(
            b_identity.card(addresses=(), capabilities=b_base.capabilities)
        )
        path = DirectoryCarrierConfig(
            name="drop",
            path=tmp_path / "drop",
            interval=10,
            jitter=0,
            idle_backoff_max=4,
            retry_seconds=0,
        )
        configured = replace(
            a_base,
            listen_enabled=False,
            direct_enabled=False,
            directory_carriers=(path,),
            routing=RoutingConfig(switch_cooldown=0),
        )
        configured.save()
        a = AnetNode(NodeConfig.load(configured.home))
        try:
            first = await a.adaptive_sync_once(force_carriers=False)
            assert first["schedules"]["carriers"][0]["ran"] is True
            assert first["schedules"]["carriers"][0]["last_delay_seconds"] == 10

            idle = await a.adaptive_sync_once(force_carriers=False)
            assert idle["carriers"] == []
            assert idle["schedules"]["carriers"][0]["ran"] is False

            packet_id = a.queue(b_identity.node_id, kind="message", body="wake-carrier")
            active = await a.adaptive_sync_once(force_carriers=False)
            assert active["carriers"][0]["pushed_packets"] == 1
            assert active["schedules"]["carriers"][0]["ran"] is True
            assert active["schedules"]["carriers"][0]["idle_rounds"] == 0
            assert any(
                item["packet_id"] == packet_id
                for item in a.store.pending_for_peer(b_identity.node_id, retry_after_ms=0)
            )

            held = await a.adaptive_sync_once(force_carriers=False)
            assert held["carriers"] == []
            assert held["schedules"]["carriers"][0]["ran"] is False
        finally:
            a.close()

    asyncio.run(scenario())


def test_real_background_nodes_do_not_poll_direct_on_fixed_short_cadence(tmp_path) -> None:
    async def scenario() -> None:
        a_base, a_identity, a_card = _node_and_card(tmp_path / "a", "a", _free_port())
        b_base, b_identity, b_card = _node_and_card(tmp_path / "b", "b", _free_port())
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_base.peers_path, own_node_id=b_identity.node_id).add(a_card)
        routing = RoutingConfig(
            direct_retry_interval=0.2,
            direct_idle_probe_interval=5,
            direct_probe_jitter=0,
            direct_idle_backoff_max=4,
            switch_cooldown=0,
        )
        for base in (a_base, b_base):
            replace(
                base,
                sync_interval=0.2,
                sync_jitter=0,
                routing=routing,
                prekey_auto_enabled=False,
            ).save()
        a = AnetNode(NodeConfig.load(a_base.home))
        b = AnetNode(NodeConfig.load(b_base.home))
        try:
            await b.start()
            await a.start()
            before = None
            for _ in range(50):
                before = a.store.path_metric(b.node_id, "direct")
                if before is not None:
                    break
                await asyncio.sleep(0.1)
            assert before is not None
            attempts_before = int(before["attempts"])

            await asyncio.sleep(0.8)
            idle = a.store.path_metric(b.node_id, "direct")
            assert idle is not None
            assert int(idle["attempts"]) == attempts_before

            queued_at = time.monotonic()
            packet_id = a.queue(b.node_id, kind="message", body="background-wake")
            for _ in range(20):
                if any(item["packet_id"] == packet_id for item in b.store.list_inbox()):
                    break
                await asyncio.sleep(0.1)
            assert any(item["packet_id"] == packet_id for item in b.store.list_inbox())
            assert time.monotonic() - queued_at < routing.direct_idle_probe_interval
            active = a.store.path_metric(b.node_id, "direct")
            assert active is not None
            assert int(active["attempts"]) >= attempts_before
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())
