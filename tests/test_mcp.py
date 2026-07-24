from __future__ import annotations

import asyncio
import json

import pytest

from anet.config import initialize_node
from anet.agent_protocol import task_cancel, task_request
from anet.identity import Identity
from anet.mcp_server import (
    anet_approval_activate,
    anet_approval_effect_begin,
    anet_approval_effect_settle,
    anet_claim,
    anet_consumer_open,
    anet_consumer_status,
    anet_inbox,
    anet_lifespan,
    anet_peers,
    anet_send,
    anet_settle,
    anet_status,
    anet_task,
    anet_task_begin,
    anet_task_cancel_apply,
    anet_task_cancel_check,
    anet_task_settle,
    server,
)
from anet.packet import OpenedMessage
from anet.peers import PeerBook
from anet.store import PacketStore


def test_mcp_approval_execution_tools_are_disabled_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ANET_MCP_ALLOW_APPROVAL_EXECUTION", raising=False)

    async def scenario() -> None:
        with pytest.raises(PermissionError, match="outside"):
            await anet_approval_activate("approvals", "00" * 16, "worker")
        with pytest.raises(PermissionError, match="outside"):
            await anet_approval_effect_begin("11" * 16, "12" * 16, "worker")
        with pytest.raises(PermissionError, match="outside"):
            await anet_approval_effect_settle(
                "11" * 16,
                "12" * 16,
                "13" * 16,
                "executed",
            )

    asyncio.run(scenario())


def test_mcp_adapter_uses_local_node_store(tmp_path, monkeypatch) -> None:
    a = initialize_node(tmp_path / "a", label="a", listen_port=45101)
    b = initialize_node(tmp_path / "b", label="b", listen_port=45102)
    a_identity = Identity.load(a.identity_path)
    b_identity = Identity.load(b.identity_path)
    book = PeerBook(a.peers_path, own_node_id=a_identity.node_id)
    book.add(
        b_identity.card(addresses=b.effective_addresses(), capabilities=b.capabilities)
    )
    monkeypatch.setenv("ANET_HOME", str(a.home))

    async def scenario() -> None:
        async with anet_lifespan(server):
            status = json.loads(await anet_status())
            assert status["node_id"] == a_identity.node_id
            peers = json.loads(await anet_peers())
            assert peers[0]["node_id"] == b_identity.node_id
            result = json.loads(
                await anet_send(
                    b_identity.node_id,
                    "agent.ir",
                    {"performative": "PROPOSE"},
                )
            )
            assert len(result["queued"]) == 32

    asyncio.run(scenario())
    store = PacketStore(a.database_path)
    try:
        assert store.status()["pending"] == 1
    finally:
        store.close()


def test_mcp_durable_consumer_claims_and_settles(tmp_path, monkeypatch) -> None:
    config = initialize_node(tmp_path / "node", label="node", listen_port=45103)
    seed = PacketStore(config.database_path)
    try:
        seed.add_inbox(
            OpenedMessage(
                packet_id="ab" * 16,
                sender_id="trusted-peer",
                sender_sign_public=b"s" * 32,
                sender_box_public=b"b" * 32,
                kind="agent.task",
                created_ms=1000,
                body={"objective": "review"},
                causal=(),
                codec="application/msgpack",
                reply_to="",
                qos="normal",
            ),
            trusted=True,
        )
    finally:
        seed.close()
    monkeypatch.setenv("ANET_HOME", str(config.home))
    monkeypatch.setenv("ANET_AGENT_ID", "runtime-test")

    async def scenario() -> None:
        async with anet_lifespan(server):
            opened = json.loads(
                await anet_consumer_open(
                    "runtime.tasks", start="earliest", kind_prefix="agent."
                )
            )
            assert opened["created"] is True
            claims = json.loads(await anet_claim("runtime.tasks", lease_seconds=60))
            assert claims[0]["body"] == {"objective": "review"}
            assert claims[0]["claim_owner"] == "runtime-test"
            assert "local policy" in claims[0]["content_security"]
            settled = json.loads(
                await anet_settle(
                    "runtime.tasks",
                    claims[0]["claim_token"],
                    "ack",
                )
            )
            assert settled["state"] == "acked"
            status = json.loads(await anet_consumer_status("runtime.tasks"))
            assert status["states"] == {"acked": 1}

    asyncio.run(scenario())


def test_mcp_process_capabilities_restrict_owner_group_kind_peer_and_raw_inbox(
    tmp_path,
    monkeypatch,
) -> None:
    config = initialize_node(tmp_path / "scoped", label="scoped", listen_port=45104)
    monkeypatch.setenv("ANET_HOME", str(config.home))
    monkeypatch.setenv("ANET_AGENT_ID", "runtime-a")
    monkeypatch.setenv("ANET_MCP_GROUP_PREFIX", "runtime-a.")
    monkeypatch.setenv("ANET_MCP_KIND_PREFIX", "agent.runtime-a.")
    monkeypatch.setenv("ANET_MCP_ALLOWED_PEERS", "allowed-peer")
    monkeypatch.setenv("ANET_MCP_ALLOW_RAW_INBOX", "0")

    async def scenario() -> None:
        async with anet_lifespan(server):
            with pytest.raises(PermissionError, match="owner"):
                await anet_claim("runtime-a.tasks", owner="other-runtime")
            with pytest.raises(PermissionError, match="group"):
                await anet_consumer_open("ahub.tasks")
            with pytest.raises(PermissionError, match="kind"):
                await anet_consumer_open(
                    "runtime-a.tasks", kind_prefix="agent.other-runtime."
                )
            with pytest.raises(PermissionError, match="peer"):
                await anet_send("blocked-peer", "agent.runtime-a.task", {})
            with pytest.raises(PermissionError, match="raw inbox"):
                await anet_inbox()

            opened = json.loads(await anet_consumer_open("runtime-a.tasks"))
            assert opened["kind_prefix"] == "agent.runtime-a."

    asyncio.run(scenario())


def test_mcp_typed_task_uses_peer_scope_and_stable_task_id(
    tmp_path,
    monkeypatch,
) -> None:
    a = initialize_node(tmp_path / "task-a", label="a", listen_port=45105)
    b = initialize_node(tmp_path / "task-b", label="b", listen_port=45106)
    a_identity = Identity.load(a.identity_path)
    b_identity = Identity.load(b.identity_path)
    PeerBook(a.peers_path, own_node_id=a_identity.node_id).add(
        b_identity.card(addresses=b.effective_addresses(), capabilities=b.capabilities)
    )
    monkeypatch.setenv("ANET_HOME", str(a.home))
    monkeypatch.setenv("ANET_MCP_ALLOWED_PEERS", b_identity.node_id)

    async def scenario() -> None:
        async with anet_lifespan(server):
            requested = json.loads(
                await anet_task(
                    b_identity.node_id,
                    "request",
                    objective="review patch",
                    payload={"commit": "abc"},
                    required_capabilities=["code.review"],
                )
            )
            assert requested["kind"] == "agent.task.request"
            assert len(requested["task_id"]) == 32

            completed = json.loads(
                await anet_task(
                    b_identity.node_id,
                    "result",
                    task_id=requested["task_id"],
                    state="completed",
                    payload={"verdict": "pass"},
                )
            )
            assert completed["task_id"] == requested["task_id"]

            with pytest.raises(PermissionError, match="peer"):
                await anet_task(
                    "blocked-peer",
                    "request",
                    objective="must not queue",
                )

    asyncio.run(scenario())


def test_mcp_task_execution_is_durable_and_settles_claim_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    config = initialize_node(
        tmp_path / "task-worker",
        label="task-worker",
        listen_port=45107,
    )
    task_id = "fa" * 16
    seed = PacketStore(config.database_path)
    try:
        seed.add_inbox(
            OpenedMessage(
                packet_id="fb" * 16,
                sender_id="trusted-peer",
                sender_sign_public=b"s" * 32,
                sender_box_public=b"b" * 32,
                kind="agent.task.request",
                created_ms=1000,
                body={
                    "protocol": "anet.agent.task",
                    "version": 1,
                    "task_id": task_id,
                    "objective": "review",
                    "input": {},
                    "required_capabilities": ["code.review"],
                    "context": {},
                },
                causal=(),
                codec="application/msgpack",
                reply_to="",
                qos="normal",
            ),
            trusted=True,
        )
    finally:
        seed.close()
    monkeypatch.setenv("ANET_HOME", str(config.home))
    monkeypatch.setenv("ANET_AGENT_ID", "runtime-worker")
    monkeypatch.setenv("ANET_MCP_GROUP_PREFIX", "runtime.")

    async def scenario() -> None:
        async with anet_lifespan(server):
            await anet_consumer_open(
                "runtime.tasks",
                start="earliest",
                kind_prefix="agent.task.request",
            )
            claim = json.loads(await anet_claim("runtime.tasks"))[0]
            with pytest.raises(PermissionError, match="sender"):
                await anet_task_begin(
                    "runtime.tasks",
                    claim["claim_token"],
                )
            monkeypatch.setenv(
                "ANET_MCP_TASK_ALLOWED_SENDERS",
                "trusted-peer",
            )
            with pytest.raises(PermissionError, match="capabilities"):
                await anet_task_begin(
                    "runtime.tasks",
                    claim["claim_token"],
                )
            monkeypatch.setenv(
                "ANET_MCP_TASK_CAPABILITIES",
                "code.review",
            )
            policy = json.loads(await anet_status())["mcp_task_policy"]
            assert policy == {
                "allow_all_senders": False,
                "allowed_sender_count": 1,
                "allow_all_capabilities": False,
                "capability_patterns": ["code.review"],
            }
            execution = json.loads(
                await anet_task_begin(
                    "runtime.tasks",
                    claim["claim_token"],
                )
            )
            assert execution["execute"] is True
            settled = json.loads(
                await anet_task_settle(
                    "runtime.tasks",
                    claim["claim_token"],
                    execution["execution_token"],
                    "completed",
                    payload={"verdict": "pass"},
                )
            )
            assert settled["state"] == "completed"
            assert settled["claim_state"] == "acked"
            status = json.loads(await anet_consumer_status("runtime.tasks"))
            assert status["states"] == {"acked": 1}

    asyncio.run(scenario())


def test_mcp_task_cancellation_fences_completion_and_is_cooperative(
    tmp_path,
    monkeypatch,
) -> None:
    config = initialize_node(
        tmp_path / "cancel-worker",
        label="cancel-worker",
        listen_port=45108,
    )
    task_id = "ca" * 16
    seed = PacketStore(config.database_path)
    try:
        for packet_id, kind, body in (
            (
                "cb" * 16,
                "agent.task.request",
                task_request(
                    task_id=task_id,
                    objective="review",
                    required_capabilities=["code.review"],
                ),
            ),
            (
                "cc" * 16,
                "agent.task.cancel",
                task_cancel(task_id=task_id, reason="operator request"),
            ),
        ):
            seed.add_inbox(
                OpenedMessage(
                    packet_id=packet_id,
                    sender_id="trusted-peer",
                    sender_sign_public=b"s" * 32,
                    sender_box_public=b"b" * 32,
                    kind=kind,
                    created_ms=1000,
                    body=body,
                    causal=(),
                    codec="application/msgpack",
                    reply_to="",
                    qos="normal",
                ),
                trusted=True,
            )
    finally:
        seed.close()
    monkeypatch.setenv("ANET_HOME", str(config.home))
    monkeypatch.setenv("ANET_AGENT_ID", "runtime-worker")
    monkeypatch.setenv("ANET_MCP_GROUP_PREFIX", "runtime.")
    monkeypatch.setenv("ANET_MCP_TASK_ALLOWED_SENDERS", "trusted-peer")
    monkeypatch.setenv("ANET_MCP_TASK_CAPABILITIES", "code.review")

    async def scenario() -> None:
        async with anet_lifespan(server):
            await anet_consumer_open(
                "runtime.tasks",
                start="earliest",
                kind_prefix="agent.task.",
            )
            request_claim = json.loads(await anet_claim("runtime.tasks"))[0]
            execution = json.loads(
                await anet_task_begin(
                    "runtime.tasks",
                    request_claim["claim_token"],
                )
            )
            cancel_claim = json.loads(await anet_claim("runtime.tasks"))[0]
            cancellation = json.loads(
                await anet_task_cancel_apply(
                    "runtime.tasks",
                    cancel_claim["claim_token"],
                )
            )
            assert cancellation["cooperative_stop_required"] is True
            check = json.loads(
                await anet_task_cancel_check(
                    "runtime.tasks",
                    execution["execution_token"],
                )
            )
            assert check["reason"] == "operator request"
            with pytest.raises(ValueError, match="only canceled"):
                await anet_task_settle(
                    "runtime.tasks",
                    request_claim["claim_token"],
                    execution["execution_token"],
                    "completed",
                )
            settled = json.loads(
                await anet_task_settle(
                    "runtime.tasks",
                    request_claim["claim_token"],
                    execution["execution_token"],
                    "canceled",
                    error=check["reason"],
                )
            )
            assert settled["state"] == "canceled"

    asyncio.run(scenario())
