from __future__ import annotations

import pytest

from anet.a2a_v1 import (
    A2A_AGENT_CARD_PATH,
    A2A_PROTOCOL_VERSION,
    A2A_STATE_BY_ANET,
    ANET_STATE_BY_A2A,
    A2ASkillBinding,
    bearer_security,
    build_agent_card,
    inbound_message_to_task,
    initial_a2a_task,
    normalize_cancel_task_request,
    task_event_to_a2a_stream_events,
)
from anet.agent_protocol import task_result, task_status


SKILL = A2ASkillBinding(
    id="code.review",
    name="Code review",
    description="Review a source change and return a structured verdict.",
    tags=("code", "review"),
    examples=("Review this patch",),
    required_capabilities=("code.review",),
)


def send_request(
    *,
    message_id: str = "message-1",
    text: str = "Review this patch",
    context_id: str = "",
    tenant: str = "",
    task_id: str = "",
) -> dict:
    message = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
        "metadata": {"untrustedCapability": "admin.*"},
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    value = {
        "message": message,
        "configuration": {"returnImmediately": True},
        "metadata": {"trace": "external"},
    }
    if tenant:
        value["tenant"] = tenant
    return value


def test_agent_card_is_v1_explicit_and_does_not_project_peer_identity() -> None:
    schemes, requirements = bearer_security()
    card = build_agent_card(
        name="Anet edge",
        description="Authenticated A2A edge backed by Anet.",
        endpoint="https://agent.example/a2a",
        agent_version="0.11.0",
        skills=[SKILL],
        security_schemes=schemes,
        security_requirements=requirements,
        streaming=True,
    )

    assert A2A_AGENT_CARD_PATH == "/.well-known/agent-card.json"
    assert card["supportedInterfaces"] == [
        {
            "url": "https://agent.example/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": A2A_PROTOCOL_VERSION,
        }
    ]
    assert card["skills"][0]["id"] == "code.review"
    assert card["securityRequirements"] == requirements
    assert "url" not in card
    assert "protocolVersion" not in card
    assert "node_id" not in card
    assert "addresses" not in card
    assert "required_capabilities" not in card["skills"][0]


def test_agent_card_fails_closed_on_auth_and_cleartext_remote_endpoint() -> None:
    with pytest.raises(ValueError, match="explicit security"):
        build_agent_card(
            name="unsafe",
            description="missing authentication",
            endpoint="https://agent.example/a2a",
            agent_version="1",
            skills=[SKILL],
        )
    with pytest.raises(ValueError, match="HTTPS"):
        build_agent_card(
            name="unsafe",
            description="cleartext remote endpoint",
            endpoint="http://agent.example/a2a",
            agent_version="1",
            skills=[SKILL],
            allow_unauthenticated=True,
        )

    loopback = build_agent_card(
        name="local",
        description="local development endpoint",
        endpoint="http://127.0.0.1:8080/a2a",
        agent_version="1",
        skills=[SKILL],
        allow_unauthenticated=True,
    )
    assert loopback["supportedInterfaces"][0]["url"].startswith("http://127.0.0.1")


def test_inbound_message_uses_sender_scoped_idempotency_and_local_capabilities() -> None:
    first = inbound_message_to_task(
        send_request(),
        authenticated_sender_id="an1sender-a",
        skill=SKILL,
    )
    retry = inbound_message_to_task(
        send_request(),
        authenticated_sender_id="an1sender-a",
        skill=SKILL,
    )
    other_sender = inbound_message_to_task(
        send_request(),
        authenticated_sender_id="an1sender-b",
        skill=SKILL,
    )

    assert first.task_id == retry.task_id
    assert first.a2a_task_id == first.task_id
    assert first.body == retry.body
    assert first.task_id != other_sender.task_id
    assert first.body["objective"] == "Review this patch"
    assert first.body["required_capabilities"] == ["code.review"]
    assert first.body["context"]["a2a"]["messageId"] == "message-1"
    assert first.body["input"]["a2aMessage"]["metadata"] == {
        "untrustedCapability": "admin.*"
    }
    assert "admin.*" not in first.body["required_capabilities"]


def test_inbound_message_preserves_context_and_enforces_tenant() -> None:
    value = inbound_message_to_task(
        send_request(context_id="context-from-client", tenant="tenant-a"),
        authenticated_sender_id="an1sender",
        skill=SKILL,
        expected_tenant="tenant-a",
    )
    assert value.context_id == "context-from-client"
    assert initial_a2a_task(value) == {
        "id": value.task_id,
        "contextId": "context-from-client",
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "history": [value.message],
        "metadata": {"anet": {"skillId": "code.review"}},
    }

    with pytest.raises(PermissionError, match="tenant"):
        inbound_message_to_task(
            send_request(tenant="tenant-b"),
            authenticated_sender_id="an1sender",
            skill=SKILL,
            expected_tenant="tenant-a",
        )


def test_follow_up_requires_and_preserves_persisted_task_mapping() -> None:
    first = inbound_message_to_task(
        send_request(context_id="context-1"),
        authenticated_sender_id="an1sender",
        skill=SKILL,
    )
    persisted = {
        "a2a_task_id": first.a2a_task_id,
        "context_id": first.context_id,
        "sender_node_id": "an1sender",
        "skill_id": first.skill_id,
        "tenant": "",
        "protocol_version": A2A_PROTOCOL_VERSION,
    }
    follow_up = inbound_message_to_task(
        send_request(
            message_id="message-2",
            text="The target branch is main",
            task_id=first.a2a_task_id,
        ),
        authenticated_sender_id="an1sender",
        skill=SKILL,
        existing_task=persisted,
    )

    assert follow_up.a2a_task_id == first.a2a_task_id
    assert follow_up.task_id != first.task_id
    assert follow_up.context_id == "context-1"
    assert follow_up.message["taskId"] == first.a2a_task_id
    assert follow_up.message["contextId"] == "context-1"
    assert follow_up.body["context"]["a2a"]["taskId"] == first.a2a_task_id

    with pytest.raises(ValueError, match="contextId"):
        inbound_message_to_task(
            send_request(
                message_id="message-3",
                context_id="wrong-context",
                task_id=first.a2a_task_id,
            ),
            authenticated_sender_id="an1sender",
            skill=SKILL,
            existing_task=persisted,
        )


def test_cancel_task_request_is_principal_tenant_and_task_scoped() -> None:
    persisted = {
        "a2a_task_id": "external-task",
        "sender_node_id": "an1sender",
        "tenant": "tenant-a",
        "state": "working",
        "cancel_state": "",
        "protocol_version": A2A_PROTOCOL_VERSION,
    }
    assert normalize_cancel_task_request(
        {
            "tenant": "tenant-a",
            "id": "external-task",
            "metadata": {"reason": "untrusted"},
        },
        authenticated_sender_id="an1sender",
        existing_task=persisted,
        expected_tenant="tenant-a",
    ) == {
        "tenant": "tenant-a",
        "id": "external-task",
        "metadata": {"reason": "untrusted"},
    }
    with pytest.raises(PermissionError, match="sender"):
        normalize_cancel_task_request(
            {"tenant": "tenant-a", "id": "external-task"},
            authenticated_sender_id="an1other",
            existing_task=persisted,
            expected_tenant="tenant-a",
        )
    with pytest.raises(PermissionError, match="tenant"):
        normalize_cancel_task_request(
            {"tenant": "tenant-b", "id": "external-task"},
            authenticated_sender_id="an1sender",
            existing_task=persisted,
            expected_tenant="tenant-a",
        )
    with pytest.raises(ValueError, match="not cancelable"):
        normalize_cancel_task_request(
            {"tenant": "tenant-a", "id": "external-task"},
            authenticated_sender_id="an1sender",
            existing_task={**persisted, "state": "completed"},
            expected_tenant="tenant-a",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["message"].update(role="ROLE_AGENT"), "ROLE_USER"),
        (lambda value: value["message"].update(taskId="existing"), "persisted"),
        (
            lambda value: value["message"]["parts"][0].update(data={"also": True}),
            "exactly one",
        ),
        (lambda value: value["message"]["parts"].append({"raw": "***"}), "base64"),
        (lambda value: value["message"].update(unknown=True), "unsupported"),
    ],
)
def test_inbound_message_rejects_ambiguous_or_unsupported_v1_shapes(
    mutation,
    message: str,
) -> None:
    value = send_request()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        inbound_message_to_task(
            value,
            authenticated_sender_id="an1sender",
            skill=SKILL,
        )


def test_all_anet_task_states_have_exact_a2a_v1_enum_mapping() -> None:
    assert set(A2A_STATE_BY_ANET) == {
        "submitted",
        "working",
        "completed",
        "failed",
        "canceled",
        "input-required",
        "rejected",
        "auth-required",
    }
    assert ANET_STATE_BY_A2A == {
        a2a: anet for anet, a2a in A2A_STATE_BY_ANET.items()
    }
    assert A2A_STATE_BY_ANET["canceled"] == "TASK_STATE_CANCELED"


def test_status_and_result_map_to_v1_stream_response_members() -> None:
    task_id = "ab" * 16
    working = task_event_to_a2a_stream_events(
        "agent.task.status",
        task_status(
            task_id=task_id,
            state="working",
            message="running tests",
            progress=0.5,
        ),
        context_id="context-1",
    )
    assert working[0]["statusUpdate"]["status"]["state"] == "TASK_STATE_WORKING"
    assert working[0]["statusUpdate"]["metadata"] == {"anetProgress": 0.5}
    assert working[0]["statusUpdate"]["status"]["message"]["role"] == "ROLE_AGENT"

    completed = task_event_to_a2a_stream_events(
        "agent.task.result",
        task_result(
            task_id=task_id,
            state="completed",
            output={"passed": 42},
        ),
        context_id="context-1",
    )
    assert [next(iter(event)) for event in completed] == [
        "artifactUpdate",
        "statusUpdate",
    ]
    artifact = completed[0]["artifactUpdate"]
    assert artifact["artifact"]["parts"] == [
        {"data": {"passed": 42}, "mediaType": "application/json"}
    ]
    assert artifact["lastChunk"] is True
    assert completed[1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"

    failed = task_event_to_a2a_stream_events(
        "agent.task.result",
        task_result(
            task_id=task_id,
            state="failed",
            error="dependency unavailable",
        ),
        context_id="context-1",
    )
    status = failed[0]["statusUpdate"]["status"]
    assert status["state"] == "TASK_STATE_FAILED"
    assert status["message"]["parts"][0]["text"] == "dependency unavailable"
