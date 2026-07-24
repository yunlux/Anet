from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from anet.ahub import AhubService
from anet.ahub_http import AhubHTTPClient
from anet.control_plane import issue_node_descriptor
from anet.identity import Identity
from anet.packet import open_packet, seal_packet


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_ahub(root: Path, port: int) -> subprocess.Popen[str]:
    environment = {
        **os.environ,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONUTF8": "1",
    }
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
        env=environment,
    )
    deadline = time.monotonic() + 10
    health_url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"Ahub exited before ready\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return process
        except OSError:
            time.sleep(0.05)
    stop_ahub(process)
    raise AssertionError("Ahub did not become healthy")


def stop_ahub(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def test_real_http_process_restart_preserves_offline_ciphertext(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ahub"
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    with AhubService(root) as setup:
        setup.allow_node(sender.node_id)
        setup.allow_node(recipient.node_id)

    port = free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    sender_client = AhubHTTPClient(
        base_url, sender, allow_insecure_http=True
    )
    recipient_client = AhubHTTPClient(
        base_url, recipient, allow_insecure_http=True
    )
    secret = "process-restart-secret"
    raw = seal_packet(
        sender,
        recipient.card(),
        kind="agent.message",
        body={"text": secret},
        ttl_seconds=60,
    )

    first = start_ahub(root, port)
    try:
        for identity, client in (
            (sender, sender_client),
            (recipient, recipient_client),
        ):
            descriptor = issue_node_descriptor(
                identity,
                capabilities=("agent.task",),
                ttl_ms=60 * 60 * 1000,
            )
            assert client.publish_descriptor(descriptor)
        custody = sender_client.submit(raw)
        assert custody.stored
        with AhubService(root) as observer:
            assert observer.status()["mailbox_packets"] == 1
    finally:
        first_stdout, first_stderr = stop_ahub(first)

    second = start_ahub(root, port)
    try:
        claims = recipient_client.claim(limit=1, lease_ms=5_000)
        assert len(claims) == 1
        assert claims[0].raw == raw
        opened = open_packet(recipient, claims[0].raw)
        assert opened.body == {"text": secret}
        assert recipient_client.settle_claim(claims[0])
    finally:
        second_stdout, second_stderr = stop_ahub(second)

    with AhubService(root) as observer:
        assert observer.status()["mailbox_packets"] == 0
        assert observer.health()

    logs = first_stdout + first_stderr + second_stdout + second_stderr
    assert secret not in logs
    assert sender.node_id not in logs
    assert recipient.node_id not in logs
    assert "route=mailbox_submit" in logs
    assert "route=mailbox_claim" in logs
