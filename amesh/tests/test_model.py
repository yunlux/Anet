from __future__ import annotations

import pytest

from amesh.model import (
    PermissionRule,
    validate_action,
    validate_actor_key,
    validate_adapter_name,
    validate_effect,
    validate_event_key,
)


def test_adapter_name_normalization() -> None:
    assert validate_adapter_name("Discord") == "discord"
    with pytest.raises(ValueError):
        validate_adapter_name("not valid")


def test_actor_key_validation() -> None:
    key = "a" * 64
    assert validate_actor_key(key) == key
    assert validate_actor_key("*", wildcard=True) == "*"
    assert validate_actor_key("A" * 64) == key
    with pytest.raises(ValueError):
        validate_actor_key("z" * 64)
    with pytest.raises(ValueError):
        validate_actor_key("*")


def test_action_and_effect_validation() -> None:
    assert validate_action("SURFACE") == "surface"
    assert validate_action("*", wildcard=True) == "*"
    with pytest.raises(ValueError):
        validate_action("observe")
    with pytest.raises(ValueError):
        validate_effect("grant")
    assert validate_effect("DENY") == "deny"


def test_event_key_validation() -> None:
    event = "b" * 32
    assert validate_event_key(event) == event
    with pytest.raises(ValueError):
        validate_event_key("c" * 31)


def test_permission_rule_to_dict() -> None:
    rule = PermissionRule(
        rule_id="r" * 32,
        adapter="discord",
        actor_key="*",
        action="reply",
        effect="deny",
        reason="test",
        created_ms=1,
    )
    value = rule.to_dict()
    assert value["effect"] == "deny"
    assert value["action"] == "reply"
