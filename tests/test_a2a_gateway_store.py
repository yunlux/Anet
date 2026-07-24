from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from anet.a2a_v1 import (
    A2A_PROTOCOL_VERSION,
    A2ASkillBinding,
    inbound_message_to_task,
    task_event_to_a2a_stream_events,
)
from anet.agent_protocol import task_result, task_status
from anet.identity import Identity
from anet.packet import open_packet, seal_packet
from anet.store import PacketStore


SENDER_A = "an1" + "a" * 32
SENDER_B = "an1" + "b" * 32
DESTINATION = "an1" + "d" * 32
SKILL = A2ASkillBinding(
    id="code.review",
    name="Code review",
    description="Review a source change.",
    tags=("code", "review"),
    required_capabilities=("code.review",),
)


def request(
    message_id: str,
    *,
    text: str = "Review this patch",
    task_id: str = "",
    context_id: str = "",
) -> dict:
    message = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return {"message": message}


def bind(
    store: PacketStore,
    principal_id: str = "gateway:user-a",
    sender_node_id: str = SENDER_A,
) -> dict:
    return store.bind_a2a_principal(
        principal_id,
        sender_node_id,
        allowed_sender_nodes={sender_node_id},
    )


def register(
    store: PacketStore,
    principal_id: str,
    mapped,
) -> dict:
    return store.register_a2a_message(
        principal_id,
        authenticated_sender_id=store.a2a_principal(principal_id)["sender_node_id"],
        a2a_task_id=mapped.a2a_task_id,
        context_id=mapped.context_id,
        message_id=mapped.message_id,
        destination_peer_id=DESTINATION,
        skill_id=mapped.skill_id,
        protocol_version=A2A_PROTOCOL_VERSION,
        request_body=mapped.body,
    )


def test_principal_binding_is_allowlisted_stable_and_persistent(tmp_path) -> None:
    path = tmp_path / "a2a.sqlite3"
    store = PacketStore(path)
    try:
        with pytest.raises(PermissionError, match="binding policy"):
            store.bind_a2a_principal(
                "gateway:user-a",
                SENDER_A,
                allowed_sender_nodes={SENDER_B},
            )
        first = bind(store)
        duplicate = bind(store)
        assert first["duplicate"] is False
        assert duplicate["duplicate"] is True
        with pytest.raises(PermissionError, match="another Node ID"):
            store.bind_a2a_principal(
                "gateway:user-a",
                SENDER_B,
                allowed_sender_nodes={SENDER_B},
            )
    finally:
        store.close()

    reopened = PacketStore(path)
    try:
        assert reopened.a2a_principal("gateway:user-a")["sender_node_id"] == SENDER_A
    finally:
        reopened.close()


def test_message_registration_is_durable_and_body_bound(tmp_path) -> None:
    path = tmp_path / "message.sqlite3"
    store = PacketStore(path)
    bind(store)
    mapped = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    try:
        first = register(store, "gateway:user-a", mapped)
        duplicate = register(store, "gateway:user-a", mapped)
        assert first["duplicate"] is False
        assert duplicate["duplicate"] is True
        assert first["message_count"] == 1
        assert duplicate["message_count"] == 1
        assert store.status()["a2a_gateway"] == {
            "principals": 1,
            "tasks": 1,
            "messages": 1,
            "events": 0,
            "dispatches": 1,
            "pending_dispatches": 1,
        }

        changed = inbound_message_to_task(
            request("message-1", text="Delete the repository"),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
        )
        with pytest.raises(ValueError, match="different mapping or body"):
            register(store, "gateway:user-a", changed)
        with pytest.raises(PermissionError, match="principal binding"):
            store.register_a2a_message(
                "gateway:user-a",
                authenticated_sender_id=SENDER_B,
                a2a_task_id=mapped.a2a_task_id,
                context_id=mapped.context_id,
                message_id=mapped.message_id,
                destination_peer_id=DESTINATION,
                skill_id=mapped.skill_id,
                protocol_version=A2A_PROTOCOL_VERSION,
                request_body=mapped.body,
            )
    finally:
        store.close()

    reopened = PacketStore(path)
    try:
        task = reopened.a2a_gateway_task(
            "gateway:user-a",
            mapped.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        assert task["message_count"] == 1
        assert task["sender_node_id"] == SENDER_A
        assert reopened.a2a_gateway_message(
            "gateway:user-a",
            "message-1",
            authenticated_sender_id=SENDER_A,
        )["anet_task_id"] == mapped.task_id
    finally:
        reopened.close()


def test_follow_up_resolves_same_a2a_task_to_a_new_anet_task(tmp_path) -> None:
    store = PacketStore(tmp_path / "follow-up.sqlite3")
    bind(store)
    initial = inbound_message_to_task(
        request("message-1", context_id="context-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    try:
        register(store, "gateway:user-a", initial)
        persisted = store.a2a_gateway_task(
            "gateway:user-a",
            initial.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        follow_up = inbound_message_to_task(
            request(
                "message-2",
                text="Use the main branch",
                task_id=initial.a2a_task_id,
            ),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
            existing_task=persisted,
        )
        registered = register(store, "gateway:user-a", follow_up)

        assert follow_up.a2a_task_id == initial.a2a_task_id
        assert follow_up.task_id != initial.task_id
        assert registered["message_count"] == 2
        assert registered["latest_anet_task_id"] == follow_up.task_id
        assert registered["context_id"] == "context-1"
    finally:
        store.close()


def test_follow_up_cannot_change_context_peer_skill_or_protocol(tmp_path) -> None:
    store = PacketStore(tmp_path / "conflicts.sqlite3")
    bind(store)
    initial = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    try:
        register(store, "gateway:user-a", initial)
        persisted = store.a2a_gateway_task(
            "gateway:user-a",
            initial.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        follow_up = inbound_message_to_task(
            request("message-2", task_id=initial.a2a_task_id),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
            existing_task=persisted,
        )
        with pytest.raises(ValueError, match="context, peer, skill, or version"):
            store.register_a2a_message(
                "gateway:user-a",
                authenticated_sender_id=SENDER_A,
                a2a_task_id=follow_up.a2a_task_id,
                context_id=follow_up.context_id,
                message_id=follow_up.message_id,
                destination_peer_id="an1" + "e" * 32,
                skill_id=follow_up.skill_id,
                protocol_version=A2A_PROTOCOL_VERSION,
                request_body=follow_up.body,
            )
        with pytest.raises(ValueError, match="does not match"):
            store.register_a2a_message(
                "gateway:user-a",
                authenticated_sender_id=SENDER_A,
                a2a_task_id=follow_up.a2a_task_id,
                context_id="wrong-context",
                message_id=follow_up.message_id,
                destination_peer_id=DESTINATION,
                skill_id=follow_up.skill_id,
                protocol_version=A2A_PROTOCOL_VERSION,
                request_body=follow_up.body,
            )
    finally:
        store.close()


def test_a2a_cancellation_is_idempotent_and_targets_all_internal_tasks(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "cancel-multi-turn.sqlite3")
    bind(store)
    initial = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    try:
        register(store, "gateway:user-a", initial)
        persisted = store.a2a_gateway_task(
            "gateway:user-a",
            initial.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        follow_up = inbound_message_to_task(
            request("message-2", task_id=initial.a2a_task_id),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
            existing_task=persisted,
        )
        register(store, "gateway:user-a", follow_up)

        canceled = store.request_a2a_task_cancellation(
            "gateway:user-a",
            {
                "id": initial.a2a_task_id,
                "metadata": {"untrustedReason": "erase everything"},
            },
            authenticated_sender_id=SENDER_A,
        )
        assert canceled["cancel_state"] == "requested"
        assert canceled["duplicate"] is False
        assert len(canceled["cancel_dispatches"]) == 2
        duplicate = store.request_a2a_task_cancellation(
            "gateway:user-a",
            {"id": initial.a2a_task_id},
            authenticated_sender_id=SENDER_A,
        )
        assert duplicate["duplicate"] is True
        assert len(duplicate["cancel_dispatches"]) == 2

        claims = store.claim_a2a_dispatches("gateway-worker", limit=10)
        cancel_claims = [
            claim for claim in claims if claim["kind"] == "agent.task.cancel"
        ]
        assert {claim["body"]["task_id"] for claim in cancel_claims} == {
            initial.task_id,
            follow_up.task_id,
        }
        assert {
            claim["body"]["reason"] for claim in cancel_claims
        } == {"A2A task cancellation requested"}

        current = store.a2a_gateway_task(
            "gateway:user-a",
            initial.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        third = inbound_message_to_task(
            request("message-3", task_id=initial.a2a_task_id),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
            existing_task=current,
        )
        with pytest.raises(ValueError, match="cancellation request"):
            register(store, "gateway:user-a", third)
        with pytest.raises(PermissionError, match="sender"):
            store.request_a2a_task_cancellation(
                "gateway:user-a",
                {"id": initial.a2a_task_id},
                authenticated_sender_id=SENDER_B,
            )
    finally:
        store.close()


def test_identical_external_ids_are_isolated_by_authenticated_principal(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "principal-scope.sqlite3")
    bind(store, "gateway:user-a", SENDER_A)
    bind(store, "gateway:user-b", SENDER_B)
    first = inbound_message_to_task(
        request("same-message"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    second = inbound_message_to_task(
        request("same-message"),
        authenticated_sender_id=SENDER_B,
        skill=SKILL,
    )
    try:
        register(store, "gateway:user-a", first)
        register(store, "gateway:user-b", second)
        assert first.task_id != second.task_id
        assert store.status()["a2a_gateway"] == {
            "principals": 2,
            "tasks": 2,
            "messages": 2,
            "events": 0,
            "dispatches": 2,
            "pending_dispatches": 2,
        }
    finally:
        store.close()


def test_concurrent_duplicate_registration_creates_one_message(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    first_store = PacketStore(path)
    second_store = PacketStore(path)
    bind(first_store)
    mapped = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    barrier = threading.Barrier(2)

    def run(store: PacketStore) -> dict:
        barrier.wait()
        return register(store, "gateway:user-a", mapped)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (first_store, second_store)))
        assert sorted(result["duplicate"] for result in results) == [False, True]
        assert first_store.status()["a2a_gateway"]["messages"] == 1
    finally:
        first_store.close()
        second_store.close()


def test_event_log_is_idempotent_resumable_and_persistent(tmp_path) -> None:
    path = tmp_path / "events.sqlite3"
    store = PacketStore(path)
    bind(store)
    mapped = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    register(store, "gateway:user-a", mapped)
    working = task_event_to_a2a_stream_events(
        "agent.task.status",
        task_status(
            task_id=mapped.task_id,
            state="working",
            message="reviewing",
            progress=0.25,
        ),
        context_id=mapped.context_id,
        a2a_task_id=mapped.a2a_task_id,
    )
    completed = task_event_to_a2a_stream_events(
        "agent.task.result",
        task_result(
            task_id=mapped.task_id,
            state="completed",
            output={"verdict": "approve"},
        ),
        context_id=mapped.context_id,
        a2a_task_id=mapped.a2a_task_id,
    )
    try:
        first = store.append_a2a_task_events(
            "gateway:user-a",
            authenticated_sender_id=SENDER_A,
            a2a_task_id=mapped.a2a_task_id,
            source_anet_task_id=mapped.task_id,
            events=working,
        )
        duplicate = store.append_a2a_task_events(
            "gateway:user-a",
            authenticated_sender_id=SENDER_A,
            a2a_task_id=mapped.a2a_task_id,
            source_anet_task_id=mapped.task_id,
            events=working,
        )
        terminal = store.append_a2a_task_events(
            "gateway:user-a",
            authenticated_sender_id=SENDER_A,
            a2a_task_id=mapped.a2a_task_id,
            source_anet_task_id=mapped.task_id,
            events=completed,
        )
        assert first == {
            "principal_id": "gateway:user-a",
            "a2a_task_id": mapped.a2a_task_id,
            "state": "working",
            "last_sequence": 1,
            "appended": 1,
            "duplicates": 0,
        }
        assert duplicate["appended"] == 0
        assert duplicate["duplicates"] == 1
        assert terminal["state"] == "completed"
        assert terminal["last_sequence"] == 3
        assert store.status()["a2a_gateway"]["events"] == 3

        first_page = store.read_a2a_task_events(
            "gateway:user-a",
            mapped.a2a_task_id,
            authenticated_sender_id=SENDER_A,
            limit=2,
        )
        assert [event["sequence"] for event in first_page["events"]] == [1, 2]
        assert first_page["caught_up"] is False
        second_page = store.read_a2a_task_events(
            "gateway:user-a",
            mapped.a2a_task_id,
            authenticated_sender_id=SENDER_A,
            after_sequence=first_page["next_sequence"],
        )
        assert [event["sequence"] for event in second_page["events"]] == [3]
        assert second_page["state"] == "completed"
        assert second_page["caught_up"] is True

        with pytest.raises(ValueError, match="terminal"):
            register(
                store,
                "gateway:user-a",
                inbound_message_to_task(
                    request(
                        "message-2",
                        task_id=mapped.a2a_task_id,
                    ),
                    authenticated_sender_id=SENDER_A,
                    skill=SKILL,
                    existing_task=store.a2a_gateway_task(
                        "gateway:user-a",
                        mapped.a2a_task_id,
                        authenticated_sender_id=SENDER_A,
                    ),
                ),
            )
    finally:
        store.close()

    reopened = PacketStore(path)
    try:
        page = reopened.read_a2a_task_events(
            "gateway:user-a",
            mapped.a2a_task_id,
            authenticated_sender_id=SENDER_A,
        )
        assert page["last_sequence"] == 3
        assert page["events"][1]["event"]["artifactUpdate"]["artifact"][
            "artifactId"
        ] == "result"
    finally:
        reopened.close()


def test_event_log_rejects_wrong_owner_invalid_transition_and_stale_source(
    tmp_path,
) -> None:
    store = PacketStore(tmp_path / "event-fencing.sqlite3")
    bind(store)
    initial = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    register(store, "gateway:user-a", initial)
    working = task_event_to_a2a_stream_events(
        "agent.task.status",
        task_status(task_id=initial.task_id, state="working"),
        context_id=initial.context_id,
        a2a_task_id=initial.a2a_task_id,
    )
    try:
        with pytest.raises(PermissionError, match="does not own"):
            store.append_a2a_task_events(
                "gateway:user-a",
                authenticated_sender_id=SENDER_B,
                a2a_task_id=initial.a2a_task_id,
                source_anet_task_id=initial.task_id,
                events=working,
            )

        follow_up = inbound_message_to_task(
            request("message-2", task_id=initial.a2a_task_id),
            authenticated_sender_id=SENDER_A,
            skill=SKILL,
            existing_task=store.a2a_gateway_task(
                "gateway:user-a",
                initial.a2a_task_id,
                authenticated_sender_id=SENDER_A,
            ),
        )
        register(store, "gateway:user-a", follow_up)
        with pytest.raises(ValueError, match="stale"):
            store.append_a2a_task_events(
                "gateway:user-a",
                authenticated_sender_id=SENDER_A,
                a2a_task_id=initial.a2a_task_id,
                source_anet_task_id=initial.task_id,
                events=working,
            )

        completed = task_event_to_a2a_stream_events(
            "agent.task.result",
            task_result(task_id=follow_up.task_id, state="completed"),
            context_id=follow_up.context_id,
            a2a_task_id=follow_up.a2a_task_id,
        )
        store.append_a2a_task_events(
            "gateway:user-a",
            authenticated_sender_id=SENDER_A,
            a2a_task_id=follow_up.a2a_task_id,
            source_anet_task_id=follow_up.task_id,
            events=completed,
        )
        with pytest.raises(ValueError, match="transition"):
            store.append_a2a_task_events(
                "gateway:user-a",
                authenticated_sender_id=SENDER_A,
                a2a_task_id=follow_up.a2a_task_id,
                source_anet_task_id=follow_up.task_id,
                events=task_event_to_a2a_stream_events(
                    "agent.task.status",
                    task_status(task_id=follow_up.task_id, state="working"),
                    context_id=follow_up.context_id,
                    a2a_task_id=follow_up.a2a_task_id,
                ),
            )
    finally:
        store.close()


def test_dispatch_outbox_claim_retry_fencing_commit_and_restart(tmp_path) -> None:
    path = tmp_path / "dispatch.sqlite3"
    first_store = PacketStore(path)
    second_store = PacketStore(path)
    bind(first_store)
    recipient = Identity.generate("recipient")
    packet_sender = Identity.generate("gateway")
    mapped = inbound_message_to_task(
        request("message-1"),
        authenticated_sender_id=SENDER_A,
        skill=SKILL,
    )
    registered = first_store.register_a2a_message(
        "gateway:user-a",
        authenticated_sender_id=SENDER_A,
        a2a_task_id=mapped.a2a_task_id,
        context_id=mapped.context_id,
        message_id=mapped.message_id,
        destination_peer_id=recipient.node_id,
        skill_id=mapped.skill_id,
        protocol_version=A2A_PROTOCOL_VERSION,
        request_body=mapped.body,
    )
    assert registered["dispatch"]["state"] == "pending"

    barrier = threading.Barrier(2)

    def claim(store: PacketStore, owner: str) -> list[dict]:
        barrier.wait()
        return store.claim_a2a_dispatches(owner, limit=1)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(claim, first_store, "worker-a"),
                pool.submit(claim, second_store, "worker-b"),
            ]
            results = [future.result() for future in futures]
        assert sorted(len(result) for result in results) == [0, 1]
        leased = next(result[0] for result in results if result)
        old_token = leased["claim_token"]
        old_reservation = leased["encryption_reservation_id"]
        retry = first_store.retry_a2a_dispatch(
            leased["owner"],
            old_token,
            error="simulated process failure",
        )
        assert retry["state"] == "retry"
        reclaimed = second_store.claim_a2a_dispatches("worker-c", limit=1)[0]
        assert reclaimed["attempts"] == 2
        assert reclaimed["claim_token"] != old_token
        assert reclaimed["encryption_reservation_id"] != old_reservation

        raw = seal_packet(
            packet_sender,
            recipient.card(),
            kind=reclaimed["kind"],
            body=reclaimed["body"],
            packet_version=2,
        )
        with pytest.raises(ValueError, match="stale"):
            first_store.commit_a2a_dispatch_packet(
                leased["owner"],
                old_token,
                raw,
            )

        dispatched = second_store.commit_a2a_dispatch_packet(
            reclaimed["owner"],
            reclaimed["claim_token"],
            raw,
        )
        assert dispatched["state"] == "dispatched"
        assert dispatched["packet_id"]
        assert second_store.status()["a2a_gateway"]["pending_dispatches"] == 0
        opened = open_packet(recipient, second_store.get_packet(dispatched["packet_id"]))
        assert opened.kind == "agent.task.request"
        assert opened.body == mapped.body
        with pytest.raises(ValueError, match="stale"):
            second_store.retry_a2a_dispatch(
                reclaimed["owner"],
                reclaimed["claim_token"],
                error="late failure",
            )
    finally:
        first_store.close()
        second_store.close()

    reopened = PacketStore(path)
    try:
        persisted = reopened.a2a_gateway_dispatch(
            "gateway:user-a",
            "message-1",
            authenticated_sender_id=SENDER_A,
        )
        assert persisted["state"] == "dispatched"
        assert reopened.has_packet(persisted["packet_id"])
        assert reopened.claim_a2a_dispatches("worker-d") == []
    finally:
        reopened.close()
