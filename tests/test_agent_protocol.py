from __future__ import annotations

import pytest

from anet.agent_protocol import (
    missing_task_capabilities,
    normalize_capability_policy,
    task_cancel,
    task_request,
    task_result,
    task_status,
    validate_task_message,
)


def test_task_request_is_versioned_normalized_and_round_trips() -> None:
    body = task_request(
        objective="Review the patch",
        input={"ref": "abc"},
        required_capabilities=["Code.Review", "code.review"],
        context={"budget": "small"},
    )

    assert len(body["task_id"]) == 32
    assert body["required_capabilities"] == ["code.review"]
    assert validate_task_message("agent.task.request", body) == body


def test_task_status_and_results_enforce_lifecycle_shape() -> None:
    task_id = "ab" * 16
    status = task_status(
        task_id=task_id,
        state="working",
        message="running tests",
        progress=0.5,
    )
    assert validate_task_message("agent.task.status", status) == status

    result = task_result(task_id=task_id, state="completed", output={"passed": 128})
    assert validate_task_message("agent.task.result", result) == result

    with pytest.raises(ValueError, match="terminal"):
        task_status(task_id=task_id, state="completed")
    with pytest.raises(ValueError, match="requires an error"):
        task_result(task_id=task_id, state="failed")
    with pytest.raises(ValueError, match="between 0 and 1"):
        task_status(task_id=task_id, state="working", progress=1.1)


def test_task_cancel_and_untrusted_body_fail_closed() -> None:
    task_id = "cd" * 16
    body = task_cancel(task_id=task_id, reason="operator request")
    assert validate_task_message("agent.task.cancel", body) == body

    with pytest.raises(ValueError, match="protocol"):
        validate_task_message(
            "agent.task.cancel",
            {"version": 1, "task_id": task_id, "reason": "forged"},
        )
    with pytest.raises(ValueError, match="reason"):
        task_cancel(task_id=task_id, reason="")


def test_capability_policy_requires_exact_or_explicit_namespace_grants() -> None:
    assert normalize_capability_policy(
        ["Code.Review", "code.*", "Code.Review"]
    ) == ("code.*", "code.review")
    assert missing_task_capabilities(
        ["code.review", "health.read"],
        ["code.*", "health.read"],
    ) == ()
    assert missing_task_capabilities(
        ["code", "code.review", "service.restart"],
        ["code.*"],
    ) == ("code", "service.restart")
    assert missing_task_capabilities(["anything"], ["*"]) == ()
    with pytest.raises(ValueError, match="policy"):
        normalize_capability_policy(["code*"])
