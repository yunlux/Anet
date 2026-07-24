from __future__ import annotations

import sqlite3

import pytest

from anet.a2a_v1 import (
    A2A_PROTOCOL_VERSION,
    A2ASkillBinding,
    inbound_message_to_task,
    task_event_to_a2a_stream_events,
)
from anet.agent_protocol import task_result
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import inspect_packet, seal_packet
from anet.peers import PeerBook
from anet.prekeys import generate_prekey_bundle, import_prekey_bundle


SKILL = A2ASkillBinding(
    id="code.review",
    name="Code review",
    description="Review a source change.",
    tags=("code", "review"),
    required_capabilities=("code.review",),
)


def _trust(node: AnetNode, other: AnetNode) -> None:
    book = PeerBook(node.config.peers_path, own_node_id=node.node_id)
    book.add(other.local_card)
    node.peers.reload()


def _register(
    node: AnetNode,
    *,
    principal_id: str,
    destination_id: str,
    message_id: str,
) -> tuple[dict, object]:
    mapped = inbound_message_to_task(
        {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": "Review this patch"}],
            }
        },
        authenticated_sender_id=node.node_id,
        skill=SKILL,
    )
    registered = node.store.register_a2a_message(
        principal_id,
        authenticated_sender_id=node.node_id,
        a2a_task_id=mapped.a2a_task_id,
        context_id=mapped.context_id,
        message_id=mapped.message_id,
        destination_peer_id=destination_id,
        skill_id=mapped.skill_id,
        protocol_version=A2A_PROTOCOL_VERSION,
        request_body=mapped.body,
    )
    return registered, mapped


def test_node_dispatches_registered_a2a_intent_with_recoverable_prekey(
    tmp_path,
) -> None:
    gateway = AnetNode(initialize_node(tmp_path / "gateway", label="gateway"))
    recipient = AnetNode(initialize_node(tmp_path / "recipient", label="recipient"))
    principal_id = "gateway:user-a"
    try:
        _trust(gateway, recipient)
        _trust(recipient, gateway)
        gateway.store.bind_a2a_principal(
            principal_id,
            gateway.node_id,
            allowed_sender_nodes={gateway.node_id},
        )
        bundle = generate_prekey_bundle(
            recipient.identity,
            recipient.store,
            peer_id=gateway.node_id,
            count=2,
        )
        import_prekey_bundle(
            bundle,
            recipient.local_card,
            gateway.store,
            recipient_node_id=gateway.node_id,
        )
        registered, mapped = _register(
            gateway,
            principal_id=principal_id,
            destination_id=recipient.node_id,
            message_id="message-1",
        )
        assert registered["dispatch"]["state"] == "pending"
        claim = gateway.store.claim_a2a_dispatches("gateway-worker")[0]

        # A process may die after reserving a one-time prekey but before sealing
        # or committing the Packet. The stable outbox reservation ID recovers
        # the exact reservation instead of consuming another prekey.
        first_reservation = gateway.store.reserve_peer_prekey(
            recipient.node_id,
            reservation_id=claim["encryption_reservation_id"],
            min_bundle_version=2,
        )
        recovered_reservation = gateway.store.reserve_peer_prekey(
            recipient.node_id,
            reservation_id=claim["encryption_reservation_id"],
            min_bundle_version=2,
        )
        assert recovered_reservation["prekey_id"] == first_reservation["prekey_id"]

        dispatched = gateway.dispatch_a2a_claim(claim)
        assert dispatched["state"] == "dispatched"
        raw = gateway.store.get_packet(dispatched["packet_id"])
        assert raw is not None
        assert inspect_packet(raw).key_mode == "opk"
        assert (
            recipient.accept_carrier_packet(
                raw,
                depth=1,
                peer_id=gateway.node_id,
            )
            == dispatched["packet_id"]
        )
        inbox = recipient.store.list_inbox()
        assert inbox[0]["kind"] == "agent.task.request"
        assert inbox[0]["body"] == mapped.body

        duplicate, _ = _register(
            gateway,
            principal_id=principal_id,
            destination_id=recipient.node_id,
            message_id="message-1",
        )
        assert duplicate["duplicate"] is True
        assert duplicate["dispatch"]["packet_id"] == dispatched["packet_id"]
        assert gateway.drain_a2a_dispatches("gateway-worker")["claimed"] == 0

        cancellation = gateway.store.request_a2a_task_cancellation(
            principal_id,
            {"id": mapped.a2a_task_id, "metadata": {"trace": "external"}},
            authenticated_sender_id=gateway.node_id,
        )
        assert cancellation["cancel_state"] == "requested"
        assert len(cancellation["cancel_dispatches"]) == 1
        cancel_batch = gateway.drain_a2a_dispatches("gateway-worker")
        assert cancel_batch["dispatched"] == 1
        cancel_raw = gateway.store.get_packet(cancel_batch["packet_ids"][0])
        assert cancel_raw is not None
        recipient.accept_carrier_packet(
            cancel_raw,
            depth=1,
            peer_id=gateway.node_id,
        )
        inbox = recipient.store.list_inbox()
        by_kind = {item["kind"]: item for item in inbox}
        assert set(by_kind) == {"agent.task.request", "agent.task.cancel"}
        assert by_kind["agent.task.cancel"]["body"]["task_id"] == mapped.task_id
        persisted = gateway.store.a2a_gateway_task(
            principal_id,
            mapped.a2a_task_id,
            authenticated_sender_id=gateway.node_id,
        )
        assert persisted["cancel_state"] == "dispatched"

        canceled_events = task_event_to_a2a_stream_events(
            "agent.task.result",
            task_result(
                task_id=mapped.task_id,
                state="canceled",
                error="A2A task cancellation requested",
            ),
            context_id=mapped.context_id,
            a2a_task_id=mapped.a2a_task_id,
        )
        gateway.store.append_a2a_task_events(
            principal_id,
            authenticated_sender_id=gateway.node_id,
            a2a_task_id=mapped.a2a_task_id,
            source_anet_task_id=mapped.task_id,
            events=canceled_events,
        )
        confirmed = gateway.store.request_a2a_task_cancellation(
            principal_id,
            {"id": mapped.a2a_task_id},
            authenticated_sender_id=gateway.node_id,
        )
        assert confirmed["state"] == "canceled"
        assert confirmed["cancel_state"] == "confirmed"
        assert confirmed["duplicate"] is True
    finally:
        gateway.close()
        recipient.close()


def test_node_retries_intent_when_destination_is_not_trusted(tmp_path) -> None:
    gateway = AnetNode(initialize_node(tmp_path / "gateway", label="gateway"))
    principal_id = "gateway:user-a"
    unknown = Identity.generate("unknown")
    try:
        gateway.store.bind_a2a_principal(
            principal_id,
            gateway.node_id,
            allowed_sender_nodes={gateway.node_id},
        )
        _register(
            gateway,
            principal_id=principal_id,
            destination_id=unknown.node_id,
            message_id="message-1",
        )
        result = gateway.drain_a2a_dispatches(
            "gateway-worker",
            retry_seconds=0,
        )
        assert result["claimed"] == 1
        assert result["dispatched"] == 0
        assert result["failed"] == 1
        state = gateway.store.a2a_gateway_dispatch(
            principal_id,
            "message-1",
            authenticated_sender_id=gateway.node_id,
        )
        assert state["state"] == "retry"
        assert state["packet_id"] == ""
        assert gateway.store.status()["a2a_gateway"]["pending_dispatches"] == 1
    finally:
        gateway.close()


def test_packet_commit_rolls_back_prekey_and_dispatch_on_insert_failure(
    tmp_path,
) -> None:
    gateway = AnetNode(initialize_node(tmp_path / "gateway", label="gateway"))
    recipient = AnetNode(initialize_node(tmp_path / "recipient", label="recipient"))
    principal_id = "gateway:user-a"
    try:
        _trust(gateway, recipient)
        gateway.store.bind_a2a_principal(
            principal_id,
            gateway.node_id,
            allowed_sender_nodes={gateway.node_id},
        )
        bundle = generate_prekey_bundle(
            recipient.identity,
            recipient.store,
            peer_id=gateway.node_id,
            count=1,
        )
        import_prekey_bundle(
            bundle,
            recipient.local_card,
            gateway.store,
            recipient_node_id=gateway.node_id,
        )
        _register(
            gateway,
            principal_id=principal_id,
            destination_id=recipient.node_id,
            message_id="message-1",
        )
        claim = gateway.store.claim_a2a_dispatches("gateway-worker")[0]
        reservation = gateway.store.reserve_peer_prekey(
            recipient.node_id,
            reservation_id=claim["encryption_reservation_id"],
            min_bundle_version=2,
        )
        raw = seal_packet(
            gateway.identity,
            recipient.local_card,
            kind=claim["kind"],
            body=claim["body"],
            recipient_prekey_public=reservation["public_key"],
            recipient_prekey_id=reservation["prekey_id"],
        )
        gateway.store.add_packet(raw, origin="collision")

        with pytest.raises(sqlite3.IntegrityError):
            gateway.store.commit_a2a_dispatch_packet(
                claim["owner"],
                claim["claim_token"],
                raw,
                prekey_id=reservation["prekey_id"],
                prekey_reservation_id=reservation["reservation_id"],
            )

        counts = gateway.store.prekey_status(recipient.node_id)["peers"][
            recipient.node_id
        ]["counts"]
        assert counts == {"reserved": 1}
        dispatch = gateway.store.a2a_gateway_dispatch(
            principal_id,
            "message-1",
            authenticated_sender_id=gateway.node_id,
        )
        assert dispatch["state"] == "leased"
        gateway.store.retry_a2a_dispatch(
            claim["owner"],
            claim["claim_token"],
            error="collision",
        )
        counts = gateway.store.prekey_status(recipient.node_id)["peers"][
            recipient.node_id
        ]["counts"]
        assert counts == {"burned": 1}
    finally:
        gateway.close()
        recipient.close()
