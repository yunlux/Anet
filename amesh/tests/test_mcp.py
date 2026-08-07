from __future__ import annotations

import asyncio
import json

import pytest

from amesh.adapters.loopback import (
    LoopbackAdapter,
    LoopbackConfig,
    LoopbackLedger,
    loopback_database_path,
    loopback_key_path,
)
from amesh.mcp_server import (
    amesh_adapters,
    amesh_permit_add,
    amesh_permit_list,
    amesh_relations,
    amesh_relations_circle,
    amesh_social_actor,
    amesh_social_inject,
    amesh_social_poll,
    amesh_social_project,
    amesh_social_reply,
    amesh_social_signals,
)
from amesh.serve import amesh_outbound_dir
from amesh.signal import DirectorySignalSink
from amesh.policy import SocialPolicy, SocialThreshold

LOW_POLICY = SocialPolicy(
    surface=SocialThreshold(0, 0),
    reply=SocialThreshold(0, 0),
    amplify=SocialThreshold(0, 0),
    connect_candidate=SocialThreshold(0, 0, ("relationship:vouched",)),
)
DEST = "agent-main"


def _node_home(tmp_path):
    return tmp_path / "node"


def test_mcp_reply_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AMESH_MCP_ALLOW_REPLY", raising=False)

    async def scenario() -> None:
        with pytest.raises(PermissionError, match="outside"):
            await amesh_social_reply("loopback", "a" * 32, "hi")

    asyncio.run(scenario())


def test_mcp_adapters_list_unconfigured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AMESH_HOME", str(tmp_path))

    async def scenario() -> None:
        out = json.loads(await amesh_adapters())
        assert {item["name"] for item in out["adapters"]} == {
            "discord",
            "loopback",
        }

    asyncio.run(scenario())


def test_mcp_loopback_shell_flow(tmp_path, monkeypatch) -> None:
    node = _node_home(tmp_path)
    monkeypatch.setenv("AMESH_HOME", str(node))
    LoopbackConfig(
        channels=("lobby",),
        policy=LOW_POLICY,
        destination_id=DEST,
        poll_interval_seconds=1.0,
    ).save(node)

    async def scenario() -> None:
        injected = json.loads(
            await amesh_social_inject("loopback", "alice", "@amesh hi")
        )
        assert injected["spooled"] is True

        poll = json.loads(await amesh_social_poll("loopback"))
        assert poll["ingested"] == 1

        ledger = LoopbackLedger(
            loopback_database_path(node),
            loopback_key_path(node),
        )
        try:
            actor_key = ledger.pseudonym("actor", "alice")
        finally:
            ledger.close()

        actor = json.loads(await amesh_social_actor("loopback", actor_key))
        assert actor["evaluation"]["action"] in {"surface", "reply", "amplify"}

        rule = json.loads(
            await amesh_permit_add(
                "loopback",
                actor_key,
                "reply",
                "deny",
                reason="quiet channel",
            )
        )
        listed = json.loads(await amesh_permit_list("loopback"))
        assert listed["rules"][0]["rule_id"] == rule["rule_id"]

        projected = json.loads(await amesh_social_project("loopback"))
        assert projected["interactions_recorded"] == 1

        relations = json.loads(await amesh_relations())
        assert len(relations["relationships"]) == 1
        subject_ref = relations["relationships"][0]["subject_ref"]

        circled = json.loads(
            await amesh_relations_circle(
                subject_ref,
                "friend",
                confidence=70,
                evidence_ref="mcp:test",
            )
        )
        assert circled["circle"] == "friend"

    asyncio.run(scenario())


def test_mcp_reply_denied_by_permission_rule(tmp_path, monkeypatch) -> None:
    node = _node_home(tmp_path)
    monkeypatch.setenv("AMESH_HOME", str(node))
    monkeypatch.setenv("AMESH_MCP_ALLOW_REPLY", "1")
    LoopbackConfig(channels=("lobby",), policy=LOW_POLICY).save(node)

    async def scenario() -> None:
        await amesh_social_inject("loopback", "alice", "@amesh hi")
        await amesh_social_poll("loopback")
        ledger = LoopbackLedger(
            loopback_database_path(node),
            loopback_key_path(node),
        )
        try:
            actor_key = ledger.pseudonym("actor", "alice")
            event_key = ledger.events()[0]["event_key"]
        finally:
            ledger.close()
        await amesh_permit_add("loopback", actor_key, "reply", "deny")
        with pytest.raises(PermissionError, match="permission rule"):
            await amesh_social_reply("loopback", event_key, "hi")

    asyncio.run(scenario())


def test_mcp_reply_allowed_when_granted(tmp_path, monkeypatch) -> None:
    node = _node_home(tmp_path)
    monkeypatch.setenv("AMESH_HOME", str(node))
    monkeypatch.setenv("AMESH_MCP_ALLOW_REPLY", "1")
    LoopbackConfig(channels=("lobby",), policy=LOW_POLICY).save(node)

    async def scenario() -> None:
        await amesh_social_inject("loopback", "alice", "@amesh hi")
        await amesh_social_poll("loopback")
        ledger = LoopbackLedger(
            loopback_database_path(node),
            loopback_key_path(node),
        )
        try:
            event_key = ledger.events()[0]["event_key"]
        finally:
            ledger.close()
        sent = json.loads(await amesh_social_reply("loopback", event_key, "hi alice"))
        assert sent["sent"] is True

    asyncio.run(scenario())


def test_mcp_signals_lists_outbound(tmp_path, monkeypatch) -> None:
    node = _node_home(tmp_path)
    monkeypatch.setenv("AMESH_HOME", str(node))
    LoopbackConfig(
        channels=("lobby",),
        policy=LOW_POLICY,
        destination_id=DEST,
    ).save(node)
    adapter = LoopbackAdapter(node)
    try:
        adapter.inject("alice", "@amesh hi")
        sink = DirectorySignalSink(amesh_outbound_dir(node))
        adapter.poll_once(
            queue_signal=lambda destination_id, kind, body: sink.emit(dict(body))
        )
    finally:
        adapter.close()

    async def scenario() -> None:
        out = json.loads(await amesh_social_signals("loopback"))
        assert out["count"] == 1
        assert out["signals"][0]["provenance"]["platform"] == "loopback"

    asyncio.run(scenario())
