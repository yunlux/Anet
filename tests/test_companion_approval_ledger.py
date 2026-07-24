from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time

import pytest

from anet.companion_protocol import (
    APPROVAL_DECISION_KIND,
    APPROVAL_REQUEST_KIND,
    approval_decision,
    approval_request,
)
from anet.config import initialize_node
from anet.control_plane import (
    ControlPlaneStore,
    HumanPrincipalIdentity,
    issue_human_device_grant,
    issue_human_device_revocation,
    issue_node_descriptor,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import seal_packet
from anet.peers import PeerBook


def paired_nodes(tmp_path: Path) -> tuple[AnetNode, AnetNode]:
    sender_base = initialize_node(
        tmp_path / "node_b",
        label="node_b",
        listen_port=0,
    )
    phone_base = initialize_node(
        tmp_path / "phone",
        label="phone",
        listen_port=0,
    )
    sender_config = replace(
        sender_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    phone_config = replace(
        phone_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    sender_identity = Identity.load(sender_config.identity_path)
    phone_identity = Identity.load(phone_config.identity_path)
    PeerBook(
        sender_config.peers_path,
        own_node_id=sender_identity.node_id,
    ).add(
        phone_identity.card(
            addresses=(),
            capabilities=phone_config.capabilities,
        )
    )
    PeerBook(
        phone_config.peers_path,
        own_node_id=phone_identity.node_id,
    ).add(
        sender_identity.card(
            addresses=(),
            capabilities=sender_config.capabilities,
        )
    )
    return AnetNode(sender_config), AnetNode(phone_config)


def deliver(sender: AnetNode, recipient: AnetNode, packet_id: str) -> None:
    raw = sender.store.get_packet(packet_id)
    assert raw is not None
    assert recipient.accept_carrier_packet(
        raw,
        depth=1,
        peer_id=sender.node_id,
    ) == packet_id


def request_body(
    *,
    human_id: str,
    phone_id: str,
    created_ms: int,
    request_id: str = "11" * 16,
    nonce: str = "12" * 16,
    mode: str = "once",
    max_uses: int = 1,
) -> dict[str, object]:
    return approval_request(
        human_id=human_id,
        device_node_id=phone_id,
        action={
            "capability": "service.restart",
            "resource": "anet://node/ahub/service/relay",
            "parameters_digest": "ab" * 32,
            "summary": "重启 Relay 服务",
        },
        scope={
            "mode": mode,
            "max_uses": max_uses,
            "grant_expires_ms": created_ms + 30 * 60 * 1000,
        },
        risk="会短暂中断实时 Relay。",
        request_id=request_id,
        nonce=nonce,
        created_ms=created_ms,
        expires_ms=created_ms + 5 * 60 * 1000,
    )


def configure_grant(
    control: ControlPlaneStore,
    phone: AnetNode,
    human: HumanPrincipalIdentity,
    *,
    capabilities: tuple[str, ...],
    issued_ms: int,
) -> tuple[ControlPlaneStore, object, object]:
    descriptor = issue_node_descriptor(
        phone.identity,
        capabilities=phone.config.capabilities,
        issued_ms=issued_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    grant = issue_human_device_grant(
        human,
        descriptor,
        capabilities=capabilities,
        issued_ms=issued_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    control.accept_descriptor(descriptor, now=issued_ms)
    control.accept_human_grant(grant, descriptor, now=issued_ms)
    return control, descriptor, grant


def queue_decision(
    node_b: AnetNode,
    phone: AnetNode,
    request: dict[str, object],
    *,
    created_ms: int,
    decision_id: str = "13" * 16,
) -> str:
    decision = approval_decision(
        request=request,
        decision="approved",
        decision_id=decision_id,
        created_ms=created_ms,
        expires_ms=int(request["expires_ms"]),
    )
    packet_id = phone.queue(
        node_b.node_id,
        kind=APPROVAL_DECISION_KIND,
        body=decision,
        qos="control",
    )
    deliver(phone, node_b, packet_id)
    return packet_id


def test_approval_activation_and_effect_execution_are_durable_and_fenced(
    tmp_path: Path,
) -> None:
    node_b, phone = paired_nodes(tmp_path)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    control, _, _ = configure_grant(
        node_b.control,
        phone,
        human,
        capabilities=("approval.sign",),
        issued_ms=current - 1_000,
    )
    request = request_body(
        human_id=human.human_id,
        phone_id=phone.node_id,
        created_ms=current,
    )
    try:
        request_packet = node_b.queue(
            phone.node_id,
            kind=APPROVAL_REQUEST_KIND,
            body=request,
            qos="control",
        )
        deliver(node_b, phone, request_packet)
        queue_decision(
            node_b,
            phone,
            request,
            created_ms=current + 1_000,
        )

        node_b.store.open_consumer_group(
            "approval-executor",
            start="earliest",
            kind_prefix=APPROVAL_DECISION_KIND,
        )
        claim = node_b.store.claim_consumer_messages(
            "approval-executor",
            "worker-a",
        )[0]
        activated = node_b.store.activate_companion_approval(
            "approval-executor",
            "worker-a",
            claim["claim_token"],
            control,
            current_ms=current + 2_000,
        )
        assert activated["state"] == "active"
        assert activated["activated"] is True
        assert node_b.store.consumer_group_status("approval-executor")[
            "states"
        ] == {"acked": 1}

        effect_id = "14" * 16
        execution = node_b.store.begin_companion_approval_effect(
            str(request["request_id"]),
            effect_id,
            "executor-a",
            control,
            current_ms=current + 3_000,
        )
        assert execution["acquired"] is True
        assert len(execution["execution_token"]) == 32
        assert len(execution["effect_idempotency_key"]) == 64

        same_worker = node_b.store.begin_companion_approval_effect(
            str(request["request_id"]),
            effect_id,
            "executor-a",
            control,
            current_ms=current + 3_001,
        )
        assert same_worker["acquired"] is False
        assert same_worker["execution_token"] == execution["execution_token"]
        with pytest.raises(ValueError, match="another worker"):
            node_b.store.begin_companion_approval_effect(
                str(request["request_id"]),
                effect_id,
                "executor-b",
                control,
                current_ms=current + 3_001,
            )

        settled = node_b.store.settle_companion_approval_effect(
            str(request["request_id"]),
            effect_id,
            execution["execution_token"],
            outcome="executed",
            result={"service": "restarted"},
            current_ms=current + 4_000,
        )
        assert settled["state"] == "executed"
        assert settled["result"] == {"service": "restarted"}
        assert node_b.store.status()["companion_approvals"] == {
            "requests": 1,
            "active": 1,
            "effects": {"executed": 1},
        }
        with pytest.raises(ValueError, match="stale or fenced"):
            node_b.store.settle_companion_approval_effect(
                str(request["request_id"]),
                effect_id,
                execution["execution_token"],
                outcome="executed",
                current_ms=current + 4_001,
            )
        with pytest.raises(PermissionError, match="use limit"):
            node_b.store.begin_companion_approval_effect(
                str(request["request_id"]),
                "15" * 16,
                "executor-a",
                control,
                current_ms=current + 5_000,
            )

        node_b.close()
        node_b = AnetNode(node_b.config)
        persisted = node_b.store.companion_approval_effect(
            str(request["request_id"]),
            effect_id,
        )
        assert persisted is not None
        assert persisted["state"] == "executed"
        assert persisted["effect_idempotency_key"] == (
            execution["effect_idempotency_key"]
        )
    finally:
        node_b.close()
        phone.close()


def test_activation_requires_exact_local_request_sender_and_current_grant(
    tmp_path: Path,
) -> None:
    node_b, phone = paired_nodes(tmp_path)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    control, descriptor, grant = configure_grant(
        node_b.control,
        phone,
        human,
        capabilities=("notify.present",),
        issued_ms=current - 1_000,
    )
    request = request_body(
        human_id=human.human_id,
        phone_id=phone.node_id,
        created_ms=current,
        mode="bounded",
        max_uses=2,
    )
    try:
        node_b.queue(
            phone.node_id,
            kind=APPROVAL_REQUEST_KIND,
            body=request,
            qos="control",
        )
        queue_decision(
            node_b,
            phone,
            request,
            created_ms=current + 1_000,
        )
        node_b.store.open_consumer_group(
            "approval-policy",
            start="earliest",
            kind_prefix=APPROVAL_DECISION_KIND,
        )
        claim = node_b.store.claim_consumer_messages(
            "approval-policy",
            "worker",
        )[0]
        with pytest.raises(PermissionError, match="lacks approval.sign"):
            node_b.store.activate_companion_approval(
                "approval-policy",
                "worker",
                claim["claim_token"],
                control,
                current_ms=current + 2_000,
            )

        replacement = issue_human_device_grant(
            human,
            descriptor,
            capabilities=("approval.sign",),
            sequence=2,
            previous_digest=grant.digest,
            issued_ms=current + 2_000,
            ttl_ms=24 * 60 * 60 * 1000,
        )
        control.accept_human_grant(
            replacement,
            descriptor,
            now=current + 2_000,
        )
        node_b.store.activate_companion_approval(
            "approval-policy",
            "worker",
            claim["claim_token"],
            control,
            current_ms=current + 2_001,
        )

        revocation = issue_human_device_revocation(
            human,
            descriptor,
            sequence=3,
            previous_digest=replacement.digest,
            revoked_ms=current + 3_000,
        )
        control.accept_human_revocation(
            revocation,
            descriptor,
            now=current + 3_000,
        )
        with pytest.raises(PermissionError, match="revoked"):
            node_b.store.begin_companion_approval_effect(
                str(request["request_id"]),
                "16" * 16,
                "executor",
                control,
                current_ms=current + 3_001,
            )
    finally:
        node_b.close()
        phone.close()


def test_bounded_approval_recovers_expired_worker_and_keeps_effect_key(
    tmp_path: Path,
) -> None:
    node_b, phone = paired_nodes(tmp_path)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    control, _, _ = configure_grant(
        node_b.control,
        phone,
        human,
        capabilities=("approval.sign",),
        issued_ms=current - 1_000,
    )
    request = request_body(
        human_id=human.human_id,
        phone_id=phone.node_id,
        created_ms=current,
        request_id="21" * 16,
        nonce="22" * 16,
        mode="bounded",
        max_uses=2,
    )
    try:
        node_b.queue(
            phone.node_id,
            kind=APPROVAL_REQUEST_KIND,
            body=request,
            qos="control",
        )
        queue_decision(
            node_b,
            phone,
            request,
            created_ms=current + 1_000,
            decision_id="23" * 16,
        )
        node_b.store.open_consumer_group(
            "approval-bounded",
            start="earliest",
            kind_prefix=APPROVAL_DECISION_KIND,
        )
        claim = node_b.store.claim_consumer_messages(
            "approval-bounded",
            "activator",
        )[0]
        node_b.store.activate_companion_approval(
            "approval-bounded",
            "activator",
            claim["claim_token"],
            control,
            current_ms=current + 2_000,
        )

        request_id = str(request["request_id"])
        effect_one = "24" * 16
        first = node_b.store.begin_companion_approval_effect(
            request_id,
            effect_one,
            "worker-a",
            control,
            lease_seconds=5,
            current_ms=current + 3_000,
        )
        recovered = node_b.store.begin_companion_approval_effect(
            request_id,
            effect_one,
            "worker-b",
            control,
            lease_seconds=5,
            current_ms=current + 8_001,
        )
        assert recovered["acquired"] is True
        assert recovered["execution_token"] != first["execution_token"]
        assert recovered["effect_idempotency_key"] == (
            first["effect_idempotency_key"]
        )
        with pytest.raises(ValueError, match="stale or fenced"):
            node_b.store.settle_companion_approval_effect(
                request_id,
                effect_one,
                first["execution_token"],
                outcome="executed",
                current_ms=current + 8_002,
            )
        retried = node_b.store.settle_companion_approval_effect(
            request_id,
            effect_one,
            recovered["execution_token"],
            outcome="retry",
            error="temporary transport failure",
            retry_seconds=2,
            current_ms=current + 8_003,
        )
        assert retried["state"] == "retry"
        with pytest.raises(ValueError, match="not due"):
            node_b.store.begin_companion_approval_effect(
                request_id,
                effect_one,
                "worker-c",
                control,
                current_ms=current + 9_000,
            )
        third = node_b.store.begin_companion_approval_effect(
            request_id,
            effect_one,
            "worker-c",
            control,
            current_ms=current + 10_004,
        )
        assert third["attempts"] == 3
        assert third["effect_idempotency_key"] == first["effect_idempotency_key"]
        node_b.store.settle_companion_approval_effect(
            request_id,
            effect_one,
            third["execution_token"],
            outcome="executed",
            current_ms=current + 11_000,
        )

        second = node_b.store.begin_companion_approval_effect(
            request_id,
            "25" * 16,
            "worker-c",
            control,
            current_ms=current + 12_000,
        )
        node_b.store.settle_companion_approval_effect(
            request_id,
            "25" * 16,
            second["execution_token"],
            outcome="executed",
            current_ms=current + 13_000,
        )
        with pytest.raises(PermissionError, match="use limit"):
            node_b.store.begin_companion_approval_effect(
                request_id,
                "26" * 16,
                "worker-c",
                control,
                current_ms=current + 14_000,
            )
    finally:
        node_b.close()
        phone.close()


def test_tampered_decision_and_nonce_reuse_fail_closed(tmp_path: Path) -> None:
    node_b, phone = paired_nodes(tmp_path)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    control, _, _ = configure_grant(
        node_b.control,
        phone,
        human,
        capabilities=("approval.sign",),
        issued_ms=current - 1_000,
    )
    request = request_body(
        human_id=human.human_id,
        phone_id=phone.node_id,
        created_ms=current,
    )
    try:
        node_b.queue(
            phone.node_id,
            kind=APPROVAL_REQUEST_KIND,
            body=request,
            qos="control",
        )
        reused_nonce = request_body(
            human_id=human.human_id,
            phone_id=phone.node_id,
            created_ms=current,
            request_id="17" * 16,
            nonce=str(request["nonce"]),
        )
        with pytest.raises(ValueError, match="nonce"):
            node_b.queue(
                phone.node_id,
                kind=APPROVAL_REQUEST_KIND,
                body=reused_nonce,
                qos="control",
            )

        decision = approval_decision(
            request=request,
            decision="approved",
            decision_id="18" * 16,
            created_ms=current + 1_000,
            expires_ms=int(request["expires_ms"]),
        )
        tampered = deepcopy(decision)
        tampered["action"]["parameters_digest"] = "cd" * 32
        raw = seal_packet(
            phone.identity,
            node_b.local_card,
            kind=APPROVAL_DECISION_KIND,
            body=tampered,
            qos="control",
        )
        node_b.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=phone.node_id,
        )
        node_b.store.open_consumer_group(
            "approval-tamper",
            start="earliest",
            kind_prefix=APPROVAL_DECISION_KIND,
        )
        claim = node_b.store.claim_consumer_messages(
            "approval-tamper",
            "worker",
        )[0]
        with pytest.raises(ValueError, match="does not match request action"):
            node_b.store.activate_companion_approval(
                "approval-tamper",
                "worker",
                claim["claim_token"],
                control,
                current_ms=current + 2_000,
            )
        assert node_b.store.consumer_group_status("approval-tamper")[
            "states"
        ] == {"leased": 1}
    finally:
        node_b.close()
        phone.close()


def test_rejected_decision_is_audited_without_creating_authority(
    tmp_path: Path,
) -> None:
    node_b, phone = paired_nodes(tmp_path)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    control, _, _ = configure_grant(
        node_b.control,
        phone,
        human,
        capabilities=("approval.sign",),
        issued_ms=current - 1_000,
    )
    request = request_body(
        human_id=human.human_id,
        phone_id=phone.node_id,
        created_ms=current,
        request_id="31" * 16,
        nonce="32" * 16,
    )
    try:
        node_b.queue(
            phone.node_id,
            kind=APPROVAL_REQUEST_KIND,
            body=request,
            qos="control",
        )
        decision = approval_decision(
            request=request,
            decision="rejected",
            reason="现在不重启",
            decision_id="33" * 16,
            created_ms=current + 1_000,
            expires_ms=int(request["expires_ms"]),
        )
        packet_id = phone.queue(
            node_b.node_id,
            kind=APPROVAL_DECISION_KIND,
            body=decision,
            qos="control",
        )
        deliver(phone, node_b, packet_id)
        node_b.store.open_consumer_group(
            "approval-rejected",
            start="earliest",
            kind_prefix=APPROVAL_DECISION_KIND,
        )
        claim = node_b.store.claim_consumer_messages(
            "approval-rejected",
            "worker",
        )[0]
        outcome = node_b.store.activate_companion_approval(
            "approval-rejected",
            "worker",
            claim["claim_token"],
            control,
            current_ms=current + 2_000,
        )
        assert outcome["state"] == "rejected"
        assert outcome["max_uses"] == 0
        assert node_b.store.status()["companion_approvals"]["active"] == 0
        with pytest.raises(PermissionError, match="not active"):
            node_b.store.begin_companion_approval_effect(
                str(request["request_id"]),
                "34" * 16,
                "executor",
                control,
                current_ms=current + 3_000,
            )
    finally:
        node_b.close()
        phone.close()
