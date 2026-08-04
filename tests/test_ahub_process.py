from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
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
