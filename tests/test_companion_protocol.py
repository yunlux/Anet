from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

import pytest

from anet.companion_protocol import (
    APPROVAL_DECISION_KIND,
    APPROVAL_REQUEST_KIND,
    EPISODE_KIND,
    INTERVENTION_KIND,
    OBSERVATION_BATCH_KIND,
    USER_RESPONSE_KIND,
    approval_decision,
    approval_request,
    consent_evidence,
    episode,
    intervention,
    observation_batch,
    user_response,
    validate_approval_decision_binding,
    validate_companion_endpoint_binding,
    validate_companion_message,
)
from anet.control_plane import HumanPrincipalIdentity
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import inspect_packet, seal_packet
from anet.peers import PeerBook


CREATED_MS = 1_700_000_000_000
WINDOW_START_MS = CREATED_MS - 60_000
WINDOW_END_MS = CREATED_MS - 1_000
EXPIRES_MS = CREATED_MS + 3_600_000

BATCH_ID = "01" * 16
OBSERVATION_ID = "02" * 16
EPISODE_ID = "03" * 16
INTERVENTION_ID = "04" * 16
RESPONSE_ID = "05" * 16
REQUEST_ID = "06" * 16
DECISION_ID = "07" * 16
CONSENT_ID = "08" * 16
NONCE = "09" * 16
PARAMETERS_DIGEST = "ab" * 32


@pytest.fixture
def identities() -> tuple[Identity, HumanPrincipalIdentity]:
    return Identity.generate("phone"), HumanPrincipalIdentity.generate()


def test_operational_observation_batch_is_strict_and_round_trips(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, _ = identities
    body = observation_batch(
        source_node_id=phone.node_id,
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        data_level="operational",
        consent=consent_evidence(
            basis="device-essential",
            scope=["device.network", "device.battery"],
        ),
        observations=[
            {
                "observation_id": OBSERVATION_ID,
                "observed_ms": WINDOW_START_MS,
                "type": "device.battery",
                "value": {"percent": 74, "charging": False},
            },
            {
                "observation_id": "0a" * 16,
                "observed_ms": WINDOW_END_MS,
                "type": "device.network",
                "value": {"transport": "wifi", "metered": False},
            },
        ],
        batch_id=BATCH_ID,
        created_ms=CREATED_MS,
        expires_ms=EXPIRES_MS,
    )

    assert validate_companion_message(OBSERVATION_BATCH_KIND, body) == body
    assert body["consent"]["scope"] == ["device.battery", "device.network"]

    unknown = {**body, "raw_events": []}
    with pytest.raises(ValueError, match="unknown fields"):
        validate_companion_message(OBSERVATION_BATCH_KIND, unknown)


def test_sensitive_observations_require_opt_in_and_raw_payloads_fail_closed(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, _ = identities
    with pytest.raises(ValueError, match="user-opt-in"):
        observation_batch(
            source_node_id=phone.node_id,
            window_start_ms=WINDOW_START_MS,
            window_end_ms=WINDOW_END_MS,
            data_level="personal-low",
            consent=consent_evidence(
                basis="device-essential",
                scope=["human.presence"],
            ),
            observations=[
                {
                    "observation_id": OBSERVATION_ID,
                    "observed_ms": WINDOW_END_MS,
                    "type": "human.presence",
                    "value": {"state": "present"},
                }
            ],
            created_ms=CREATED_MS,
            expires_ms=EXPIRES_MS,
        )

    with pytest.raises(ValueError, match="unknown fields"):
        observation_batch(
            source_node_id=phone.node_id,
            window_start_ms=WINDOW_START_MS,
            window_end_ms=WINDOW_END_MS,
            data_level="personal-low",
            consent=consent_evidence(
                basis="user-opt-in",
                grant_id=CONSENT_ID,
                scope=["device.app-category-window"],
            ),
            observations=[
                {
                    "observation_id": OBSERVATION_ID,
                    "observed_ms": WINDOW_END_MS,
                    "type": "device.app-category-window",
                    "value": {
                        "category_durations_ms": {"communication": 30_000},
                        "switch_count": 4,
                        "raw_events": [],
                    },
                }
            ],
            created_ms=CREATED_MS,
            expires_ms=EXPIRES_MS,
        )

    with pytest.raises(ValueError, match="binary sensor payloads"):
        validate_companion_message(
            EPISODE_KIND,
            {
                "protocol": "anet.companion",
                "version": 1,
                "object_type": "episode",
                "episode_id": EPISODE_ID,
                "created_ms": CREATED_MS,
                "expires_ms": EXPIRES_MS,
                "source_node_id": phone.node_id,
                "source_batch_ids": [BATCH_ID],
                "episode_type": "device.battery-window",
                "window_start_ms": WINDOW_START_MS,
                "window_end_ms": WINDOW_END_MS,
                "data_level": "operational",
                "consent": consent_evidence(
                    basis="device-essential",
                    scope=["device.battery"],
                ),
                "transform_version": "battery.v1",
                "metrics": {"sample": b"raw"},
            },
        )


def test_self_report_and_episode_preserve_provenance_without_human_state(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, _ = identities
    consent = consent_evidence(
        basis="user-initiated",
        scope=["human.self-report"],
    )
    batch = observation_batch(
        source_node_id=phone.node_id,
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        data_level="personal-low",
        consent=consent,
        observations=[
            {
                "observation_id": OBSERVATION_ID,
                "observed_ms": WINDOW_END_MS,
                "type": "human.self-report",
                "value": {"text": "需要稍后提醒我休息", "format": "plain"},
            }
        ],
        batch_id=BATCH_ID,
        created_ms=CREATED_MS,
        expires_ms=EXPIRES_MS,
    )
    value = episode(
        source_node_id=phone.node_id,
        source_batch_ids=[batch["batch_id"]],
        episode_type="human.self-report",
        window_start_ms=WINDOW_START_MS,
        window_end_ms=WINDOW_END_MS,
        data_level="personal-low",
        consent=consent,
        transform_version="self-report.v1",
        metrics={"report_count": 1, "source": "explicit"},
        episode_id=EPISODE_ID,
        created_ms=CREATED_MS,
        expires_ms=EXPIRES_MS,
    )

    assert validate_companion_message(EPISODE_KIND, value) == value
    assert value["source_batch_ids"] == [BATCH_ID]

    forbidden = deepcopy(value)
    forbidden["metrics"]["human_state"] = "fatigued"
    with pytest.raises(ValueError, match="forbidden Companion field"):
        validate_companion_message(EPISODE_KIND, forbidden)
    wrong_consent = deepcopy(value)
    wrong_consent["consent"] = consent_evidence(
        basis="device-essential",
        scope=["human.self-report"],
    )
    with pytest.raises(ValueError, match="user-initiated"):
        validate_companion_message(EPISODE_KIND, wrong_consent)
    non_finite = deepcopy(value)
    non_finite["metrics"]["ratio"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        validate_companion_message(EPISODE_KIND, non_finite)
    with pytest.raises(ValueError, match="unsupported P0 episode type"):
        episode(
            **{
                **{
                    key: value[key]
                    for key in (
                        "source_node_id",
                        "source_batch_ids",
                        "window_start_ms",
                        "window_end_ms",
                        "data_level",
                        "consent",
                        "transform_version",
                        "metrics",
                        "created_ms",
                        "expires_ms",
                    )
                },
                "episode_type": "human.fatigue",
            }
        )


def test_intervention_and_user_response_form_an_idempotent_loop(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, human = identities
    prompt = intervention(
        human_id=human.human_id,
        target_device_id=phone.node_id,
        category="choice",
        priority="normal",
        title="稍后休息？",
        message="你希望多久后再提醒？",
        response_options=[
            {"action_id": "snooze.15m", "label": "15 分钟"},
            {"action_id": "decline", "label": "不再提醒"},
        ],
        related_episode_ids=[EPISODE_ID],
        intervention_id=INTERVENTION_ID,
        dedupe_key=INTERVENTION_ID,
        created_ms=CREATED_MS,
        expires_ms=EXPIRES_MS,
    )
    response = user_response(
        intervention_id=prompt["intervention_id"],
        human_id=human.human_id,
        device_node_id=phone.node_id,
        disposition="answered",
        action_id="snooze.15m",
        response_id=RESPONSE_ID,
        created_ms=CREATED_MS + 1_000,
        expires_ms=EXPIRES_MS,
    )

    assert validate_companion_message(INTERVENTION_KIND, prompt) == prompt
    assert validate_companion_message(USER_RESPONSE_KIND, response) == response
    assert response["intervention_id"] == prompt["intervention_id"]
    assert prompt["dedupe_key"] == INTERVENTION_ID

    replay_shape = {**response, "response_id": RESPONSE_ID}
    assert validate_companion_message(USER_RESPONSE_KIND, replay_shape) == response


def test_approval_decision_is_bound_to_exact_request_and_short_scope(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, human = identities
    request = approval_request(
        human_id=human.human_id,
        device_node_id=phone.node_id,
        action={
            "capability": "service.restart",
            "resource": "anet://node/ahub/service/relay",
            "parameters_digest": PARAMETERS_DIGEST,
            "summary": "重启 Relay 服务",
        },
        scope={
            "mode": "once",
            "max_uses": 1,
            "grant_expires_ms": CREATED_MS + 10 * 60 * 1000,
        },
        risk="会短暂中断实时 Relay，会话将重新建立。",
        request_id=REQUEST_ID,
        nonce=NONCE,
        created_ms=CREATED_MS,
        expires_ms=CREATED_MS + 5 * 60 * 1000,
    )
    decision = approval_decision(
        request=request,
        decision="approved",
        decision_id=DECISION_ID,
        created_ms=CREATED_MS + 1_000,
        expires_ms=CREATED_MS + 5 * 60 * 1000,
    )

    assert validate_companion_message(APPROVAL_REQUEST_KIND, request) == request
    assert validate_companion_message(APPROVAL_DECISION_KIND, decision) == decision
    assert validate_approval_decision_binding(request, decision) == decision

    tampered = deepcopy(decision)
    tampered["action"]["parameters_digest"] = "cd" * 32
    with pytest.raises(ValueError, match="does not match request action"):
        validate_approval_decision_binding(request, tampered)

    replayed = {**decision, "nonce": "ef" * 16}
    with pytest.raises(ValueError, match="does not match request nonce"):
        validate_approval_decision_binding(request, replayed)

    with pytest.raises(ValueError, match="once approval"):
        approval_request(
            human_id=human.human_id,
            device_node_id=phone.node_id,
            action=request["action"],
            scope={
                "mode": "once",
                "max_uses": 2,
                "grant_expires_ms": CREATED_MS + 60_000,
            },
            risk="test",
            created_ms=CREATED_MS,
            expires_ms=CREATED_MS + 60_000,
        )


def test_kind_and_object_type_cannot_be_confused(
    identities: tuple[Identity, HumanPrincipalIdentity],
) -> None:
    phone, human = identities
    prompt = intervention(
        human_id=human.human_id,
        target_device_id=phone.node_id,
        category="notification",
        priority="high",
        title="连接已恢复",
        message="手机与主电脑已重新连接。",
        intervention_id=INTERVENTION_ID,
        created_ms=CREATED_MS,
        expires_ms=EXPIRES_MS,
    )
    with pytest.raises(ValueError, match="kind/object_type"):
        validate_companion_message(
            USER_RESPONSE_KIND,
            {
                **prompt,
                "response_id": RESPONSE_ID,
                "intervention_id": INTERVENTION_ID,
                "device_node_id": phone.node_id,
                "disposition": "presented",
                "action_id": "",
                "text": "",
            },
        )
    with pytest.raises(ValueError, match="unsupported Companion kind"):
        validate_companion_message("companion.human-state", prompt)
    other_phone = Identity.generate("other-phone")
    with pytest.raises(ValueError, match="Packet destination"):
        validate_companion_endpoint_binding(
            INTERVENTION_KIND,
            prompt,
            sender_node_id=other_phone.node_id,
            destination_node_id=other_phone.node_id,
            now_ms=CREATED_MS,
        )


def test_node_queue_and_receive_enforce_companion_validation(tmp_path) -> None:
    sender_base = initialize_node(
        tmp_path / "sender",
        label="sender",
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
    sender_card = sender_identity.card(
        addresses=(),
        capabilities=sender_config.capabilities,
    )
    phone_card = phone_identity.card(
        addresses=(),
        capabilities=phone_config.capabilities,
    )
    PeerBook(
        sender_config.peers_path,
        own_node_id=sender_identity.node_id,
    ).add(phone_card)
    PeerBook(
        phone_config.peers_path,
        own_node_id=phone_identity.node_id,
    ).add(sender_card)
    sender = AnetNode(sender_config)
    phone = AnetNode(phone_config)
    human = HumanPrincipalIdentity.generate()
    current = int(time.time() * 1000)
    prompt = intervention(
        human_id=human.human_id,
        target_device_id=phone.node_id,
        category="notification",
        priority="high",
        title="连接恢复",
        message="主电脑已经重新连接。",
        intervention_id=INTERVENTION_ID,
        created_ms=current,
        expires_ms=current + 3_600_000,
    )
    try:
        packet_id = sender.queue(
            phone.node_id,
            kind=INTERVENTION_KIND,
            body=prompt,
            qos="interactive",
        )
        raw = sender.store.get_packet(packet_id)
        assert raw is not None
        assert phone.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=sender.node_id,
        ) == packet_id
        received = next(
            item for item in phone.store.list_inbox()
            if item["packet_id"] == packet_id
        )
        assert received["body"] == prompt
        assert received["trusted"] is True

        malformed = {**prompt, "human_state": "fatigued"}
        with pytest.raises(ValueError, match="unknown fields"):
            sender.queue(
                phone.node_id,
                kind=INTERVENTION_KIND,
                body=malformed,
            )
        wrong_target = {**prompt, "target_device_id": sender.node_id}
        with pytest.raises(ValueError, match="Packet destination"):
            sender.queue(
                phone.node_id,
                kind=INTERVENTION_KIND,
                body=wrong_target,
            )

        bypass = seal_packet(
            sender_identity,
            phone_card,
            kind=INTERVENTION_KIND,
            body=malformed,
        )
        bypass_id = inspect_packet(bypass).packet_id
        with pytest.raises(ValueError, match="unknown fields"):
            phone.accept_carrier_packet(
                bypass,
                depth=1,
                peer_id=sender.node_id,
            )
        rejection = phone.store.packet_rejection(bypass_id)
        assert rejection is not None
        assert "unknown fields" in rejection["reason"]
    finally:
        sender.close()
        phone.close()


def test_language_neutral_json_fixtures_match_python_validator() -> None:
    root = (
        Path(__file__).parents[1]
        / "docs"
        / "examples"
        / "companion-v1"
    )
    fixtures = {
        "observation-batch.json": OBSERVATION_BATCH_KIND,
        "episode.json": EPISODE_KIND,
        "intervention.json": INTERVENTION_KIND,
        "user-response.json": USER_RESPONSE_KIND,
        "approval-request.json": APPROVAL_REQUEST_KIND,
        "approval-decision.json": APPROVAL_DECISION_KIND,
    }
    loaded: dict[str, dict[str, object]] = {}
    for filename, kind in fixtures.items():
        body = json.loads((root / filename).read_text(encoding="utf-8"))
        assert validate_companion_message(kind, body) == body
        loaded[filename] = body
    assert validate_approval_decision_binding(
        loaded["approval-request.json"],
        loaded["approval-decision.json"],
    ) == loaded["approval-decision.json"]


def test_companion_fixture_canonical_hashes_match_kotlin_contract() -> None:
    root = Path(__file__).parents[1] / "docs" / "examples" / "companion-v1"
    expected = json.loads(
        (root / "canonical-sha256.json").read_text(encoding="utf-8")
    )
    fixtures = {
        "observation-batch.json": OBSERVATION_BATCH_KIND,
        "episode.json": EPISODE_KIND,
        "intervention.json": INTERVENTION_KIND,
        "user-response.json": USER_RESPONSE_KIND,
        "approval-request.json": APPROVAL_REQUEST_KIND,
        "approval-decision.json": APPROVAL_DECISION_KIND,
    }
    actual = {}
    for filename, kind in fixtures.items():
        body = json.loads((root / filename).read_text(encoding="utf-8"))
        normalized = validate_companion_message(kind, body)
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        actual[filename] = hashlib.sha256(canonical).hexdigest()
    assert actual == expected
