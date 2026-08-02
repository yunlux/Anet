from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from anet.ahub import AhubService
from anet.ahub_http import AhubHTTPClient
from anet.config import AhubCarrierConfig, initialize_node
from anet.carriers.ahub import (
    current_node_descriptor,
    current_node_reachability,
    sync_ahub_once,
)
from anet.node import AnetNode
from anet.peers import PeerBook


def trust_each_other(a: AnetNode, b: AnetNode) -> None:
    a_card = a.identity.card(addresses=(), capabilities=a.config.capabilities)
    b_card = b.identity.card(addresses=(), capabilities=b.config.capabilities)
    PeerBook(a.config.peers_path, own_node_id=a.node_id).add(b_card)
    PeerBook(b.config.peers_path, own_node_id=b.node_id).add(a_card)
    a.peers.reload()
    b.peers.reload()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_server(root: Path, port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
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
        env={
            **os.environ,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONUTF8": "1",
        },
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"Ahub exited\n{stdout}\n{stderr}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=0.5
            ) as response:
                if response.status == 200:
                    return process
        except OSError:
            time.sleep(0.05)
    stop_server(process)
    raise AssertionError("Ahub did not become ready")


def stop_server(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def test_ahub_store_carrier_preserves_all_ack_layers_across_restart(
    tmp_path: Path,
) -> None:
    port = free_port()
    config = AhubCarrierConfig(
        name="public",
        base_url=f"http://127.0.0.1:{port}",
        retry_seconds=0,
        allow_insecure_http=True,
    )
    a_config = replace(
        initialize_node(tmp_path / "a", label="a", listen_port=0),
        listen_enabled=False,
        direct_enabled=False,
        ahub_carriers=(config,),
    )
    b_config = replace(
        initialize_node(tmp_path / "b", label="b", listen_port=0),
        listen_enabled=False,
        direct_enabled=False,
        ahub_carriers=(config,),
    )
    a_config.save()
    b_config.save()
    a = AnetNode(a_config)
    b = AnetNode(b_config)
    trust_each_other(a, b)
    ahub_root = tmp_path / "ahub"
    with AhubService(ahub_root) as service:
        service.allow_node(a.node_id)
        service.allow_node(b.node_id)
    path_id = config.path_id
    secret = "ahub-carrier-secret"
    process = start_server(ahub_root, port)
    logs: list[str] = []
    try:
        packet_id = a.queue(
            b.node_id,
            kind="intent",
            body={"objective": secret},
        )
        first_round = asyncio.run(
            a.adaptive_sync_once(skip_direct=True)
        )
        first = first_round["carriers"][0]
        assert first["pushed_packets"] == 1
        control_client = AhubHTTPClient(
            config.base_url,
            a.identity,
            allow_insecure_http=True,
        )
        _descriptor, reachability = control_client.lookup(a.node_id)
        assert reachability is not None
        assert reachability.protocol_versions == ("anet/1",)
        assert reachability.sequence == 1
        assert (a.config.home / "reachability-state.json").exists()
        assert not a.store.packet_delivered(packet_id)
        assert (
            a.store.delivery_path_state(packet_id, b.node_id, path_id)
            == "custodied"
        )
        assert a.store.pending_for_peer(
            b.node_id, retry_after_ms=0, path_id=path_id
        ) == []
        assert any(
            item["packet_id"] == packet_id
            for item in a.store.pending_for_peer(
                b.node_id, retry_after_ms=0
            )
        )

        stdout, stderr = stop_server(process)
        logs.extend((stdout, stderr))
        process = start_server(ahub_root, port)

        second = asyncio.run(
            b.adaptive_sync_once(skip_direct=True)
        )["carriers"][0]
        assert second["pulled_packets"] == 1
        assert second["settled_packets"] == 1
        assert second["pushed_packets"] == 1
        inbox = [
            item
            for item in b.store.list_inbox()
            if item["packet_id"] == packet_id
        ]
        assert len(inbox) == 1
        assert inbox[0]["trusted"] is True
        assert inbox[0]["body"] == {"objective": secret}

        third = asyncio.run(
            a.adaptive_sync_once(skip_direct=True)
        )["carriers"][0]
        assert third["pulled_acks"] == 1
        assert third["pulled_packets"] == 1
        assert a.store.packet_delivered(packet_id)
        assert a.store.receipt(packet_id)["recipient_id"] == b.node_id

        fourth = asyncio.run(
            b.adaptive_sync_once(skip_direct=True)
        )["carriers"][0]
        assert fourth["pulled_acks"] == 1
        assert a.store.status()["pending"] == 0
        assert b.store.status()["pending"] == 0

        repeated = asyncio.run(
            a.adaptive_sync_once(skip_direct=True)
        )["carriers"][0]
        assert repeated["pulled_packets"] == 0
        assert repeated["pulled_acks"] == 0
        assert len(
            [
                item
                for item in b.store.list_inbox()
                if item["packet_id"] == packet_id
            ]
        ) == 1

        control_state = (
            a.config.home / "control-state.json"
        ).read_text(encoding="utf-8")
        assert "private" not in control_state.lower()
        assert secret not in control_state
    finally:
        stdout, stderr = stop_server(process)
        logs.extend((stdout, stderr))
        a.close()
        b.close()

    combined_logs = "".join(logs)
    assert secret not in combined_logs
    assert a.node_id not in combined_logs
    assert b.node_id not in combined_logs


def test_reachability_checkpoint_retries_without_skipping_on_publish_failure(
    tmp_path: Path,
) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_host="192.0.2.20",
        listen_port=43121,
    )
    node = AnetNode(config)
    try:
        descriptor = current_node_descriptor(node)
        carrier = AhubCarrierConfig(
            name="test",
            base_url="https://example.invalid",
        )
        with (
            patch.object(AhubHTTPClient, "publish_descriptor", return_value=True),
            patch.object(
                AhubHTTPClient,
                "publish_reachability",
                side_effect=OSError("simulated publish failure"),
            ),
        ):
            with pytest.raises(OSError, match="simulated publish failure"):
                sync_ahub_once(node, carrier)

        first = current_node_reachability(node, descriptor)
        assert first.sequence == 1
        assert first.candidates == ("tls://192.0.2.20:43121",)
        retry = current_node_reachability(node, descriptor)
        assert retry == first
        assert not (config.home / "reachability-state.json").exists()

        with (
            patch.object(AhubHTTPClient, "publish_descriptor", return_value=True),
            patch.object(
                AhubHTTPClient,
                "publish_reachability",
                return_value=True,
            ),
        ):
            stats = sync_ahub_once(node, carrier)
        assert stats["reachability_sequence"] == 1
        assert (config.home / "reachability-state.json").exists()

        restarted = AnetNode(config)
        try:
            next_descriptor = current_node_descriptor(restarted)
            next_record = current_node_reachability(restarted, next_descriptor)
            assert next_record.sequence == 2
            assert next_record.previous_digest == first.digest
            assert next_record.session_id != first.session_id
        finally:
            restarted.close()
    finally:
        node.close()
