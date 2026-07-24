from __future__ import annotations

import os
import re
from typing import Any


AGENT_PROTOCOL = "anet.agent.task"
AGENT_PROTOCOL_VERSION = 1

TASK_KINDS = frozenset(
    {
        "agent.task.request",
        "agent.task.status",
        "agent.task.result",
        "agent.task.cancel",
    }
)
TASK_STATES = frozenset(
    {
        "submitted",
        "working",
        "input-required",
        "auth-required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    }
)
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled", "rejected"})

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_OBJECTIVE_CHARS = 16_384
_MAX_MESSAGE_CHARS = 8_192
_MAX_CAPABILITIES = 64


def new_task_id() -> str:
    return os.urandom(16).hex()


def _task_id(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _TASK_ID_RE.fullmatch(normalized):
        raise ValueError("task_id must be 32 lowercase hexadecimal characters")
    return normalized


def _capabilities(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or ():
        capability = str(value).strip().lower()
        if not _CAPABILITY_RE.fullmatch(capability):
            raise ValueError(f"invalid task capability: {value!r}")
        if capability not in normalized:
            normalized.append(capability)
    if len(normalized) > _MAX_CAPABILITIES:
        raise ValueError("too many task capabilities")
    return sorted(normalized)


def normalize_capability_policy(
    values: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
) -> tuple[str, ...]:
    """Normalize exact capabilities and explicit ``namespace.*`` grants."""

    normalized: set[str] = set()
    for value in values or ():
        pattern = str(value).strip().lower()
        if pattern == "*":
            normalized.add(pattern)
            continue
        if pattern.endswith(".*"):
            namespace = pattern[:-2]
            if not _CAPABILITY_RE.fullmatch(namespace):
                raise ValueError(f"invalid task capability policy: {value!r}")
            normalized.add(f"{namespace}.*")
            continue
        if not _CAPABILITY_RE.fullmatch(pattern):
            raise ValueError(f"invalid task capability policy: {value!r}")
        normalized.add(pattern)
    return tuple(sorted(normalized))


def missing_task_capabilities(
    required: list[str] | tuple[str, ...] | None,
    allowed: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
) -> tuple[str, ...]:
    required_capabilities = _capabilities(required)
    policy = normalize_capability_policy(allowed)
    if "*" in policy:
        return ()

    def granted(capability: str) -> bool:
        if capability in policy:
            return True
        return any(
            pattern.endswith(".*")
            and capability.startswith(f"{pattern[:-2]}.")
            for pattern in policy
        )

    return tuple(
        capability
        for capability in required_capabilities
        if not granted(capability)
    )


def _base(task_id: str) -> dict[str, Any]:
    return {
        "protocol": AGENT_PROTOCOL,
        "version": AGENT_PROTOCOL_VERSION,
        "task_id": _task_id(task_id),
    }


def task_request(
    *,
    objective: str,
    input: Any = None,
    required_capabilities: list[str] | tuple[str, ...] | None = None,
    task_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    objective = str(objective).strip()
    if not objective:
        raise ValueError("task objective is required")
    if len(objective) > _MAX_OBJECTIVE_CHARS:
        raise ValueError("task objective is too long")
    value = _base(task_id or new_task_id())
    value.update(
        {
            "objective": objective,
            "input": input,
            "required_capabilities": _capabilities(required_capabilities),
            "context": dict(context or {}),
        }
    )
    return value


def task_status(
    *,
    task_id: str,
    state: str,
    message: str = "",
    progress: float | None = None,
) -> dict[str, Any]:
    state = str(state).strip().lower()
    if state not in TASK_STATES:
        raise ValueError("invalid task state")
    if state in TERMINAL_TASK_STATES:
        raise ValueError("terminal task states must use agent.task.result")
    message = str(message).strip()
    if len(message) > _MAX_MESSAGE_CHARS:
        raise ValueError("task status message is too long")
    if progress is not None:
        progress = float(progress)
        if not 0.0 <= progress <= 1.0:
            raise ValueError("task progress must be between 0 and 1")
    value = _base(task_id)
    value.update({"state": state, "message": message, "progress": progress})
    return value


def task_result(
    *,
    task_id: str,
    state: str,
    output: Any = None,
    error: str = "",
) -> dict[str, Any]:
    state = str(state).strip().lower()
    if state not in TERMINAL_TASK_STATES:
        raise ValueError("task result state must be terminal")
    error = str(error).strip()
    if len(error) > _MAX_MESSAGE_CHARS:
        raise ValueError("task result error is too long")
    if state == "completed" and error:
        raise ValueError("a completed task cannot contain an error")
    if state != "completed" and not error:
        raise ValueError("an unsuccessful task result requires an error")
    value = _base(task_id)
    value.update({"state": state, "output": output, "error": error})
    return value


def task_cancel(*, task_id: str, reason: str) -> dict[str, Any]:
    reason = str(reason).strip()
    if not reason:
        raise ValueError("task cancellation reason is required")
    if len(reason) > _MAX_MESSAGE_CHARS:
        raise ValueError("task cancellation reason is too long")
    value = _base(task_id)
    value["reason"] = reason
    return value


def validate_task_message(kind: str, body: Any) -> dict[str, Any]:
    kind = str(kind).strip().lower()
    if kind not in TASK_KINDS:
        raise ValueError("unsupported agent task kind")
    if not isinstance(body, dict):
        raise ValueError("agent task body must be a map")
    if body.get("protocol") != AGENT_PROTOCOL:
        raise ValueError("unsupported agent task protocol")
    if int(body.get("version", 0)) != AGENT_PROTOCOL_VERSION:
        raise ValueError("unsupported agent task protocol version")

    if kind == "agent.task.request":
        return task_request(
            task_id=str(body.get("task_id", "")),
            objective=str(body.get("objective", "")),
            input=body.get("input"),
            required_capabilities=body.get("required_capabilities"),
            context=body.get("context"),
        )
    if kind == "agent.task.status":
        return task_status(
            task_id=str(body.get("task_id", "")),
            state=str(body.get("state", "")),
            message=str(body.get("message", "")),
            progress=body.get("progress"),
        )
    if kind == "agent.task.result":
        return task_result(
            task_id=str(body.get("task_id", "")),
            state=str(body.get("state", "")),
            output=body.get("output"),
            error=str(body.get("error", "")),
        )
    return task_cancel(
        task_id=str(body.get("task_id", "")),
        reason=str(body.get("reason", "")),
    )
