from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from anet.cli import main
from anet.config import (
    DirectDialerConfig,
    NodeConfig,
    StdioDialerConfig,
    initialize_node,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


ADAPTER_SOURCE = r"""
import os
import socket
import sys
import threading

target = os.environ["ANET_TARGET_HOST"]
port = int(os.environ["ANET_TARGET_PORT"])
pid_path = sys.argv[1]
env_path = sys.argv[2]
with open(pid_path, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
with open(env_path, "w", encoding="utf-8") as handle:
    handle.write("allowed=" + os.environ.get("ANET_TEST_ALLOWED", ""))
    handle.write("\nblocked=" + os.environ.get("ANET_TEST_BLOCKED", ""))

sock = socket.create_connection((target, port), timeout=5)
sock.settimeout(None)

def stdin_to_socket():
    try:
        while True:
            data = os.read(sys.stdin.fileno(), 65536)
            if not data:
                break
            sock.sendall(data)
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

thread = threading.Thread(target=stdin_to_socket, daemon=True)
thread.start()
try:
    while True:
        data = sock.recv(65536)
        if not data:
            break
        os.write(sys.stdout.fileno(), data)
finally:
    sock.close()
"""

SLOW_ADAPTER_SOURCE = r"""
import os
import sys
import time

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
time.sleep(30)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_adapter(tmp_path: Path) -> Path:
    path = tmp_path / "stdio_adapter.py"
    path.write_text(ADAPTER_SOURCE, encoding="utf-8")
    return path


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_stdio_dialer_config_round_trip_and_rejects_shell_input(
    tmp_path: Path,
) -> None:
    executable = Path(sys.executable).resolve()
    stdio = StdioDialerConfig.from_dict(
        {
            "executable": str(executable),
            "args": ["adapter.py", "--fixed"],
            "env": ["ANET_TEST_ALLOWED"],
            "startup_timeout": 3.0,
        }
    )
    config = replace(
        initialize_node(tmp_path / "node", label="node"),
        direct_dialers=(
            DirectDialerConfig(name="radio", priority=5, stdio=stdio),
        ),
    )
    config.save()
    loaded = NodeConfig.load(config.home)
    assert loaded.direct_dialers == config.direct_dialers
    assert loaded.direct_dialers[0].kind == "stdio"
    assert loaded.direct_dialers[0].to_dict()["executable"] == str(executable)

    with pytest.raises(ValueError, match="absolute"):
        StdioDialerConfig.from_dict({"executable": "python"})
    with pytest.raises(ValueError, match="newline"):
        StdioDialerConfig.from_dict(
            {"executable": str(executable), "args": ["ok\nmalicious"]}
        )
    with pytest.raises(ValueError, match="environment variable"):
        StdioDialerConfig.from_dict(
            {"executable": str(executable), "env": ["BAD-NAME"]}
        )


def test_cli_adds_stdio_dialer_without_exposing_environment_values(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    home = tmp_path / "cli"
    assert main(["--home", str(home), "init", "--label", "cli"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("ANET_TEST_ALLOWED", "must-not-be-printed")
    assert main(
        [
            "--home",
            str(home),
            "dialer-add",
            "serial-bridge",
            "--type",
            "stdio",
            "--executable",
            str(Path(sys.executable).resolve()),
            "--arg",
            "adapter.py",
            "--env",
            "ANET_TEST_ALLOWED",
            "--startup-timeout",
            "4",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "must-not-be-printed" not in output
    added = json.loads(output)
    assert added["added"]["type"] == "stdio"
    assert added["added"]["env"] == ["ANET_TEST_ALLOWED"]


def test_stdio_dialer_delivers_over_external_byte_stream_and_cleans_process(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        port = _free_port()
        adapter = _write_adapter(tmp_path)
        pid_path = tmp_path / "adapter.pid"
        env_path = tmp_path / "adapter.env"
        monkeypatch.setenv("ANET_TEST_ALLOWED", "visible-to-adapter")
        monkeypatch.setenv("ANET_TEST_BLOCKED", "must-stay-hidden")

        a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
        b_config = initialize_node(
            tmp_path / "b",
            label="b",
            listen_host="127.0.0.1",
            listen_port=port,
        )
        stdio = StdioDialerConfig.from_dict(
            {
                "executable": str(Path(sys.executable).resolve()),
                "args": [str(adapter), str(pid_path), str(env_path)],
                "env": ["ANET_TEST_ALLOWED"],
                "startup_timeout": 5,
            }
        )
        a_config = replace(
            a_config,
            direct_dialers=(DirectDialerConfig(name="stdio", stdio=stdio),),
        )
        a_config.save()
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
        b_card = b_identity.card(
            addresses=b_config.effective_addresses(),
            capabilities=b_config.capabilities,
        )
        PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            packet_id = a.queue(
                b_identity.node_id,
                kind="evidence",
                body={"path": "external-stdio"},
            )
            reverse_packet_id = b.queue(
                a_identity.node_id,
                kind="result",
                body={"path": "external-stdio-reverse"},
            )
            assert await a._sync_peer(b_card) is True
            inbox = [
                item for item in b.store.list_inbox()
                if item["packet_id"] == packet_id
            ]
            assert len(inbox) == 1
            assert inbox[0]["trusted"] is True
            reverse_inbox = [
                item for item in a.store.list_inbox()
                if item["packet_id"] == reverse_packet_id
            ]
            assert len(reverse_inbox) == 1
            assert reverse_inbox[0]["trusted"] is True
            deadline = time.monotonic() + 2
            while (
                not any(
                    item["state"] == "acked"
                    for item in b.store.delivery_paths(reverse_packet_id)
                )
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.02)
            assert any(
                item["state"] == "acked"
                for item in b.store.delivery_paths(reverse_packet_id)
            )
            assert a.peer_state[b_identity.node_id]["dialer"] == "stdio"
            assert env_path.read_text(encoding="utf-8") == (
                "allowed=visible-to-adapter\nblocked="
            )
            pid = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while _pid_alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not _pid_alive(pid)
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_losing_stdio_race_is_cancelled_without_an_orphan_process(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        port = _free_port()
        adapter = tmp_path / "slow_adapter.py"
        adapter.write_text(SLOW_ADAPTER_SOURCE, encoding="utf-8")
        pid_path = tmp_path / "slow.pid"
        a_config = initialize_node(tmp_path / "race-a", label="a", listen_port=0)
        b_config = initialize_node(
            tmp_path / "race-b",
            label="b",
            listen_host="127.0.0.1",
            listen_port=port,
        )
        stdio = StdioDialerConfig.from_dict(
            {
                "executable": str(Path(sys.executable).resolve()),
                "args": [str(adapter), str(pid_path)],
                "startup_timeout": 5,
            }
        )
        a_config = replace(
            a_config,
            direct_dialers=(
                DirectDialerConfig(name="a-slow-stdio", priority=0, stdio=stdio),
                DirectDialerConfig(name="z-fast-raw", priority=0),
            ),
            routing=replace(
                a_config.routing,
                direct_race_width=2,
                direct_race_delay=0.15,
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
        PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(b_card)
        PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(a_card)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        try:
            await b.start()
            assert await a._sync_peer(b_card) is True
            assert a.peer_state[b_identity.node_id]["dialer"] == "z-fast-raw"
            assert pid_path.exists()
            pid = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while _pid_alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not _pid_alive(pid)
            assert a.store.path_metric(
                b_identity.node_id,
                f"direct:a-slow-stdio:{b_card.addresses[0]}",
            ) is None
        finally:
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_stdio_dialer_classifies_early_adapter_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = tmp_path / "exit_adapter.py"
        adapter.write_text("raise SystemExit(7)\n", encoding="utf-8")
        config = initialize_node(tmp_path / "exit-node", label="node")
        stdio = StdioDialerConfig.from_dict(
            {
                "executable": str(Path(sys.executable).resolve()),
                "args": [str(adapter)],
            }
        )
        config = replace(
            config,
            direct_dialers=(DirectDialerConfig(name="exit", stdio=stdio),),
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
            assert result["results"][0]["category"] == "adapter_exit"
        finally:
            node.close()

    asyncio.run(scenario())


def test_stdio_dialer_classifies_spawn_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = initialize_node(tmp_path / "node", label="node")
        stdio = StdioDialerConfig.from_dict(
            {"executable": str((tmp_path / "missing-adapter").resolve())}
        )
        config = replace(
            config,
            direct_dialers=(DirectDialerConfig(name="missing", stdio=stdio),),
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
            assert result["results"][0]["category"] == "adapter_spawn"
        finally:
            node.close()

    asyncio.run(scenario())
