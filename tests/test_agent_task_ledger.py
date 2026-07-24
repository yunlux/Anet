from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import anet.store as store_module
from anet.agent_protocol import task_request
from anet.packet import OpenedMessage
from anet.store import PacketStore


def request_message(
    index: int,
    *,
    task_id: str,
    objective: str = "review patch",
    trusted_sender: str = "peer-a",
) -> OpenedMessage:
    return OpenedMessage(
        packet_id=f"{index:032x}",
        sender_id=trusted_sender,
        sender_sign_public=b"s" * 32,
        sender_box_public=b"b" * 32,
        kind="agent.task.request",
        created_ms=1000 + index,
        body=task_request(
            task_id=task_id,
            objective=objective,
            input={"commit": "abc"},
            required_capabilities=["code.review"],
        ),
        causal=(),
        codec="application/msgpack",
        reply_to="",
        qos="normal",
    )


def begin_task(
    store: PacketStore,
    group: str,
    owner: str,
    claim_token: str,
    *,
    allowed_senders: set[str] | None = None,
    allowed_capabilities: set[str] | None = None,
) -> dict:
    return store.begin_agent_task(
        group,
        owner,
        claim_token,
        allowed_senders=(
            {"peer-a"} if allowed_senders is None else allowed_senders
        ),
        allowed_capabilities=(
            {"code.review"}
            if allowed_capabilities is None
            else allowed_capabilities
        ),
    )


def test_completed_logical_task_deduplicates_a_new_packet_and_returns_result(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "tasks.sqlite3")
    task_id = "ab" * 16
    try:
        store.add_inbox(request_message(1, task_id=task_id), trusted=True)
        store.open_consumer_group(
            "runtime.tasks",
            start="earliest",
            kind_prefix="agent.task.request",
        )
        first_claim = store.claim_consumer_messages(
            "runtime.tasks", "worker-a", lease_seconds=60
        )[0]
        first = begin_task(
            store,
            "runtime.tasks", "worker-a", first_claim["claim_token"]
        )
        assert first["execute"] is True
        assert first["attempts"] == 1
        with pytest.raises(ValueError, match="settle_agent_task"):
            store.acknowledge_claim(
                "runtime.tasks", "worker-a", first_claim["claim_token"]
            )
        with pytest.raises(ValueError, match="settle_agent_task"):
            store.reject_claim(
                "runtime.tasks", "worker-a", first_claim["claim_token"]
            )

        completed = store.settle_agent_task(
            "runtime.tasks",
            "worker-a",
            first_claim["claim_token"],
            first["execution_token"],
            state="completed",
            output={"verdict": "pass"},
        )
        assert completed["state"] == "completed"
        assert completed["claim_state"] == "acked"

        store.add_inbox(request_message(2, task_id=task_id), trusted=True)
        duplicate_claim = store.claim_consumer_messages(
            "runtime.tasks", "worker-b", lease_seconds=60
        )[0]
        duplicate = begin_task(
            store,
            "runtime.tasks", "worker-b", duplicate_claim["claim_token"]
        )
        assert duplicate["execute"] is False
        assert duplicate["duplicate"] is True
        assert duplicate["claim_acked"] is True
        assert duplicate["output"] == {"verdict": "pass"}
        assert store.consumer_group_status("runtime.tasks")["states"] == {
            "acked": 2
        }
    finally:
        store.close()


def test_task_id_reuse_with_different_request_fails_closed(tmp_path) -> None:
    store = PacketStore(tmp_path / "conflict.sqlite3")
    task_id = "bc" * 16
    try:
        store.add_inbox(request_message(10, task_id=task_id), trusted=True)
        store.open_consumer_group("tasks", start="earliest")
        first_claim = store.claim_consumer_messages("tasks", "worker-a")[0]
        first = begin_task(
            store,
            "tasks", "worker-a", first_claim["claim_token"]
        )
        store.settle_agent_task(
            "tasks",
            "worker-a",
            first_claim["claim_token"],
            first["execution_token"],
            state="completed",
            output={},
        )

        store.add_inbox(
            request_message(11, task_id=task_id, objective="delete everything"),
            trusted=True,
        )
        conflicting_claim = store.claim_consumer_messages("tasks", "worker-b")[0]
        with pytest.raises(ValueError, match="different request body"):
            begin_task(
                store,
                "tasks", "worker-b", conflicting_claim["claim_token"]
            )
    finally:
        store.close()


def test_expired_claim_can_take_over_but_old_execution_token_cannot_settle(
    tmp_path,
    monkeypatch,
) -> None:
    clock = [10_000]
    monkeypatch.setattr(store_module, "now_ms", lambda: clock[0])
    store = PacketStore(tmp_path / "takeover.sqlite3")
    task_id = "cd" * 16
    try:
        store.add_inbox(request_message(20, task_id=task_id), trusted=True)
        store.open_consumer_group("tasks", start="earliest")
        old_claim = store.claim_consumer_messages(
            "tasks", "worker-a", lease_seconds=5
        )[0]
        old_execution = begin_task(
            store,
            "tasks", "worker-a", old_claim["claim_token"]
        )

        clock[0] += 5001
        new_claim = store.claim_consumer_messages(
            "tasks", "worker-b", lease_seconds=5
        )[0]
        new_execution = begin_task(
            store,
            "tasks", "worker-b", new_claim["claim_token"]
        )
        assert new_execution["execute"] is True
        assert new_execution["attempts"] == 2
        assert new_execution["execution_token"] != old_execution["execution_token"]

        with pytest.raises(ValueError, match="stale"):
            store.settle_agent_task(
                "tasks",
                "worker-a",
                old_claim["claim_token"],
                old_execution["execution_token"],
                state="completed",
                output={"unsafe": True},
            )
        settled = store.settle_agent_task(
            "tasks",
            "worker-b",
            new_claim["claim_token"],
            new_execution["execution_token"],
            state="completed",
            output={"safe": True},
        )
        assert settled["output"] == {"safe": True}
    finally:
        store.close()


def test_retry_releases_consumer_claim_and_task_execution_together(
    tmp_path,
    monkeypatch,
) -> None:
    clock = [20_000]
    monkeypatch.setattr(store_module, "now_ms", lambda: clock[0])
    store = PacketStore(tmp_path / "retry.sqlite3")
    task_id = "de" * 16
    try:
        store.add_inbox(request_message(30, task_id=task_id), trusted=True)
        store.open_consumer_group("tasks", start="earliest")
        first_claim = store.claim_consumer_messages("tasks", "worker-a")[0]
        first = begin_task(
            store,
            "tasks", "worker-a", first_claim["claim_token"]
        )
        retry = store.settle_agent_task(
            "tasks",
            "worker-a",
            first_claim["claim_token"],
            first["execution_token"],
            state="retry",
            error="dependency unavailable",
            retry_seconds=10,
        )
        assert retry["state"] == "retry"
        assert retry["claim_state"] == "retry"
        assert store.claim_consumer_messages("tasks", "worker-b") == []

        clock[0] += 10_000
        second_claim = store.claim_consumer_messages("tasks", "worker-b")[0]
        second = begin_task(
            store,
            "tasks", "worker-b", second_claim["claim_token"]
        )
        assert second["execute"] is True
        assert second["attempts"] == 2
    finally:
        store.close()


def test_untrusted_task_cannot_enter_execution_ledger(tmp_path) -> None:
    store = PacketStore(tmp_path / "untrusted.sqlite3")
    try:
        store.add_inbox(
            request_message(40, task_id="ef" * 16),
            trusted=False,
        )
        store.open_consumer_group(
            "tasks",
            start="earliest",
            trusted_only=False,
        )
        claim = store.claim_consumer_messages("tasks", "worker")[0]
        with pytest.raises(PermissionError, match="trusted sender"):
            begin_task(store, "tasks", "worker", claim["claim_token"])
        assert store.status()["agent_tasks"] == 0
    finally:
        store.close()


def test_sender_and_capability_policy_are_required_before_ledger_acquisition(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "policy.sqlite3")
    try:
        store.add_inbox(
            request_message(45, task_id="ad" * 16),
            trusted=True,
        )
        store.open_consumer_group("tasks", start="earliest")
        claim = store.claim_consumer_messages("tasks", "worker")[0]

        with pytest.raises(PermissionError, match="sender"):
            begin_task(
                store,
                "tasks",
                "worker",
                claim["claim_token"],
                allowed_senders=set(),
                allowed_capabilities={"code.review"},
            )
        with pytest.raises(PermissionError, match="capabilities"):
            begin_task(
                store,
                "tasks",
                "worker",
                claim["claim_token"],
                allowed_senders={"peer-a"},
                allowed_capabilities={"health.*"},
            )
        assert store.status()["agent_tasks"] == 0

        execution = begin_task(
            store,
            "tasks",
            "worker",
            claim["claim_token"],
            allowed_senders={"peer-a"},
            allowed_capabilities={"code.*"},
        )
        assert execution["execute"] is True
    finally:
        store.close()


def test_concurrent_duplicate_packets_allow_only_one_logical_execution(
    tmp_path,
) -> None:
    path = tmp_path / "concurrent-tasks.sqlite3"
    first = PacketStore(path)
    second = PacketStore(path)
    task_id = "ac" * 16
    try:
        first.add_inbox(request_message(50, task_id=task_id), trusted=True)
        first.add_inbox(request_message(51, task_id=task_id), trusted=True)
        first.open_consumer_group("tasks", start="earliest")
        claim_a = first.claim_consumer_messages("tasks", "worker-a")[0]
        claim_b = second.claim_consumer_messages("tasks", "worker-b")[0]
        barrier = threading.Barrier(2)

        def begin(
            store: PacketStore,
            owner: str,
            claim_token: str,
        ) -> dict:
            barrier.wait()
            return begin_task(store, "tasks", owner, claim_token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                begin,
                first,
                "worker-a",
                claim_a["claim_token"],
            )
            future_b = pool.submit(
                begin,
                second,
                "worker-b",
                claim_b["claim_token"],
            )
            results = [future_a.result(), future_b.result()]

        assert sorted(item["execute"] for item in results) == [False, True]
        winner = next(item for item in results if item["execute"])
        loser = next(item for item in results if not item["execute"])
        assert winner["attempts"] == 1
        assert loser["duplicate"] is True
        assert loser["claim_acked"] is True
    finally:
        first.close()
        second.close()
