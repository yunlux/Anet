from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest

import anet.remote_control as remote_control
from anet.cli import main
from anet.config import initialize_node
from anet.remote_control import SupervisorLock, run_supervisor, write_control_settings
from anet.supervisor_health import (
    SUPERVISOR_HEALTH_KIND,
    SUPERVISOR_HEALTH_NAME,
    SupervisorHealthReporter,
    inspect_supervisor_health,
)


def test_missing_health_is_machine_readable_and_unhealthy(tmp_path: Path) -> None:
    result = inspect_supervisor_health(tmp_path, now_ms=1_000)

    assert result["kind"] == SUPERVISOR_HEALTH_KIND
    assert result["ok"] is False
    assert result["state"] == "missing"


def test_reporter_exposes_fresh_running_supervisor_and_child(tmp_path: Path) -> None:
    reporter = SupervisorHealthReporter(tmp_path, now_ms=1_000)
    reporter.synced({"sequence": 7}, poll_seconds=15, now_ms=2_000)
    reporter.child_running(os.getpid(), now_ms=3_000)

    result = inspect_supervisor_health(tmp_path, now_ms=4_000)

    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["last_sequence"] == 7
    assert result["supervisor_process_alive"] is True
    assert result["child_process_alive"] is True
    assert result["fresh"] is True


def test_stale_heartbeat_fails_closed(tmp_path: Path) -> None:
    reporter = SupervisorHealthReporter(tmp_path, now_ms=1_000)
    reporter.synced({"sequence": 1}, poll_seconds=5, now_ms=2_000)
    reporter.child_running(os.getpid(), now_ms=3_000)

    result = inspect_supervisor_health(tmp_path, now_ms=38_001)

    assert result["ok"] is False
    assert result["fresh"] is False
    assert result["reason"] == "supervisor heartbeat is stale"


def test_dead_child_fails_even_when_reported_running(tmp_path: Path) -> None:
    reporter = SupervisorHealthReporter(tmp_path, now_ms=1_000)
    reporter.synced({"sequence": 1}, poll_seconds=30, now_ms=2_000)
    reporter.child_running(2_147_483_647, now_ms=3_000)

    result = inspect_supervisor_health(tmp_path, now_ms=4_000)

    assert result["ok"] is False
    assert result["child_process_alive"] is False
    assert result["reason"] == "Anet server child is not running"


def test_degraded_and_stopped_transitions_are_durable(tmp_path: Path) -> None:
    reporter = SupervisorHealthReporter(tmp_path, now_ms=1_000)
    reporter.degraded("  remote\n page failed  ", child_pid=os.getpid(), now_ms=2_000)

    degraded = inspect_supervisor_health(tmp_path, now_ms=3_000)
    assert degraded["ok"] is False
    assert degraded["state"] == "degraded"
    assert degraded["last_error"] == "remote page failed"
    assert degraded["consecutive_failures"] == 1

    reporter.stopped(now_ms=4_000)
    stopped = inspect_supervisor_health(tmp_path, now_ms=5_000)
    assert stopped["ok"] is False
    assert stopped["state"] == "stopped"
    assert stopped["stopped_at_ms"] == 4_000


def test_invalid_health_document_fails_closed(tmp_path: Path) -> None:
    (tmp_path / SUPERVISOR_HEALTH_NAME).write_text("[]", encoding="utf-8")

    result = inspect_supervisor_health(tmp_path, now_ms=1_000)

    assert result["ok"] is False
    assert result["state"] == "invalid"


def test_supervisor_status_cli_uses_health_as_exit_gate(
    tmp_path: Path, capsys
) -> None:
    assert main(["--home", str(tmp_path), "supervisor-status"]) == 1
    missing = json.loads(capsys.readouterr().out)
    assert missing["state"] == "missing"

    reporter = SupervisorHealthReporter(tmp_path)
    reporter.synced({"sequence": 4}, poll_seconds=30)
    reporter.child_running(os.getpid())

    assert main(["--home", str(tmp_path), "supervisor-status"]) == 0
    healthy = json.loads(capsys.readouterr().out)
    assert healthy["ok"] is True


def test_health_document_is_private_on_posix(tmp_path: Path) -> None:
    SupervisorHealthReporter(tmp_path)
    mode = (tmp_path / SUPERVISOR_HEALTH_NAME).stat().st_mode & 0o777

    if os.name != "nt":
        assert mode == 0o600


@pytest.mark.asyncio
async def test_real_supervisor_publishes_and_closes_health_lifecycle(
    tmp_path: Path,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    node = initialize_node(
        tmp_path / "node",
        label="health-integration",
        listen_host="127.0.0.1",
        listen_port=port,
    )
    page = tmp_path / "control.json"
    page.write_text(
        json.dumps({"version": 1, "sequence": 1, "config": {}}),
        encoding="utf-8",
    )
    write_control_settings(node.home, url=page.as_uri(), interval=5)

    task = asyncio.create_task(run_supervisor(node.home, interval=5))
    try:
        for _ in range(100):
            health = inspect_supervisor_health(node.home)
            if health["ok"]:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"supervisor did not become healthy: {health}")
        assert health["last_sequence"] == 1
        assert health["child_process_alive"] is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    stopped = inspect_supervisor_health(node.home)
    assert stopped["ok"] is False
    assert stopped["state"] == "stopped"
    assert stopped["child_process_alive"] is False


@pytest.mark.asyncio
async def test_health_start_failure_releases_the_home_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "node"
    home.mkdir()

    class FailingReporter:
        def __init__(self, _home: Path) -> None:
            raise OSError("health storage unavailable")

    monkeypatch.setattr(remote_control, "SupervisorHealthReporter", FailingReporter)
    with pytest.raises(OSError, match="health storage unavailable"):
        await run_supervisor(home)

    with SupervisorLock(home):
        pass
