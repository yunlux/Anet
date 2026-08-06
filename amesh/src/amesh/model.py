from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ADAPTER_ACTIONS = ("surface", "reply", "amplify", "connect_candidate")
PERMISSION_EFFECTS = ("allow", "deny")

_ACTOR_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def validate_adapter_name(value: Any) -> str:
    text = str(value).strip().lower()
    if not _ADAPTER_NAME_RE.fullmatch(text):
        raise ValueError("invalid Amesh adapter name")
    return text


def validate_actor_key(value: Any, *, wildcard: bool = False) -> str:
    text = str(value).strip().lower()
    if wildcard and text == "*":
        return text
    if not _ACTOR_KEY_RE.fullmatch(text):
        raise ValueError("invalid Amesh actor key")
    return text


def validate_action(value: Any, *, wildcard: bool = False) -> str:
    text = str(value).strip().lower()
    if wildcard and text == "*":
        return text
    if text not in ADAPTER_ACTIONS:
        raise ValueError("invalid Amesh adapter action")
    return text


def validate_effect(value: Any) -> str:
    text = str(value).strip().lower()
    if text not in PERMISSION_EFFECTS:
        raise ValueError("invalid Amesh permission effect")
    return text


def validate_event_key(value: Any) -> str:
    text = str(value).strip().lower()
    if not _EVENT_KEY_RE.fullmatch(text):
        raise ValueError("invalid Amesh event key")
    return text


@dataclass(frozen=True)
class PermissionRule:
    rule_id: str
    adapter: str
    actor_key: str
    action: str
    effect: str
    reason: str
    created_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "adapter": self.adapter,
            "actor_key": self.actor_key,
            "action": self.action,
            "effect": self.effect,
            "reason": self.reason,
            "created_ms": self.created_ms,
        }
