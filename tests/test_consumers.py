from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import anet.store as store_module
from anet.packet import OpenedMessage
from anet.store import PacketStore


def message(
    index: int,
    *,
    kind: str = "agent.task",
    sender: str = "peer-a",
    body=None,  # noqa: ANN001
) -> OpenedMessage:
    return OpenedMessage(
        packet_id=f"{index:032x}",
        sender_id=sender,
        sender_sign_public=b"s" * 32,
        sender_box_public=b"b" * 32,
        kind=kind,
        created_ms=1000 + index,
        body={"index": index} if body is None else body,
        causal=(),
        codec="application/msgpack",
        reply_to="",
        qos="normal",
    )


def test_consumer_group_defaults_to_new_trusted_visible_matching_messages(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "consumer.sqlite3")
    try:
        store.add_inbox(message(1), trusted=True)
        opened = store.open_consumer_group(
            "node_b.tasks",
            start="latest",
            kind_prefix="agent.",
            sender_id="peer-a",
        )
        assert opened["created"] is True
        assert store.claim_consumer_messages("node_b.tasks", "worker-1") == []

        store.add_inbox(message(2), trusted=True)
        store.add_inbox(message(3, kind="chat.message"), trusted=True)
        store.add_inbox(message(4, sender="peer-b"), trusted=True)
        store.add_inbox(message(5), trusted=False)
        store.add_inbox(message(6), trusted=True, visible=False)
        claimed = store.claim_consumer_messages("node_b.tasks", "worker-1", limit=10)
        assert [item["packet_id"] for item in claimed] == [f"{2:032x}"]
        assert claimed[0]["content_security"].startswith("Authenticated sender")

        reopened = store.open_consumer_group(
            "node_b.tasks",
            start="earliest",
            kind_prefix="agent.",
            sender_id="peer-a",
        )
        assert reopened["created"] is False
        with pytest.raises(ValueError, match="different filters"):
            store.open_consumer_group("node_b.tasks", kind_prefix="other.")
    finally:
        store.close()


def test_claim_lease_recovery_renew_nack_and_ack(tmp_path, monkeypatch) -> None:
    clock = [10_000]
    monkeypatch.setattr(store_module, "now_ms", lambda: clock[0])
    store = PacketStore(tmp_path / "lease.sqlite3")
    try:
        store.add_inbox(message(10), trusted=True)
        store.open_consumer_group("jobs", start="earliest")

        first = store.claim_consumer_messages("jobs", "worker-a", lease_seconds=5)[0]
        assert first["delivery_attempt"] == 1
        assert store.claim_consumer_messages("jobs", "worker-b", lease_seconds=5) == []

        clock[0] += 5001
        second = store.claim_consumer_messages("jobs", "worker-b", lease_seconds=5)[0]
        assert second["packet_id"] == first["packet_id"]
        assert second["claim_token"] != first["claim_token"]
        assert second["delivery_attempt"] == 2
        with pytest.raises(ValueError, match="stale"):
            store.acknowledge_claim("jobs", "worker-a", first["claim_token"])

        renewed = store.renew_claim(
            "jobs",
            "worker-b",
            second["claim_token"],
            lease_seconds=10,
        )
        assert renewed["lease_until_ms"] == clock[0] + 10_000
        retry = store.reject_claim(
            "jobs",
            "worker-b",
            second["claim_token"],
            retry_seconds=10,
            error="temporary",
        )
        assert retry["retry_after_ms"] == clock[0] + 10_000
        assert store.claim_consumer_messages("jobs", "worker-c") == []

        clock[0] += 10_000
        third = store.claim_consumer_messages("jobs", "worker-c")[0]
        assert third["delivery_attempt"] == 3
        acknowledged = store.acknowledge_claim("jobs", "worker-c", third["claim_token"])
        assert acknowledged["state"] == "acked"
        assert store.claim_consumer_messages("jobs", "worker-d") == []
        status = store.consumer_group_status("jobs")
        assert status["matching"] == 1
        assert status["available"] == 0
        assert status["states"] == {"acked": 1}
    finally:
        store.close()


def test_consumer_groups_are_fanout_but_workers_in_one_group_are_atomic(
    tmp_path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    first = PacketStore(path)
    second = PacketStore(path)
    try:
        first.add_inbox(message(20), trusted=True)
        first.open_consumer_group("research", start="earliest")
        first.open_consumer_group("audit", start="earliest")

        barrier = threading.Barrier(2)

        def claim(store: PacketStore, owner: str) -> list[dict]:
            barrier.wait()
            return store.claim_consumer_messages("research", owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(claim, first, "worker-a")
            b = pool.submit(claim, second, "worker-b")
            claims = [*a.result(), *b.result()]
        assert len(claims) == 1
        assert claims[0]["packet_id"] == f"{20:032x}"

        audit = second.claim_consumer_messages("audit", "auditor")
        assert [item["packet_id"] for item in audit] == [f"{20:032x}"]
    finally:
        first.close()
        second.close()


def test_purge_removes_orphan_consumer_delivery(tmp_path) -> None:
    store = PacketStore(tmp_path / "purge.sqlite3")
    try:
        store.add_inbox(message(30), trusted=True)
        store.open_consumer_group("purge", start="earliest")
        store.claim_consumer_messages("purge", "worker")
        with store._lock:
            store._conn.execute(
                "DELETE FROM inbox WHERE packet_id = ?", (f"{30:032x}",)
            )
        result = store.purge()
        assert result["orphan_consumer_deliveries"] == 1
        assert store.consumer_group_status("purge")["states"] == {}
    finally:
        store.close()
