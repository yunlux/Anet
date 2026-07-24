from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import time

import pytest

from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.packet import seal_packet
from anet.peers import PeerBook
from anet.social import (
    DISCORD_SIGNAL_KIND,
    SocialPolicy,
    build_discord_signal,
    normalize_social_label,
    validate_discord_signal,
)


def _stats(**overrides: int) -> dict[str, int]:
    value = {
        "account_age_days": 0,
        "mention_count": 0,
        "reply_count": 0,
        "reaction_count": 0,
        "pinned_count": 0,
    }
    value.update(overrides)
    return value


def _evaluation() -> dict:
    return SocialPolicy().evaluate(
        _stats(
            account_age_days=2000,
            mention_count=8,
            reply_count=5,
            reaction_count=20,
            pinned_count=2,
        ),
        {"relationship:vouched"},
        {"interaction:mention"},
    )


def _signal() -> dict:
    return build_discord_signal(
        source_event_id="01" * 16,
        actor_key="02" * 32,
        created_ms=1_700_000_000_000,
        expires_ms=1_700_003_600_000,
        content_level="mention",
        content="@anet hello",
        labels={"platform:discord", "interaction:mention"},
        evaluation=_evaluation(),
        guild_key="03" * 32,
        channel_key="04" * 32,
        message_revision="create",
    )


def test_unknown_actor_is_surface_only() -> None:
    result = SocialPolicy().evaluate(
        _stats(),
        set(),
        {"interaction:mention"},
    )
    assert result["action"] == "surface"
    assert result["allowed_actions"] == ["observe", "surface"]
    assert result["reputation"] == {
        "score": 50,
        "raw_score": 50,
        "confidence": 0,
        "algorithm": "anet-social-evidence-v1",
        "reasons": ["no reputation evidence"],
    }


def test_vouch_and_bounded_interactions_raise_actions_explainably() -> None:
    result = _evaluation()
    assert result["action"] == "connect_candidate"
    assert result["reputation"]["score"] == 100
    assert result["reputation"]["confidence"] == 100
    assert result["allowed_actions"] == [
        "observe",
        "surface",
        "reply",
        "amplify",
        "connect_candidate",
    ]
    assert "cannot create Anet trust" in result["reasons"][-1]


@pytest.mark.parametrize(
    "label",
    ["risk:block", "risk:spam", "risk:impersonation", "risk:malware"],
)
def test_risk_labels_fail_closed(label: str) -> None:
    result = SocialPolicy().evaluate(
        _stats(
            account_age_days=4000,
            mention_count=100,
            reply_count=100,
            reaction_count=100,
            pinned_count=100,
        ),
        {"relationship:vouched", label},
        {"interaction:mention"},
    )
    assert result["allowed_actions"] == ["observe"]
    assert result["action"] == "observe"


def test_reserved_and_malformed_manual_labels_are_rejected() -> None:
    assert normalize_social_label("Interest:Agents", manual=True) == (
        "interest:agents"
    )
    with pytest.raises(ValueError, match="reserved prefix"):
        normalize_social_label("actor:human", manual=True)
    with pytest.raises(ValueError, match="invalid social label"):
        normalize_social_label("interest:agent social", manual=True)


def test_discord_signal_is_exact_and_carries_no_platform_ids() -> None:
    signal = _signal()
    assert validate_discord_signal(signal) == signal
    assert signal["decision"]["action"] == "connect_candidate"
    assert set(signal["provenance"]) == {
        "platform",
        "adapter",
        "guild_key",
        "channel_key",
        "message_revision",
    }
    assert "guild_id" not in repr(signal)
    assert "channel_id" not in repr(signal)
    assert "discord_user_id" not in repr(signal)


def test_discord_signal_rejects_unknown_fields_and_privacy_downgrade() -> None:
    signal = _signal()
    malformed = {**signal, "capability": "admin.*"}
    with pytest.raises(ValueError, match="unknown fields"):
        validate_discord_signal(malformed)

    metadata = deepcopy(signal)
    metadata["content_level"] = "metadata"
    with pytest.raises(ValueError, match="contains content"):
        validate_discord_signal(metadata)

    wrong_type = deepcopy(signal)
    wrong_type["reputation"]["confidence"] = True
    with pytest.raises(ValueError, match="must be an integer"):
        validate_discord_signal(wrong_type)


def test_discord_signal_rejects_decision_action_escalation() -> None:
    signal = _signal()
    signal["decision"]["action"] = "connect_candidate"
    signal["decision"]["allowed_actions"] = ["observe", "surface"]
    with pytest.raises(ValueError, match="allowed action sequence"):
        validate_discord_signal(signal)


def test_node_send_and_receive_enforce_social_signal_schema(tmp_path) -> None:
    sender_base = initialize_node(
        tmp_path / "sender",
        label="sender",
        listen_port=0,
    )
    receiver_base = initialize_node(
        tmp_path / "receiver",
        label="receiver",
        listen_port=0,
    )
    sender_config = replace(
        sender_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    receiver_config = replace(
        receiver_base,
        prekey_policy="disable",
        prekey_auto_enabled=False,
    )
    sender_identity = Identity.load(sender_config.identity_path)
    receiver_identity = Identity.load(receiver_config.identity_path)
    sender_card = sender_identity.card(addresses=())
    receiver_card = receiver_identity.card(addresses=())
    PeerBook(
        sender_config.peers_path,
        own_node_id=sender_identity.node_id,
    ).add(receiver_card)
    PeerBook(
        receiver_config.peers_path,
        own_node_id=receiver_identity.node_id,
    ).add(sender_card)
    sender = AnetNode(sender_config)
    receiver = AnetNode(receiver_config)
    current = int(time.time() * 1000)
    signal = build_discord_signal(
        source_event_id="01" * 16,
        actor_key="02" * 32,
        created_ms=current,
        expires_ms=current + 3_600_000,
        content_level="mention",
        content="@anet hello",
        labels={"platform:discord", "interaction:mention"},
        evaluation=_evaluation(),
        guild_key="03" * 32,
        channel_key="04" * 32,
        message_revision="create",
    )
    try:
        packet_id = sender.queue(
            receiver.node_id,
            kind=DISCORD_SIGNAL_KIND,
            body=signal,
        )
        raw = sender.store.get_packet(packet_id)
        assert raw is not None
        assert receiver.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=sender.node_id,
        ) == packet_id
        received = next(
            item
            for item in receiver.store.list_inbox()
            if item["packet_id"] == packet_id
        )
        assert received["body"] == signal
        assert received["trusted"] is True

        malformed = {**signal, "capability": "admin.*"}
        with pytest.raises(ValueError, match="unknown fields"):
            sender.queue(
                receiver.node_id,
                kind=DISCORD_SIGNAL_KIND,
                body=malformed,
            )
        malicious_raw = seal_packet(
            sender_identity,
            receiver_card,
            kind=DISCORD_SIGNAL_KIND,
            body=malformed,
            ttl_seconds=3600,
            max_hops=8,
            padding_min=0,
        )
        with pytest.raises(ValueError, match="unknown fields"):
            receiver.accept_carrier_packet(
                malicious_raw,
                depth=1,
                peer_id=sender.node_id,
            )
    finally:
        sender.close()
        receiver.close()
