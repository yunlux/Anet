from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest

import anet.supervisor_health as supervisor_health
from anet.cli import main
from anet.config import initialize_node
from anet.continuity import (
    CONTINUITY_CHALLENGE_KIND,
    CONTINUITY_PREPARED_KIND,
    CONTINUITY_RECEIPT_KIND,
    ContinuityError,
    prepare_continuity,
    verify_continuity,
)
from anet.remote_control import run_supervisor, write_control_settings
from anet.supervisor_health import SupervisorHealthReporter, inspect_supervisor_health


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _node_home(tmp_path: Path, name: str = "node") -> Path:
    node = initialize_node(
        tmp_path / name,
        label=name,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
    )
    return node.home


def _healthy(home: Path, *, sequence: int = 1) -> SupervisorHealthReporter:
    reporter = SupervisorHealthReporter(home)
    reporter.synced({"sequence": sequence}, poll_seconds=30)
    reporter.child_running(os.getpid())
    return reporter


async def _wait_healthy(home: Path) -> dict[str, object]:
    for _ in range(100):
        health = inspect_supervisor_health(home)
        if health["ok"]:
            return health
        await asyncio.sleep(0.05)
    pytest.fail(f"supervisor did not become healthy: {health}")


def test_prepare_requires_current_healthy_supervisor(tmp_path: Path) -> None:
    home = _node_home(tmp_path)

    with pytest.raises(ContinuityError, match="not healthy before"):
        prepare_continuity(home)


def test_prepare_creates_private_one_time_challenge(tmp_path: Path) -> None:
    home = _node_home(tmp_path)
    _healthy(home)

    prepared = prepare_continuity(home)
    challenge_path = Path(str(prepared["challenge_path"]))
    challenge = json.loads(challenge_path.read_text(encoding="utf-8"))

    assert prepared["kind"] == CONTINUITY_PREPARED_KIND
    assert prepared["ok"] is True
    assert challenge["kind"] == CONTINUITY_CHALLENGE_KIND
    assert set(challenge["protected_hashes"]) == {
        "identity.json",
        "tls-key.pem",
        "tls-cert.pem",
    }
    if os.name != "nt":
        assert challenge_path.stat().st_mode & 0o777 == 0o600


def test_prepare_rejects_mismatched_tls_baseline(tmp_path: Path) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    (home / "tls-key.pem").write_bytes(b"invalid-key")

    with pytest.raises(ContinuityError, match="TLS identity is invalid before"):
        prepare_continuity(home)


def test_service_restart_preserves_identity_and_consumes_challenge(
    tmp_path: Path,
) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)
    challenge_copy = tmp_path / "copied-challenge.json"
    challenge_copy.write_bytes(Path(str(prepared["challenge_path"])).read_bytes())

    _healthy(home, sequence=2)
    receipt = verify_continuity(home, Path(str(prepared["challenge_path"])))

    assert receipt["kind"] == CONTINUITY_RECEIPT_KIND
    assert receipt["ok"] is True
    assert receipt["mode"] == "supervisor-restart"
    assert receipt["identity_preserved"] is True
    assert receipt["supervisor_restarted"] is True
    assert receipt["boot_session_changed"] is False
    assert (
        prepared["supervisor_instance_id"]
        != receipt["current_supervisor_instance_id"]
    )
    assert Path(receipt["receipt_path"]).is_file()

    with pytest.raises(ContinuityError, match="already been consumed"):
        verify_continuity(home, challenge_copy)


def test_verify_rejects_same_supervisor_instance(tmp_path: Path) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)

    with pytest.raises(ContinuityError, match="instance did not change"):
        verify_continuity(home, Path(str(prepared["challenge_path"])))


def test_verify_rejects_changed_private_identity_material(tmp_path: Path) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)
    (home / "tls-key.pem").write_bytes(b"replaced-key")
    _healthy(home, sequence=2)

    with pytest.raises(ContinuityError, match="identity material changed"):
        verify_continuity(home, Path(str(prepared["challenge_path"])))


def test_verify_rejects_expired_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)
    challenge_path = Path(str(prepared["challenge_path"]))
    expires_at_ms = int(prepared["expires_at_ms"])
    monkeypatch.setattr("anet.continuity._now_ms", lambda: expires_at_ms + 1)
    _healthy(home, sequence=2)

    with pytest.raises(ContinuityError, match="expired"):
        verify_continuity(home, challenge_path)


def test_verify_rejects_tampered_challenge(tmp_path: Path) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)
    challenge_path = Path(str(prepared["challenge_path"]))
    challenge = json.loads(challenge_path.read_text(encoding="utf-8"))
    challenge["expires_at_ms"] += 1
    challenge_path.write_text(json.dumps(challenge), encoding="utf-8")
    _healthy(home, sequence=2)

    with pytest.raises(ContinuityError, match="signature is invalid"):
        verify_continuity(home, challenge_path)


def test_device_restart_requires_a_new_boot_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    prepared = prepare_continuity(home)
    challenge_path = Path(str(prepared["challenge_path"]))
    _healthy(home, sequence=2)

    with pytest.raises(ContinuityError, match="boot session did not change"):
        verify_continuity(home, challenge_path, require_boot_change=True)

    monkeypatch.setattr(
        supervisor_health,
        "current_boot_session_id",
        lambda: "test:new-boot-session",
    )
    _healthy(home, sequence=3)
    receipt = verify_continuity(home, challenge_path, require_boot_change=True)

    assert receipt["mode"] == "device-restart"
    assert receipt["boot_session_changed"] is True


def test_challenge_cannot_be_verified_for_another_home(tmp_path: Path) -> None:
    first = _node_home(tmp_path, "first")
    second = _node_home(tmp_path, "second")
    _healthy(first)
    _healthy(second)
    prepared = prepare_continuity(first)
    _healthy(second, sequence=2)

    with pytest.raises(ContinuityError, match="another node home"):
        verify_continuity(second, Path(str(prepared["challenge_path"])))


def test_continuity_cli_round_trip(tmp_path: Path, capsys) -> None:
    home = _node_home(tmp_path)
    _healthy(home)
    challenge = tmp_path / "cli-challenge.json"

    assert (
        main(
            [
                "--home",
                str(home),
                "continuity-prepare",
                "--out",
                str(challenge),
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["kind"] == CONTINUITY_PREPARED_KIND

    _healthy(home, sequence=2)
    assert (
        main(["--home", str(home), "continuity-verify", str(challenge)])
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["kind"] == CONTINUITY_RECEIPT_KIND


@pytest.mark.asyncio
async def test_real_supervisor_and_child_restart_satisfies_continuity(
    tmp_path: Path,
) -> None:
    home = _node_home(tmp_path)
    page = tmp_path / "control.json"
    page.write_text(
        json.dumps({"version": 1, "sequence": 1, "config": {}}),
        encoding="utf-8",
    )
    write_control_settings(home, url=page.as_uri(), interval=5)

    first_task = asyncio.create_task(run_supervisor(home, interval=5))
    try:
        first_health = await _wait_healthy(home)
        prepared = prepare_continuity(home)
    finally:
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

    second_task = asyncio.create_task(run_supervisor(home, interval=5))
    try:
        second_health = await _wait_healthy(home)
        receipt = verify_continuity(
            home,
            Path(str(prepared["challenge_path"])),
        )
        assert receipt["ok"] is True
        assert receipt["mode"] == "supervisor-restart"
        assert first_health["instance_id"] != second_health["instance_id"]
    finally:
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task
