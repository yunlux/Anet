from __future__ import annotations

import pytest

from amesh.policy import PermissionStore, amesh_database_path

ACTOR = "a" * 64
OTHER = "b" * 64


def test_add_and_list_rules(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        rule = store.add_rule("discord", ACTOR, "surface", "deny", reason="spam")
        assert rule.adapter == "discord"
        assert rule.actor_key == ACTOR
        assert rule.effect == "deny"
        assert len(rule.rule_id) == 32
        assert store.rules(adapter="discord") == [rule]
        assert store.rules(adapter="discord", actor_key=ACTOR) == [rule]
        assert store.rules(adapter="discord", actor_key=OTHER) == []
    finally:
        store.close()


def test_remove_rule(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        rule = store.add_rule("discord", "*", "reply", "deny")
        assert store.remove_rule(rule.rule_id) is True
        assert store.remove_rule(rule.rule_id) is False
        with pytest.raises(ValueError):
            store.require_rule(rule.rule_id)
    finally:
        store.close()


def test_effective_precedence_wildcard_then_exact(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.add_rule("discord", "*", "*", "deny", reason="default deny")
        assert store.effective("discord", ACTOR, "reply").effect == "deny"
        allowed = store.add_rule(
            "discord", ACTOR, "reply", "allow", reason="vouched friend"
        )
        assert store.effective("discord", ACTOR, "reply") == allowed
        assert store.effective("discord", OTHER, "reply").effect == "deny"
    finally:
        store.close()


def test_deny_wins_on_equal_specificity(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.add_rule("discord", ACTOR, "reply", "allow")
        store.add_rule("discord", ACTOR, "reply", "deny")
        assert store.effective("discord", ACTOR, "reply").effect == "deny"
    finally:
        store.close()


def test_apply_allowed_filters(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.add_rule("discord", "*", "amplify", "deny")
        store.add_rule("discord", ACTOR, "reply", "allow")
        allowed, reasons = store.apply_allowed(
            "discord",
            ACTOR,
            ["observe", "surface", "reply", "amplify", "connect_candidate"],
        )
        assert allowed == ["observe", "surface", "reply", "connect_candidate"]
        assert any("amplify denied" in reason for reason in reasons)
        allowed_other, _ = store.apply_allowed(
            "discord",
            OTHER,
            ["observe", "surface", "reply"],
        )
        assert allowed_other == ["observe", "surface", "reply"]
    finally:
        store.close()


def test_apply_allowed_keeps_observe_always(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.add_rule("discord", "*", "*", "deny")
        allowed, reasons = store.apply_allowed("discord", ACTOR, ["observe", "surface"])
        assert allowed == ["observe"]
        assert any("surface denied" in reason for reason in reasons)
    finally:
        store.close()


def test_decision_audit(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.record_decision(
            "discord",
            ACTOR,
            "surface",
            "deny",
            event_key="c" * 32,
        )
        decisions = store.decisions(adapter="discord")
        assert len(decisions) == 1
        assert decisions[0]["action"] == "surface"
        assert decisions[0]["effect"] == "deny"
    finally:
        store.close()


def test_invalid_inputs(tmp_path) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        with pytest.raises(ValueError):
            store.add_rule("Discord!", ACTOR, "surface", "deny")
        with pytest.raises(ValueError):
            store.add_rule("discord", "not-hex", "surface", "deny")
        with pytest.raises(ValueError):
            store.add_rule("discord", ACTOR, "observe", "deny")
        with pytest.raises(ValueError):
            store.add_rule("discord", ACTOR, "surface", "grant")
        with pytest.raises(ValueError):
            store.decisions(limit=0)
    finally:
        store.close()
