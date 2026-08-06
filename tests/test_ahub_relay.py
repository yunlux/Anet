from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from anet.ahub import (
    AHUB_DB_VERSION,
    AhubLimits,
    AhubService,
    issue_ahub_request,
)
from anet.ahub_http import AhubHTTPClient, AhubHTTPError
from anet.control_plane import issue_node_descriptor
from anet.config import AhubCarrierConfig, NodeConfig, initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from websockets.exceptions import ConnectionClosed


def provision(service: AhubService, identity: Identity, *, now: int) -> None:
    service.allow_node(identity.node_id, now=now)
    service.publish_descriptor(
        issue_node_descriptor(
            identity,
            capabilities=("ahub-relay-v1",),
            issued_ms=now,
            ttl_ms=60 * 60 * 1000,
        ),
        now=now,
    )


def reserve_body(
    peer_id: str,
    *,
    ttl_ms: int = 60_000,
    duration_ms: int = 30_000,
    byte_limit: int = 1024,
) -> bytes:
    from anet.ahub_http import ahub_json

    return ahub_json(
        {
            "allowed_peer_id": peer_id,
            "ttl_ms": ttl_ms,
            "max_duration_ms": duration_ms,
            "max_bytes_each_direction": byte_limit,
        }
    )


def test_relay_reservation_is_durable_scoped_and_replay_safe(
    tmp_path: Path,
) -> None:
    now = int(time.time() * 1000)
    owner = Identity.generate("owner")
    peer = Identity.generate("peer")
    outsider = Identity.generate("outsider")
    root = tmp_path / "ahub"
    body = reserve_body(peer.node_id)
    path = "/v1/relay/reservations"
    signed = issue_ahub_request(
        owner,
        method="POST",
        path=path,
        body=body,
        issued_ms=now,
        nonce="r" * 24,
    )

    with AhubService(root) as service:
        for identity in (owner, peer, outsider):
            provision(service, identity, now=now)
        reservation = service.reserve_relay(
            signed,
            body,
            allowed_peer_id=peer.node_id,
            ttl_ms=60_000,
            max_duration_ms=30_000,
            max_bytes_each_direction=1024,
            now=now,
        )
        assert service.status(now=now)["database_version"] == AHUB_DB_VERSION
        assert service.status(now=now)["relay_reservations"] == 1
        with pytest.raises(PermissionError, match="already used"):
            service.reserve_relay(
                signed,
                body,
                allowed_peer_id=peer.node_id,
                ttl_ms=60_000,
                max_duration_ms=30_000,
                max_bytes_each_direction=1024,
                now=now,
            )

    with AhubService(root) as restarted:
        relay_path = f"/v1/relay/{reservation.reservation_id}"
        owner_auth = None
        for member in (owner, peer):
            auth = issue_ahub_request(
                member,
                method="GET",
                path=relay_path,
                issued_ms=now + 1,
            )
            if member is owner:
                owner_auth = auth
            authorized = restarted.authorize_relay(
                auth,
                b"",
                reservation_id=reservation.reservation_id,
                now=now + 1,
            )
            assert authorized == reservation
        assert owner_auth is not None
        with pytest.raises(PermissionError, match="already used"):
            restarted.authorize_relay(
                owner_auth,
                b"",
                reservation_id=reservation.reservation_id,
                now=now + 1,
            )

        outsider_auth = issue_ahub_request(
            outsider,
            method="GET",
            path=relay_path,
            issued_ms=now + 1,
        )
        with pytest.raises(PermissionError, match="not authorized"):
            restarted.authorize_relay(
                outsider_auth,
                b"",
                reservation_id=reservation.reservation_id,
                now=now + 1,
            )

        expired_auth = issue_ahub_request(
            peer,
            method="GET",
            path=relay_path,
            issued_ms=now + 60_001,
        )
        with pytest.raises(LookupError, match="not found"):
            restarted.authorize_relay(
                expired_auth,
                b"",
                reservation_id=reservation.reservation_id,
                now=now + 60_001,
            )


def test_relay_reservation_refresh_and_owner_quota(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    owner = Identity.generate("owner")
    peers = tuple(Identity.generate(f"peer-{index}") for index in range(3))
    limits = AhubLimits(max_relay_reservations_per_node=2)
    with AhubService(tmp_path / "ahub", limits=limits) as service:
        provision(service, owner, now=now)
        for peer in peers:
            provision(service, peer, now=now)

        def reserve(peer: Identity, nonce: str, at: int):
            body = reserve_body(peer.node_id)
            return service.reserve_relay(
                issue_ahub_request(
                    owner,
                    method="POST",
                    path="/v1/relay/reservations",
                    body=body,
                    issued_ms=at,
                    nonce=nonce,
                ),
                body,
                allowed_peer_id=peer.node_id,
                ttl_ms=60_000,
                max_duration_ms=30_000,
                max_bytes_each_direction=1024,
                now=at,
            )

        first = reserve(peers[0], "a" * 24, now)
        refreshed = reserve(peers[0], "b" * 24, now + 1)
        assert refreshed.reservation_id == first.reservation_id
        assert refreshed.expires_ms == now + 60_001
        reserve(peers[1], "c" * 24, now + 1)
        with pytest.raises(OverflowError, match="quota"):
            reserve(peers[2], "d" * 24, now + 1)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class RunningAhub:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.stdout_chunks: list[str] = []
        self.stderr_chunks: list[str] = []
        self._readers = (
            threading.Thread(
                target=self._drain,
                args=(process.stdout, self.stdout_chunks),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, self.stderr_chunks),
                daemon=True,
            ),
        )
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _drain(stream, chunks: list[str]) -> None:
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            chunks.append(chunk)

    def poll(self) -> int | None:
        return self.process.poll()


def start_ahub(root: Path, port: int) -> RunningAhub:
    environment = {
        **os.environ,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONUTF8": "1",
    }
    running = RunningAhub(
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "anet",
                "ahub-serve",
                "--root",
                str(root),
                "--port",
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
    )
    deadline = time.monotonic() + 10
    health_url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if running.poll() is not None:
            stdout, stderr = stop_ahub(running)
            raise AssertionError(
                f"Ahub exited before ready\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return running
        except OSError:
            time.sleep(0.05)
    stop_ahub(running)
    raise AssertionError("Ahub did not become healthy")


def stop_ahub(running: RunningAhub) -> tuple[str, str]:
    if running.poll() is None:
        running.process.terminate()
    try:
        running.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        running.process.kill()
        running.process.wait(timeout=5)
    for reader in running._readers:
        reader.join(timeout=5)
    return "".join(running.stdout_chunks), "".join(running.stderr_chunks)


async def wait_for_reservation(
    client: AhubHTTPClient,
    owner_id: str,
    *,
    timeout: float = 10.0,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await asyncio.to_thread(
                client.relay_reservation,
                owner_id,
            )
        except AhubHTTPError:
            await asyncio.sleep(0.05)
    raise AssertionError("matching Relay reservation did not become available")


def test_live_relay_config_requires_explicit_peers_and_roundtrips(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="explicit peers"):
        AhubCarrierConfig(
            name="public",
            base_url="https://ahub.example",
            live_relay_enabled=True,
        )

    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    peer = Identity.generate("peer")
    carrier = AhubCarrierConfig.from_dict(
        {
            "type": "ahub",
            "name": "public",
            "base_url": "http://127.0.0.1:8422",
            "peers": [peer.node_id],
            "allow_insecure_http": True,
            "live_relay_enabled": True,
            "relay_reservation_ttl_seconds": 60,
            "relay_session_seconds": 30,
            "relay_bytes_each_direction": 1024 * 1024,
            "relay_listener_retry_seconds": 0.2,
        }
    )
    replace(config, ahub_carriers=(carrier,)).save()
    loaded = NodeConfig.load(config.home).ahub_carriers[0]
    assert loaded.live_relay_enabled
    assert loaded.peers == (peer.node_id,)
    assert loaded.relay_path_id == "ahub-relay:public"
    assert loaded.relay_reservation_ttl_seconds == 60
    assert loaded.relay_session_seconds == 30
    assert loaded.relay_bytes_each_direction == 1024 * 1024
    assert loaded.relay_listener_retry_seconds == 0.2


@pytest.mark.asyncio
async def test_live_relay_discovery_failure_falls_back_to_mailbox(
    tmp_path: Path,
) -> None:
    a_base = initialize_node(
        tmp_path / "a-fallback",
        label="a-fallback",
        listen_port=0,
    )
    b_base = initialize_node(
        tmp_path / "b-fallback",
        label="b-fallback",
        listen_port=0,
    )
    a_identity = Identity.load(a_base.identity_path)
    b_identity = Identity.load(b_base.identity_path)
    port = free_loopback_port()
    carrier = AhubCarrierConfig(
        name="public",
        base_url=f"http://127.0.0.1:{port}",
        mode="fallback",
        peers=(b_identity.node_id,),
        allow_insecure_http=True,
        retry_seconds=0.0,
        live_relay_enabled=True,
        relay_reservation_ttl_seconds=60.0,
        relay_session_seconds=30.0,
        relay_bytes_each_direction=1024 * 1024,
        relay_listener_retry_seconds=0.2,
    )
    a_config = replace(
        a_base,
        listen_enabled=False,
        direct_enabled=False,
        prekey_policy="disable",
        prekey_auto_enabled=False,
        ahub_carriers=(carrier,),
    )
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(
        b_identity.card(
            addresses=(),
            capabilities=b_base.capabilities,
        )
    )

    root = tmp_path / "ahub-fallback"
    with AhubService(root) as setup:
        setup.allow_node(a_identity.node_id)
        setup.allow_node(b_identity.node_id)

    process = start_ahub(root, port)
    node = AnetNode(a_config)
    secret = "mailbox-after-live-miss"
    delivery_paths: list[dict[str, object]] = []
    try:
        packet_id = node.queue(
            b_identity.node_id,
            kind="agent.message",
            body={"text": secret},
        )
        result = await node.adaptive_sync_once(
            force_carriers=True,
            skip_direct=True,
        )
        assert result["carriers"], result
        live = result["carriers"][0]["live_relay"]
        assert live["attempted"] is True
        assert live["connected"] is False
        assert live["error_category"] == "not_found"
        assert result["carriers"][0]["pushed_packets"] == 1
        delivery_paths = node.store.delivery_paths(packet_id)
    finally:
        node.close()
        stdout, stderr = stop_ahub(process)

    with AhubService(root) as observer:
        mailbox_ids = {
            str(row["packet_id"])
            for row in observer.ahub.connection.execute(
                "SELECT packet_id FROM ahub_mailbox"
            ).fetchall()
        }
    assert packet_id in mailbox_ids
    assert any(
        item["path_id"] == "ahub:public"
        and item["state"] == "custodied"
        for item in delivery_paths
    )
    logs = stdout + stderr
    assert secret not in logs
    assert a_identity.node_id not in logs
    assert b_identity.node_id not in logs


@pytest.mark.asyncio
async def test_real_process_relay_is_bidirectional_bounded_and_restart_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ahub"
    owner = Identity.generate("owner")
    peer = Identity.generate("peer")
    outsider = Identity.generate("outsider")
    now = int(time.time() * 1000)
    with AhubService(root) as setup:
        for identity in (owner, peer, outsider):
            provision(setup, identity, now=now)

    port = free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    owner_client = AhubHTTPClient(
        base_url, owner, allow_insecure_http=True, timeout_seconds=5
    )
    peer_client = AhubHTTPClient(
        base_url, peer, allow_insecure_http=True, timeout_seconds=5
    )
    outsider_client = AhubHTTPClient(
        base_url, outsider, allow_insecure_http=True, timeout_seconds=5
    )

    first_process = start_ahub(root, port)
    try:
        reservation = await asyncio.to_thread(
            owner_client.reserve_relay,
            peer.node_id,
            ttl_ms=60_000,
            max_duration_ms=30_000,
            max_bytes_each_direction=64,
        )
        discovered = await asyncio.to_thread(
            peer_client.relay_reservation,
            owner.node_id,
        )
        assert discovered == reservation
        with pytest.raises(AhubHTTPError) as hidden:
            await asyncio.to_thread(
                outsider_client.relay_reservation,
                owner.node_id,
            )
        assert hidden.value.category == "not_found"
    finally:
        first_stdout, first_stderr = stop_ahub(first_process)

    second_process = start_ahub(root, port)
    secret_a = b"owner-to-peer-secret"
    secret_b = b"peer-to-owner-secret"
    try:
        with pytest.raises(AhubHTTPError):
            await outsider_client.open_relay(reservation.reservation_id)

        owner_open = asyncio.create_task(
            owner_client.open_relay(reservation.reservation_id)
        )
        await asyncio.sleep(0.1)
        peer_connection = await peer_client.open_relay(
            reservation.reservation_id
        )
        owner_connection = await owner_open
        assert owner_connection.peer_node_id == peer.node_id
        assert peer_connection.peer_node_id == owner.node_id

        await owner_connection.send(secret_a)
        assert await peer_connection.receive() == secret_a
        await peer_connection.send(secret_b)
        assert await owner_connection.receive() == secret_b

        await owner_connection.send(b"x" * 45)
        with pytest.raises(ConnectionClosed):
            await peer_connection.receive()
        await owner_connection.close()
        await peer_connection.close()

        refreshed = await asyncio.to_thread(
            owner_client.reserve_relay,
            peer.node_id,
            ttl_ms=60_000,
            max_duration_ms=1_000,
            max_bytes_each_direction=64,
        )
        assert refreshed.reservation_id == reservation.reservation_id
        await asyncio.sleep(0.1)
        owner_open = asyncio.create_task(
            owner_client.open_relay(refreshed.reservation_id)
        )
        await asyncio.sleep(0.1)
        peer_connection = await peer_client.open_relay(
            refreshed.reservation_id
        )
        owner_connection = await owner_open
        with pytest.raises(ConnectionClosed):
            await peer_connection.receive()
        await owner_connection.close()
        await peer_connection.close()
    finally:
        second_stdout, second_stderr = stop_ahub(second_process)

    logs = first_stdout + first_stderr + second_stdout + second_stderr
    assert secret_a.decode() not in logs
    assert secret_b.decode() not in logs
    assert owner.node_id not in logs
    assert peer.node_id not in logs
    assert outsider.node_id not in logs
    assert "route=relay_reservation" in logs
    assert "route=relay_stream" in logs


@pytest.mark.asyncio
async def test_configured_relay_automatically_syncs_and_recovers_ahub_restart(
    tmp_path: Path,
) -> None:
    a_base = initialize_node(
        tmp_path / "a-auto",
        label="a-auto",
        listen_port=0,
    )
    b_base = initialize_node(
        tmp_path / "b-auto",
        label="b-auto",
        listen_port=0,
    )
    a_identity = Identity.load(a_base.identity_path)
    b_identity = Identity.load(b_base.identity_path)
    root = tmp_path / "ahub-auto"
    with AhubService(root) as setup:
        setup.allow_node(a_identity.node_id)
        setup.allow_node(b_identity.node_id)

    port = free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    common = {
        "name": "public",
        "base_url": base_url,
        "mode": "fallback",
        "interval": 0.5,
        "jitter": 0.0,
        "retry_seconds": 0.0,
        "allow_insecure_http": True,
        "live_relay_enabled": True,
        "relay_reservation_ttl_seconds": 60.0,
        "relay_session_seconds": 30.0,
        "relay_bytes_each_direction": 1024 * 1024,
        "relay_listener_retry_seconds": 0.2,
    }
    a_carrier = AhubCarrierConfig(
        peers=(b_identity.node_id,),
        **common,
    )
    b_carrier = AhubCarrierConfig(
        peers=(a_identity.node_id,),
        **common,
    )
    a_config = replace(
        a_base,
        listen_enabled=False,
        direct_enabled=False,
        prekey_policy="disable",
        prekey_auto_enabled=False,
        sync_interval=60.0,
        ahub_carriers=(a_carrier,),
    )
    b_config = replace(
        b_base,
        listen_enabled=False,
        direct_enabled=False,
        prekey_policy="disable",
        prekey_auto_enabled=False,
        sync_interval=60.0,
        ahub_carriers=(b_carrier,),
    )
    a_card = a_identity.card(
        addresses=(),
        capabilities=a_config.capabilities,
    )
    b_card = b_identity.card(
        addresses=(),
        capabilities=b_config.capabilities,
    )
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_card)
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_card)

    a_client = AhubHTTPClient(
        base_url,
        a_identity,
        allow_insecure_http=True,
        timeout_seconds=5,
    )
    first_process = start_ahub(root, port)
    second_process: RunningAhub | None = None
    first_stdout = ""
    first_stderr = ""
    second_stdout = ""
    second_stderr = ""
    a = AnetNode(a_config)
    b = AnetNode(b_config)
    first_secret = "automatic-relay-first"
    second_secret = "automatic-relay-after-restart"
    try:
        from anet.carriers.ahub import current_node_descriptor

        await asyncio.to_thread(
            a_client.publish_descriptor,
            current_node_descriptor(a),
        )
        await b.start()
        await wait_for_reservation(
            a_client,
            b_identity.node_id,
        )
        await asyncio.sleep(0.1)
        first_packet = a.queue(
            b_identity.node_id,
            kind="agent.message",
            body={"text": first_secret},
        )
        first_result = await a.adaptive_sync_once(
            force_carriers=True,
            skip_direct=True,
        )
        first_carrier = first_result["carriers"][0]
        assert first_carrier["live_relay"]["connected"] is True
        assert first_carrier["live_relay"]["path_id"] == (
            "ahub-relay:public"
        )
        assert any(
            item["packet_id"] == first_packet
            for item in b.store.list_inbox()
        )
        assert any(
            item["path_id"] == "ahub-relay:public"
            and item["state"] == "acked"
            for item in a.store.delivery_paths(first_packet)
        )

        first_stdout, first_stderr = stop_ahub(first_process)
        await asyncio.sleep(0.3)
        second_process = start_ahub(root, port)
        await wait_for_reservation(
            a_client,
            b_identity.node_id,
        )
        await asyncio.sleep(0.1)
        second_packet = a.queue(
            b_identity.node_id,
            kind="agent.message",
            body={"text": second_secret},
        )
        second_result = await a.adaptive_sync_once(
            force_carriers=True,
            skip_direct=True,
        )
        assert second_result["carriers"][0]["live_relay"][
            "connected"
        ] is True, second_result["carriers"][0]["live_relay"]["error_category"]
        assert any(
            item["packet_id"] == second_packet
            for item in b.store.list_inbox()
        )
        assert a.store.status()["pending"] == 0
        assert b.store.status()["pending"] == 0
        assert a.store.status()["receipts"] == 2
        with AhubService(root) as observer:
            mailbox_ids = {
                str(row["packet_id"])
                for row in observer.ahub.connection.execute(
                    "SELECT packet_id FROM ahub_mailbox"
                ).fetchall()
            }
            assert first_packet not in mailbox_ids
            assert second_packet not in mailbox_ids
        assert a._server is None
        assert b._server is None
    finally:
        await b.stop()
        a.close()
        b.close()
        if second_process is not None:
            second_stdout, second_stderr = stop_ahub(second_process)
        else:
            second_stdout, second_stderr = "", ""
            if first_process.poll() is None:
                first_stdout, first_stderr = stop_ahub(first_process)

    logs = (
        first_stdout
        + first_stderr
        + second_stdout
        + second_stderr
    )
    assert first_secret not in logs
    assert second_secret not in logs
    assert a_identity.node_id not in logs
    assert b_identity.node_id not in logs
    assert "route=relay_discovery" in logs
    assert "route=relay_stream" in logs


@pytest.mark.asyncio
async def test_disposable_nodes_run_existing_tls_sync_over_relay(
    tmp_path: Path,
) -> None:
    a_base = initialize_node(
        tmp_path / "a",
        label="a",
        listen_port=0,
    )
    b_base = initialize_node(
        tmp_path / "b",
        label="b",
        listen_port=0,
    )
    a_config = replace(
        a_base,
        listen_enabled=False,
        direct_enabled=False,
        prekey_policy="disable",
    )
    b_config = replace(
        b_base,
        listen_enabled=False,
        direct_enabled=False,
        prekey_policy="disable",
    )
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    a_card = a_identity.card(
        addresses=(),
        capabilities=a_config.capabilities,
    )
    b_card = b_identity.card(
        addresses=(),
        capabilities=b_config.capabilities,
    )
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_card)
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_card)

    root = tmp_path / "ahub"
    now = int(time.time() * 1000)
    with AhubService(root) as setup:
        provision(setup, a_identity, now=now)
        provision(setup, b_identity, now=now)

    port = free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    a_client = AhubHTTPClient(
        base_url,
        a_identity,
        allow_insecure_http=True,
        timeout_seconds=10,
    )
    b_client = AhubHTTPClient(
        base_url,
        b_identity,
        allow_insecure_http=True,
        timeout_seconds=10,
    )
    process = start_ahub(root, port)
    a = AnetNode(a_config)
    b = AnetNode(b_config)
    secret = "tls-over-relay-secret"
    path_id = "ahub-relay:test"
    try:
        reservation = await asyncio.to_thread(
            b_client.reserve_relay,
            a_identity.node_id,
            ttl_ms=60_000,
            max_duration_ms=30_000,
            max_bytes_each_direction=1024 * 1024,
        )
        packet_id = a.queue(
            b_identity.node_id,
            kind="agent.message",
            body={"text": secret},
        )
        listener = asyncio.create_task(
            b.serve_ahub_relay_once(
                b_client,
                reservation.reservation_id,
                path_id=path_id,
            )
        )
        await asyncio.sleep(0.1)
        await a.sync_ahub_relay_once(
            b_identity.node_id,
            a_client,
            reservation.reservation_id,
            path_id=path_id,
        )
        await listener

        inbox = b.store.list_inbox()
        received = next(
            item for item in inbox if item["packet_id"] == packet_id
        )
        assert received["trusted"] is True
        assert received["body"] == {"text": secret}
        assert a.store.status()["pending"] == 0
        assert b.store.status()["pending"] == 0
        assert a.store.status()["receipts"] == 1
        assert any(
            item["path_id"] == path_id and item["state"] == "acked"
            for item in a.store.delivery_paths(packet_id)
        )
        repeated_listener = asyncio.create_task(
            b.serve_ahub_relay_once(
                b_client,
                reservation.reservation_id,
                path_id=path_id,
            )
        )
        await asyncio.sleep(0.1)
        await a.sync_ahub_relay_once(
            b_identity.node_id,
            a_client,
            reservation.reservation_id,
            path_id=path_id,
        )
        await repeated_listener
        assert sum(
            item["packet_id"] == packet_id
            for item in b.store.list_inbox()
        ) == 1
        assert a._server is None
        assert b._server is None
        assert not a.config.listen_enabled
        assert not b.config.listen_enabled
        await asyncio.sleep(0)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("anet-relay-")
        ]
    finally:
        a.close()
        b.close()
        stdout, stderr = stop_ahub(process)

    logs = stdout + stderr
    assert secret not in logs
    assert a_identity.node_id not in logs
    assert b_identity.node_id not in logs
    assert "route=relay_stream" in logs
