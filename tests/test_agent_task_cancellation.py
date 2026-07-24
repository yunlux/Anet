from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import anet.store as store_module
from anet.agent_protocol import task_cancel, task_request
from anet.packet import OpenedMessage
from anet.store import PacketStore


def message(
    index: int,
    *,
    kind: str,
    task_id: str,
    sender_id: str = "peer-a",
    reason: str = "operator requested cancellation",
) -> OpenedMessage:
    body = (
        task_request(
            task_id=task_id,
            objective="review patch",
            input={"commit": "abc"},
            required_capabilities=["code.review"],
        )
        if kind == "agent.task.request"
        else task_cancel(task_id=task_id, reason=reason)
    )
    return OpenedMessage(
        packet_id=f"{index:032x}",
        sender_id=sender_id,
        sender_sign_public=b"s" * 32,
        sender_box_public=b"b" * 32,
        kind=kind,
        created_ms=1000 + index,
        body=body,
        causal=(),
        codec="application/msgpack",
        reply_to="",
        qos="normal",
    )


def begin(
    store: PacketStore,
    owner: str,
    claim_token: str,
) -> dict:
    return store.begin_agent_task(
        "tasks",
        owner,
        claim_token,
        allowed_senders={"peer-a"},
        allowed_capabilities={"code.review"},
    )


def apply_cancel(
    store: PacketStore,
    owner: str,
    claim_token: str,
    *,
    allowed_senders: set[str] | None = None,
) -> dict:
    return store.apply_agent_task_cancellation(
        "tasks",
        owner,
        claim_token,
        allowed_senders=allowed_senders or {"peer-a"},
    )


def open_group(store: PacketStore) -> None:
    store.open_consumer_group(
        "tasks",
        start="earliest",
        kind_prefix="agent.task.",
    )


def test_running_task_must_stop_and_settle_canceled(tmp_path) -> None:
    store = PacketStore(tmp_path / "cooperative.sqlite3")
    task_id = "a1" * 16
    try:
        store.add_inbox(
            message(1, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        open_group(store)
        request_claim = store.claim_consumer_messages("tasks", "worker")[0]
        execution = begin(store, "worker", request_claim["claim_token"])

        store.add_inbox(
            message(2, kind="agent.task.cancel", task_id=task_id),
            trusted=True,
        )
        cancel_claim = store.claim_consumer_messages("tasks", "canceler")[0]
        cancellation = apply_cancel(
            store,
            "canceler",
            cancel_claim["claim_token"],
        )
        assert cancellation["state"] == "requested"
        assert cancellation["cooperative_stop_required"] is True
        pending = store.agent_task_cancellation(
            "tasks",
            "worker",
            execution["execution_token"],
        )
        assert pending["reason"] == "operator requested cancellation"

        with pytest.raises(ValueError, match="settle_agent_task"):
            store.acknowledge_claim(
                "tasks",
                "worker",
                request_claim["claim_token"],
            )
        with pytest.raises(ValueError, match="only canceled"):
            store.settle_agent_task(
                "tasks",
                "worker",
                request_claim["claim_token"],
                execution["execution_token"],
                state="completed",
                output={"unsafe": True},
            )

        settled = store.settle_agent_task(
            "tasks",
            "worker",
            request_claim["claim_token"],
            execution["execution_token"],
            state="canceled",
            error=pending["reason"],
        )
        assert settled["state"] == "canceled"
        assert store.agent_task_cancellation(
            "tasks",
            "worker",
            execution["execution_token"],
        ) is None
        assert store.consumer_group_status("tasks")["states"] == {"acked": 2}
    finally:
        store.close()


def test_cancel_arriving_before_request_prevents_execution(tmp_path) -> None:
    path = tmp_path / "reordered.sqlite3"
    store = PacketStore(path)
    task_id = "b2" * 16
    try:
        store.add_inbox(
            message(1, kind="agent.task.cancel", task_id=task_id),
            trusted=True,
        )
        open_group(store)
        cancel_claim = store.claim_consumer_messages("tasks", "canceler")[0]
        cancellation = apply_cancel(
            store,
            "canceler",
            cancel_claim["claim_token"],
        )
        assert cancellation["state"] == "requested"
        assert cancellation["cooperative_stop_required"] is False

        store.close()
        store = PacketStore(path)
        store.add_inbox(
            message(2, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        request_claim = store.claim_consumer_messages("tasks", "worker")[0]
        execution = begin(store, "worker", request_claim["claim_token"])
        assert execution["execute"] is False
        assert execution["cancel_requested"] is True
        assert execution["state"] == "canceled"
        assert execution["attempts"] == 0
        assert execution["claim_acked"] is True
        assert store.status()["active_agent_tasks"] == 0
    finally:
        store.close()


def test_terminal_completion_wins_race_and_cancel_is_too_late(tmp_path) -> None:
    store = PacketStore(tmp_path / "too-late.sqlite3")
    task_id = "c3" * 16
    try:
        store.add_inbox(
            message(1, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        open_group(store)
        request_claim = store.claim_consumer_messages("tasks", "worker")[0]
        execution = begin(store, "worker", request_claim["claim_token"])
        store.settle_agent_task(
            "tasks",
            "worker",
            request_claim["claim_token"],
            execution["execution_token"],
            state="completed",
            output={"verdict": "pass"},
        )

        store.add_inbox(
            message(2, kind="agent.task.cancel", task_id=task_id),
            trusted=True,
        )
        cancel_claim = store.claim_consumer_messages("tasks", "canceler")[0]
        cancellation = apply_cancel(
            store,
            "canceler",
            cancel_claim["claim_token"],
        )
        assert cancellation["state"] == "too_late"
        assert cancellation["terminal_state"] == "completed"
        assert cancellation["cooperative_stop_required"] is False
    finally:
        store.close()


def test_expired_canceling_execution_is_fenced_by_lease_takeover(
    tmp_path,
    monkeypatch,
) -> None:
    clock = [10_000]
    monkeypatch.setattr(store_module, "now_ms", lambda: clock[0])
    store = PacketStore(tmp_path / "forced-fence.sqlite3")
    task_id = "d4" * 16
    try:
        store.add_inbox(
            message(1, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        open_group(store)
        old_claim = store.claim_consumer_messages(
            "tasks",
            "worker-a",
            lease_seconds=5,
        )[0]
        old_execution = begin(store, "worker-a", old_claim["claim_token"])
        store.add_inbox(
            message(2, kind="agent.task.cancel", task_id=task_id),
            trusted=True,
        )
        cancel_claim = store.claim_consumer_messages("tasks", "canceler")[0]
        apply_cancel(store, "canceler", cancel_claim["claim_token"])

        clock[0] += 5001
        takeover_claim = store.claim_consumer_messages(
            "tasks",
            "worker-b",
            lease_seconds=5,
        )[0]
        fenced = begin(store, "worker-b", takeover_claim["claim_token"])
        assert fenced["execute"] is False
        assert fenced["state"] == "canceled"
        assert fenced["cancel_requested"] is True
        with pytest.raises(ValueError, match="stale"):
            store.settle_agent_task(
                "tasks",
                "worker-a",
                old_claim["claim_token"],
                old_execution["execution_token"],
                state="canceled",
                error="late stop",
            )
    finally:
        store.close()


def test_cancellation_is_scoped_to_authenticated_sender(tmp_path) -> None:
    store = PacketStore(tmp_path / "sender-scope.sqlite3")
    task_id = "e5" * 16
    try:
        store.add_inbox(
            message(1, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        open_group(store)
        request_claim = store.claim_consumer_messages("tasks", "worker")[0]
        execution = begin(store, "worker", request_claim["claim_token"])

        store.add_inbox(
            message(
                2,
                kind="agent.task.cancel",
                task_id=task_id,
                sender_id="peer-b",
            ),
            trusted=True,
        )
        cancel_claim = store.claim_consumer_messages("tasks", "canceler")[0]
        unrelated = apply_cancel(
            store,
            "canceler",
            cancel_claim["claim_token"],
            allowed_senders={"peer-a", "peer-b"},
        )
        assert unrelated["sender_id"] == "peer-b"
        assert unrelated["cooperative_stop_required"] is False
        assert store.agent_task_cancellation(
            "tasks",
            "worker",
            execution["execution_token"],
        ) is None
        completed = store.settle_agent_task(
            "tasks",
            "worker",
            request_claim["claim_token"],
            execution["execution_token"],
            state="completed",
            output={},
        )
        assert completed["state"] == "completed"
    finally:
        store.close()


def test_completion_and_cancellation_race_has_one_durable_winner(tmp_path) -> None:
    path = tmp_path / "race.sqlite3"
    executor_store = PacketStore(path)
    cancel_store = PacketStore(path)
    task_id = "f6" * 16
    try:
        executor_store.add_inbox(
            message(1, kind="agent.task.request", task_id=task_id),
            trusted=True,
        )
        open_group(executor_store)
        request_claim = executor_store.claim_consumer_messages(
            "tasks",
            "worker",
        )[0]
        execution = begin(
            executor_store,
            "worker",
            request_claim["claim_token"],
        )
        executor_store.add_inbox(
            message(2, kind="agent.task.cancel", task_id=task_id),
            trusted=True,
        )
        cancel_claim = cancel_store.claim_consumer_messages(
            "tasks",
            "canceler",
        )[0]
        barrier = threading.Barrier(2)

        def complete() -> dict:
            barrier.wait()
            return executor_store.settle_agent_task(
                "tasks",
                "worker",
                request_claim["claim_token"],
                execution["execution_token"],
                state="completed",
                output={"verdict": "pass"},
            )

        def cancel() -> dict:
            barrier.wait()
            return apply_cancel(
                cancel_store,
                "canceler",
                cancel_claim["claim_token"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            completion_future = pool.submit(complete)
            cancellation_future = pool.submit(cancel)
            try:
                completion = completion_future.result()
                completion_error = None
            except ValueError as exc:
                completion = None
                completion_error = exc
            cancellation = cancellation_future.result()

        if completion is not None:
            assert completion["state"] == "completed"
            assert cancellation["state"] == "too_late"
            assert cancellation["terminal_state"] == "completed"
        else:
            assert "only canceled" in str(completion_error)
            assert cancellation["state"] == "requested"
            settled = executor_store.settle_agent_task(
                "tasks",
                "worker",
                request_claim["claim_token"],
                execution["execution_token"],
                state="canceled",
                error=cancellation["reason"],
            )
            assert settled["state"] == "canceled"
    finally:
        executor_store.close()
        cancel_store.close()
