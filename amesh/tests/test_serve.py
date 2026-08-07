from __future__ import annotations

import asyncio

import pytest

from amesh.adapters.loopback import (
    LOOPBACK_SIGNAL_KIND,
    LoopbackAdapter,
    LoopbackConfig,
    LoopbackLedger,
    loopback_database_path,
    loopback_key_path,
)
from amesh.serve import ServeLock, amesh_outbound_dir, serve
from amesh.signal import DirectorySignalSink, validate_signal
from amesh.policy import SocialPolicy, SocialThreshold

LOW_POLICY = SocialPolicy(
    surface=SocialThreshold(0, 0),
    reply=SocialThreshold(0, 0),
    amplify=SocialThreshold(0, 0),
    connect_candidate=SocialThreshold(0, 0, ("relationship:vouched",)),
)
DEST = "agent-main"


def _configure(home, *, destination: str = DEST) -> None:
    LoopbackConfig(
        channels=("lobby",),
        policy=LOW_POLICY,
        destination_id=destination,
        poll_interval_seconds=1.0,
    ).save(home)


def test_loopback_routes_signal(tmp_path) -> None:
    _configure(tmp_path)
    adapter = LoopbackAdapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh hello")
        captured = {}

        def queue_signal(destination_id, kind, body):
            captured["destination_id"] = destination_id
            captured["kind"] = kind
            captured["body"] = body
            return body["signal_id"]

        result = adapter.poll_once(queue_signal=queue_signal)
        assert result["routed"] == 1
        assert captured["destination_id"] == DEST
        assert captured["kind"] == LOOPBACK_SIGNAL_KIND
        signal = validate_signal(captured["body"])
        assert signal["provenance"]["platform"] == "loopback"
        assert signal["actor_key"] == captured["body"]["actor_key"]
        assert "interaction:mention" in signal["labels"]
    finally:
        adapter.close()


def test_loopback_metadata_event_routes_content_free(tmp_path) -> None:
    _configure(tmp_path)
    adapter = LoopbackAdapter(tmp_path)
    try:
        adapter.inject("bob", "just a normal message")
        captured = {}

        def queue_signal(destination_id, kind, body):
            captured["body"] = body
            return body["signal_id"]

        adapter.poll_once(queue_signal=queue_signal)
        assert captured["body"]["content_level"] == "metadata"
        assert captured["body"]["content"] == ""
    finally:
        adapter.close()


def test_loopback_routing_respects_permission_deny(tmp_path) -> None:
    _configure(tmp_path)
    adapter = LoopbackAdapter(tmp_path)
    try:
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            adapter.inject("alice", "@amesh hello")
            adapter.poll_once()
            actor_key = ledger.pseudonym("actor", "alice")
        finally:
            ledger.close()
        adapter._permissions.add_rule("loopback", actor_key, "surface", "deny")
        adapter.inject("alice", "@amesh again")
        routed = []

        def queue_signal(destination_id, kind, body):
            routed.append(body)
            return body["signal_id"]

        result = adapter.poll_once(queue_signal=queue_signal)
        assert result["routed"] == 0
        assert routed == []
        decisions = adapter._permissions.decisions(adapter="loopback")
        assert decisions and decisions[0]["action"] == "surface"
    finally:
        adapter.close()


def test_serve_hosts_loopback_and_emits_signals(tmp_path) -> None:
    _configure(tmp_path)
    adapter = LoopbackAdapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh hello from serve")
        adapter.inject("bob", "@amesh me too")
    finally:
        adapter.close()

    async def _run() -> dict:
        stop = asyncio.Event()
        task = asyncio.create_task(
            serve(tmp_path, names=("loopback", "discord"), stop=stop)
        )
        await asyncio.sleep(1.6)
        stop.set()
        return await task

    result = asyncio.run(_run())
    assert result["hosted"] == ["loopback"]
    assert result["signal_count"] == 2
    sink = DirectorySignalSink(amesh_outbound_dir(tmp_path))
    signals = sink.list(platform="loopback")
    assert len(signals) == 2
    assert all(s["provenance"]["adapter"] == "loopback-spool-v1" for s in signals)


def test_serve_stops_cleanly_when_nothing_configured(tmp_path) -> None:
    async def _run() -> dict:
        stop = asyncio.Event()
        stop.set()
        return await serve(tmp_path, names=("loopback", "discord"), stop=stop)

    result = asyncio.run(_run())
    assert result["hosted"] == []
    assert result["signal_count"] == 0


def test_invalid_destination_node_id(tmp_path) -> None:
    with pytest.raises(ValueError):
        LoopbackConfig(destination_id="not a target")


def test_serve_lock_prevents_second_owner(tmp_path) -> None:
    first = ServeLock(tmp_path)
    first.acquire()
    try:
        second = ServeLock(tmp_path)
        with pytest.raises(RuntimeError, match="already holds"):
            second.acquire()
        assert second._descriptor is None
    finally:
        first.release()
    reacquire = ServeLock(tmp_path)
    reacquire.acquire()
    reacquire.release()


def test_serve_lock_context_manager(tmp_path) -> None:
    with ServeLock(tmp_path):
        with pytest.raises(RuntimeError, match="already holds"):
            ServeLock(tmp_path).acquire()
    ServeLock(tmp_path).acquire()
    ServeLock(tmp_path).release()


def test_serve_rejects_second_supervisor_on_same_home(tmp_path) -> None:
    async def _run() -> None:
        first = ServeLock(tmp_path)
        first.acquire()
        try:
            stop = asyncio.Event()
            stop.set()
            with pytest.raises(RuntimeError, match="already holds"):
                await serve(tmp_path, names=(), stop=stop)
        finally:
            first.release()

    asyncio.run(_run())
