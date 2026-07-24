from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from .agent_protocol import (
    TERMINAL_TASK_STATES,
    task_request,
    validate_task_message,
)


A2A_PROTOCOL_VERSION = "1.0"
A2A_AGENT_CARD_PATH = "/.well-known/agent-card.json"

A2A_STATE_BY_ANET = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "rejected": "TASK_STATE_REJECTED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
}
ANET_STATE_BY_A2A = {value: key for key, value in A2A_STATE_BY_ANET.items()}

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_JSON_BYTES = 7 * 1024 * 1024
_MAX_PARTS = 64
_MAX_ID_CHARS = 1024
_MAX_OBJECTIVE_CHARS = 16_000
_MESSAGE_FIELDS = frozenset(
    {
        "messageId",
        "contextId",
        "taskId",
        "role",
        "parts",
        "metadata",
        "extensions",
        "referenceTaskIds",
    }
)
_PART_FIELDS = frozenset(
    {"text", "raw", "url", "data", "metadata", "filename", "mediaType"}
)
_PART_CONTENT_FIELDS = frozenset({"text", "raw", "url", "data"})
_REQUEST_FIELDS = frozenset({"tenant", "message", "configuration", "metadata"})
_CANCEL_REQUEST_FIELDS = frozenset({"tenant", "id", "metadata"})


def _required_string(value: Any, name: str, *, limit: int = _MAX_ID_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} is too long")
    return value


def _string_list(value: Any, name: str, *, limit: int = 64) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{name} must be a bounded list")
    return [_required_string(item, f"{name} item") for item in value]


def _json_clone(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON values") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError(f"{name} is too large")
    return json.loads(encoded.decode("utf-8"))


def _json_map(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return _json_clone(value, name)


def _safe_id(value: str, name: str) -> str:
    value = _required_string(value, name, limit=128).lower()
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must use lowercase capability-style syntax")
    return value


def _endpoint_url(value: str) -> str:
    value = _required_string(value, "A2A endpoint", limit=2048)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A2A endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("A2A endpoint cannot contain credentials, query, or fragment")
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        loopback = hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise ValueError("non-loopback A2A endpoints must use HTTPS")
    return value


def _derived_id(sender_id: str, external_id: str, *, person: bytes) -> str:
    sender_id = _required_string(sender_id, "authenticated sender ID", limit=512)
    external_id = _required_string(external_id, "A2A messageId")
    return hashlib.blake2s(
        f"{sender_id}\0{external_id}".encode("utf-8"),
        digest_size=16,
        person=person,
    ).hexdigest()


@dataclass(frozen=True)
class A2ASkillBinding:
    """Public A2A skill metadata plus private local execution requirements."""

    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    examples: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("application/json", "text/plain")
    output_modes: tuple[str, ...] = ("application/json", "text/plain")

    def agent_skill(self) -> dict[str, Any]:
        skill_id = _safe_id(self.id, "A2A skill id")
        tags = tuple(dict.fromkeys(_required_string(tag, "A2A skill tag") for tag in self.tags))
        if not tags:
            raise ValueError("A2A skill requires at least one tag")
        value: dict[str, Any] = {
            "id": skill_id,
            "name": _required_string(self.name, "A2A skill name"),
            "description": _required_string(
                self.description,
                "A2A skill description",
                limit=8192,
            ),
            "tags": list(tags),
        }
        if self.examples:
            value["examples"] = [
                _required_string(item, "A2A skill example", limit=8192)
                for item in self.examples
            ]
        if self.input_modes:
            value["inputModes"] = [
                _required_string(item, "A2A input mode")
                for item in self.input_modes
            ]
        if self.output_modes:
            value["outputModes"] = [
                _required_string(item, "A2A output mode")
                for item in self.output_modes
            ]
        return value


@dataclass(frozen=True)
class A2AInboundTask:
    task_id: str
    a2a_task_id: str
    context_id: str
    message_id: str
    skill_id: str
    message: dict[str, Any]
    body: dict[str, Any]


def bearer_security(
    scheme_name: str = "bearer",
    *,
    description: str = "Bearer token issued by the Anet A2A gateway operator",
    bearer_format: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return A2A v1 ProtoJSON security scheme and matching requirement."""

    scheme_name = _safe_id(scheme_name, "A2A security scheme name")
    http_auth: dict[str, Any] = {
        "scheme": "Bearer",
        "description": _required_string(description, "A2A security description"),
    }
    if bearer_format:
        http_auth["bearerFormat"] = _required_string(
            bearer_format,
            "A2A bearer format",
        )
    return (
        {scheme_name: {"httpAuthSecurityScheme": http_auth}},
        [{"schemes": {scheme_name: {"list": []}}}],
    )


def _validate_security(
    schemes: dict[str, Any] | None,
    requirements: list[dict[str, Any]] | None,
    *,
    allow_unauthenticated: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_schemes = _json_map(schemes, "A2A securitySchemes")
    normalized_requirements = _json_clone(
        requirements or [],
        "A2A securityRequirements",
    )
    if not isinstance(normalized_requirements, list):
        raise ValueError("A2A securityRequirements must be a list")
    if not normalized_schemes or not normalized_requirements:
        if not allow_unauthenticated:
            raise ValueError(
                "A2A Agent Card requires explicit security or "
                "allow_unauthenticated=True"
            )
        if normalized_schemes or normalized_requirements:
            raise ValueError(
                "A2A security schemes and requirements must be configured together"
            )
        return {}, []

    for requirement in normalized_requirements:
        if not isinstance(requirement, dict) or not isinstance(
            requirement.get("schemes"), dict
        ):
            raise ValueError("invalid A2A security requirement")
        for name, scopes in requirement["schemes"].items():
            if name not in normalized_schemes:
                raise ValueError(f"unknown A2A security scheme in requirement: {name}")
            if not isinstance(scopes, dict) or not isinstance(scopes.get("list"), list):
                raise ValueError("invalid A2A security requirement scopes")
            _string_list(scopes["list"], "A2A security scopes")
    return normalized_schemes, normalized_requirements


def build_agent_card(
    *,
    name: str,
    description: str,
    endpoint: str,
    agent_version: str,
    skills: Iterable[A2ASkillBinding],
    security_schemes: dict[str, Any] | None = None,
    security_requirements: list[dict[str, Any]] | None = None,
    protocol_binding: str = "JSONRPC",
    tenant: str = "",
    default_input_modes: tuple[str, ...] = ("application/json", "text/plain"),
    default_output_modes: tuple[str, ...] = ("application/json", "text/plain"),
    streaming: bool = False,
    allow_unauthenticated: bool = False,
) -> dict[str, Any]:
    """Build a minimal A2A 1.0 Agent Card from an explicit public skill set.

    This function deliberately does not accept a PeerCard. Transport capabilities,
    Node IDs, addresses, and signing keys must not be projected accidentally.
    """

    public_skills = [skill.agent_skill() for skill in skills]
    if not public_skills:
        raise ValueError("A2A Agent Card requires at least one skill")
    skill_ids = [skill["id"] for skill in public_skills]
    if len(skill_ids) != len(set(skill_ids)):
        raise ValueError("A2A Agent Card skill IDs must be unique")
    schemes, requirements = _validate_security(
        security_schemes,
        security_requirements,
        allow_unauthenticated=allow_unauthenticated,
    )
    interface: dict[str, Any] = {
        "url": _endpoint_url(endpoint),
        "protocolBinding": _required_string(
            protocol_binding,
            "A2A protocol binding",
        ),
        "protocolVersion": A2A_PROTOCOL_VERSION,
    }
    if tenant:
        interface["tenant"] = _required_string(tenant, "A2A tenant")

    card: dict[str, Any] = {
        "name": _required_string(name, "A2A agent name"),
        "description": _required_string(
            description,
            "A2A agent description",
            limit=8192,
        ),
        "supportedInterfaces": [interface],
        "version": _required_string(agent_version, "A2A agent version"),
        "capabilities": {
            "streaming": bool(streaming),
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": [
            _required_string(item, "A2A default input mode")
            for item in default_input_modes
        ],
        "defaultOutputModes": [
            _required_string(item, "A2A default output mode")
            for item in default_output_modes
        ],
        "skills": public_skills,
    }
    if not card["defaultInputModes"] or not card["defaultOutputModes"]:
        raise ValueError("A2A Agent Card requires default input and output modes")
    if schemes:
        card["securitySchemes"] = schemes
        card["securityRequirements"] = requirements
    return card


def _normalize_part(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("A2A message parts must be objects")
    unknown = set(value) - _PART_FIELDS
    if unknown:
        raise ValueError(f"unsupported A2A Part fields: {sorted(unknown)!r}")
    content_fields = set(value) & _PART_CONTENT_FIELDS
    if len(content_fields) != 1:
        raise ValueError("A2A Part must contain exactly one content member")

    normalized: dict[str, Any] = {}
    content_field = next(iter(content_fields))
    content = value[content_field]
    if content_field in {"text", "raw", "url"}:
        if not isinstance(content, str):
            raise ValueError(f"A2A Part {content_field} must be a string")
        if content_field == "raw":
            try:
                base64.b64decode(content, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("A2A raw Part must be valid base64") from exc
        if content_field == "url" and not content.strip():
            raise ValueError("A2A URL Part cannot be empty")
        normalized[content_field] = content
    else:
        normalized["data"] = _json_clone(content, "A2A data Part")

    if "metadata" in value:
        normalized["metadata"] = _json_map(value["metadata"], "A2A Part metadata")
    for field in ("filename", "mediaType"):
        if field in value:
            normalized[field] = _required_string(
                value[field],
                f"A2A Part {field}",
            )
    return normalized


def _normalize_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("A2A SendMessageRequest.message must be an object")
    unknown = set(value) - _MESSAGE_FIELDS
    if unknown:
        raise ValueError(f"unsupported A2A Message fields: {sorted(unknown)!r}")
    message_id = _required_string(value.get("messageId"), "A2A messageId")
    if value.get("role") != "ROLE_USER":
        raise ValueError("A2A inbound Message role must be ROLE_USER")
    if value.get("taskId"):
        raise ValueError(
            "A2A follow-up taskId requires a persisted gateway task mapping"
        )
    parts = value.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= _MAX_PARTS:
        raise ValueError("A2A Message requires a bounded non-empty parts list")

    normalized: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [_normalize_part(part) for part in parts],
    }
    for field in ("contextId",):
        if value.get(field):
            normalized[field] = _required_string(value[field], f"A2A {field}")
    if "metadata" in value:
        normalized["metadata"] = _json_map(
            value["metadata"],
            "A2A Message metadata",
        )
    for field in ("extensions", "referenceTaskIds"):
        if field in value:
            normalized[field] = _string_list(value[field], f"A2A Message {field}")
    return normalized


def _objective(message: dict[str, Any]) -> str:
    text = "\n".join(
        part["text"].strip()
        for part in message["parts"]
        if "text" in part and part["text"].strip()
    )
    if not text:
        return f"Handle A2A message {message['messageId']}"
    if len(text) <= _MAX_OBJECTIVE_CHARS:
        return text
    return f"{text[: _MAX_OBJECTIVE_CHARS - 1]}…"


def inbound_message_to_task(
    request: dict[str, Any],
    *,
    authenticated_sender_id: str,
    skill: A2ASkillBinding,
    expected_tenant: str = "",
    existing_task: dict[str, Any] | None = None,
) -> A2AInboundTask:
    """Translate one new A2A 1.0 SendMessageRequest into an Anet task request.

    ``authenticated_sender_id`` must come from the gateway authentication
    boundary. Capabilities come only from the locally configured skill binding;
    request metadata cannot expand them.
    """

    if not isinstance(request, dict):
        raise ValueError("A2A SendMessageRequest must be an object")
    unknown = set(request) - _REQUEST_FIELDS
    if unknown:
        raise ValueError(f"unsupported A2A SendMessageRequest fields: {sorted(unknown)!r}")
    normalized_request = _json_clone(request, "A2A SendMessageRequest")
    tenant = str(normalized_request.get("tenant", ""))
    if tenant != expected_tenant:
        raise PermissionError("A2A tenant does not match the selected interface")

    raw_message = normalized_request.get("message")
    raw_task_id = (
        str(raw_message.get("taskId", "")).strip()
        if isinstance(raw_message, dict)
        else ""
    )
    if raw_task_id and existing_task is None:
        raise ValueError(
            "A2A follow-up taskId requires a persisted gateway task mapping"
        )
    if existing_task is not None and not raw_task_id:
        raise ValueError("persisted A2A task mapping requires a follow-up taskId")

    message_for_normalization = (
        {key: value for key, value in raw_message.items() if key != "taskId"}
        if raw_task_id and isinstance(raw_message, dict)
        else raw_message
    )
    message = _normalize_message(message_for_normalization)
    message_id = message["messageId"]
    task_id = _derived_id(
        authenticated_sender_id,
        message_id,
        person=b"a2a-task",
    )
    skill_id = _safe_id(skill.id, "A2A skill id")
    if existing_task is None:
        a2a_task_id = task_id
        context_id = message.get("contextId") or (
            "anet-" + _derived_id(
                authenticated_sender_id,
                message_id,
                person=b"a2a-ctx1",
            )
        )
    else:
        required_fields = {
            "a2a_task_id",
            "context_id",
            "sender_node_id",
            "skill_id",
            "tenant",
            "protocol_version",
        }
        if not required_fields.issubset(existing_task):
            raise ValueError("incomplete persisted A2A task mapping")
        a2a_task_id = _required_string(
            existing_task["a2a_task_id"],
            "persisted A2A task ID",
        )
        context_id = _required_string(
            existing_task["context_id"],
            "persisted A2A context ID",
        )
        if raw_task_id != a2a_task_id:
            raise ValueError("A2A follow-up taskId does not match persisted mapping")
        if existing_task["sender_node_id"] != authenticated_sender_id:
            raise PermissionError(
                "A2A follow-up sender does not match persisted task mapping"
            )
        if existing_task["skill_id"] != skill_id:
            raise PermissionError(
                "A2A follow-up skill does not match persisted task mapping"
            )
        if existing_task["tenant"] != tenant:
            raise PermissionError(
                "A2A follow-up tenant does not match persisted task mapping"
            )
        if existing_task["protocol_version"] != A2A_PROTOCOL_VERSION:
            raise ValueError("persisted A2A task uses another protocol version")
        if message.get("contextId") and message["contextId"] != context_id:
            raise ValueError(
                "A2A follow-up contextId does not match persisted task mapping"
            )
        message["taskId"] = a2a_task_id
        message["contextId"] = context_id

    body = task_request(
        task_id=task_id,
        objective=_objective(message),
        input={
            "a2aMessage": message,
            "configuration": _json_map(
                normalized_request.get("configuration"),
                "A2A SendMessageRequest.configuration",
            ),
            "metadata": _json_map(
                normalized_request.get("metadata"),
                "A2A SendMessageRequest.metadata",
            ),
        },
        required_capabilities=list(skill.required_capabilities),
        context={
            "a2a": {
                "protocolVersion": A2A_PROTOCOL_VERSION,
                "messageId": message_id,
                "contextId": context_id,
                "taskId": a2a_task_id,
                "skillId": skill_id,
                "tenant": tenant,
            }
        },
    )
    return A2AInboundTask(
        task_id=task_id,
        a2a_task_id=a2a_task_id,
        context_id=context_id,
        message_id=message_id,
        skill_id=skill_id,
        message=message,
        body=body,
    )


def initial_a2a_task(value: A2AInboundTask) -> dict[str, Any]:
    return {
        "id": value.a2a_task_id,
        "contextId": value.context_id,
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "history": [value.message],
        "metadata": {"anet": {"skillId": value.skill_id}},
    }


def normalize_cancel_task_request(
    request: dict[str, Any],
    *,
    authenticated_sender_id: str,
    existing_task: dict[str, Any],
    expected_tenant: str = "",
) -> dict[str, Any]:
    """Validate the supported A2A 1.0 CancelTaskRequest boundary."""

    if not isinstance(request, dict):
        raise ValueError("A2A CancelTaskRequest must be an object")
    unknown = set(request) - _CANCEL_REQUEST_FIELDS
    if unknown:
        raise ValueError(
            f"unsupported A2A CancelTaskRequest fields: {sorted(unknown)!r}"
        )
    normalized = _json_clone(request, "A2A CancelTaskRequest")
    tenant = str(normalized.get("tenant", ""))
    if tenant != expected_tenant:
        raise PermissionError("A2A tenant does not match the selected interface")
    required = {
        "a2a_task_id",
        "sender_node_id",
        "tenant",
        "state",
        "protocol_version",
    }
    if not isinstance(existing_task, dict) or not required.issubset(existing_task):
        raise ValueError("incomplete persisted A2A task mapping")
    task_id = _required_string(normalized.get("id"), "A2A task ID")
    if task_id != existing_task["a2a_task_id"]:
        raise ValueError("A2A cancellation task ID does not match persisted mapping")
    if existing_task["sender_node_id"] != authenticated_sender_id:
        raise PermissionError(
            "A2A cancellation sender does not match persisted task mapping"
        )
    if existing_task["tenant"] != tenant:
        raise PermissionError(
            "A2A cancellation tenant does not match persisted task mapping"
        )
    if existing_task["protocol_version"] != A2A_PROTOCOL_VERSION:
        raise ValueError("persisted A2A task uses another protocol version")
    terminal_cancel_duplicate = (
        existing_task["state"] == "canceled"
        and existing_task.get("cancel_state") == "confirmed"
    )
    if (
        existing_task["state"] in TERMINAL_TASK_STATES
        and not terminal_cancel_duplicate
    ):
        raise ValueError("terminal A2A task is not cancelable")
    metadata = _json_map(
        normalized.get("metadata"),
        "A2A CancelTaskRequest.metadata",
    )
    return {
        "tenant": tenant,
        "id": task_id,
        "metadata": metadata,
    }


def _agent_status_message(
    *,
    source_task_id: str,
    a2a_task_id: str,
    context_id: str,
    state: str,
    text: str,
) -> dict[str, Any]:
    message_id = hashlib.blake2s(
        f"{source_task_id}\0{state}\0{text}".encode("utf-8"),
        digest_size=16,
        person=b"a2a-msg1",
    ).hexdigest()
    return {
        "messageId": message_id,
        "contextId": context_id,
        "taskId": a2a_task_id,
        "role": "ROLE_AGENT",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }


def _output_part(output: Any) -> dict[str, Any]:
    if isinstance(output, str):
        return {"text": output, "mediaType": "text/plain"}
    if isinstance(output, bytes):
        return {
            "raw": base64.b64encode(output).decode("ascii"),
            "mediaType": "application/octet-stream",
        }
    return {
        "data": _json_clone(output, "Anet task output"),
        "mediaType": "application/json",
    }


def task_event_to_a2a_stream_events(
    kind: str,
    body: dict[str, Any],
    *,
    context_id: str,
    a2a_task_id: str = "",
) -> tuple[dict[str, Any], ...]:
    """Map a validated Anet status/result to A2A v1 StreamResponse values."""

    if kind not in {"agent.task.status", "agent.task.result"}:
        raise ValueError("only Anet task status and result events map to A2A streams")
    context_id = _required_string(context_id, "A2A contextId")
    normalized = validate_task_message(kind, body)
    source_task_id = normalized["task_id"]
    a2a_task_id = (
        _required_string(a2a_task_id, "A2A taskId")
        if a2a_task_id
        else source_task_id
    )
    state = normalized["state"]
    status: dict[str, Any] = {"state": A2A_STATE_BY_ANET[state]}
    status_text = (
        str(normalized.get("message", "")).strip()
        if kind == "agent.task.status"
        else str(normalized.get("error", "")).strip()
    )
    if status_text:
        status["message"] = _agent_status_message(
            source_task_id=source_task_id,
            a2a_task_id=a2a_task_id,
            context_id=context_id,
            state=state,
            text=status_text,
        )

    events: list[dict[str, Any]] = []
    if kind == "agent.task.result" and normalized.get("output") is not None:
        events.append(
            {
                "artifactUpdate": {
                    "taskId": a2a_task_id,
                    "contextId": context_id,
                    "artifact": {
                        "artifactId": "result",
                        "name": "Anet task result",
                        "parts": [_output_part(normalized["output"])],
                    },
                    "append": False,
                    "lastChunk": True,
                }
            }
        )

    status_update: dict[str, Any] = {
        "taskId": a2a_task_id,
        "contextId": context_id,
        "status": status,
    }
    if kind == "agent.task.status" and normalized.get("progress") is not None:
        status_update["metadata"] = {"anetProgress": normalized["progress"]}
    events.append({"statusUpdate": status_update})
    return tuple(events)


def validate_a2a_stream_event(
    value: dict[str, Any],
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any]:
    """Validate the supported A2A 1.0 StreamResponse event subset."""

    task_id = _required_string(task_id, "A2A taskId")
    context_id = _required_string(context_id, "A2A contextId")
    normalized = _json_clone(value, "A2A stream event")
    if not isinstance(normalized, dict) or len(normalized) != 1:
        raise ValueError("A2A stream event requires exactly one wrapper member")
    wrapper = next(iter(normalized))
    if wrapper not in {"statusUpdate", "artifactUpdate"}:
        raise ValueError("unsupported A2A stream event wrapper")
    update = normalized[wrapper]
    if not isinstance(update, dict):
        raise ValueError("A2A stream event update must be an object")
    if update.get("taskId") != task_id or update.get("contextId") != context_id:
        raise ValueError("A2A stream event task or context does not match mapping")

    if wrapper == "statusUpdate":
        allowed = {"taskId", "contextId", "status", "metadata"}
        if set(update) - allowed:
            raise ValueError("unsupported A2A status update fields")
        status = update.get("status")
        if not isinstance(status, dict) or set(status) - {
            "state",
            "message",
            "timestamp",
        }:
            raise ValueError("invalid A2A task status")
        if status.get("state") not in ANET_STATE_BY_A2A:
            raise ValueError("invalid A2A task state")
        message = status.get("message")
        if message is not None:
            if not isinstance(message, dict):
                raise ValueError("A2A status message must be an object")
            if (
                message.get("role") != "ROLE_AGENT"
                or message.get("taskId") != task_id
                or message.get("contextId") != context_id
            ):
                raise ValueError("A2A status message is not bound to the task")
            _required_string(message.get("messageId"), "A2A status messageId")
            parts = message.get("parts")
            if not isinstance(parts, list) or not 1 <= len(parts) <= _MAX_PARTS:
                raise ValueError("A2A status message requires parts")
            for part in parts:
                _normalize_part(part)
        if "metadata" in update:
            _json_map(update["metadata"], "A2A status update metadata")
        return normalized

    allowed = {
        "taskId",
        "contextId",
        "artifact",
        "append",
        "lastChunk",
        "metadata",
    }
    if set(update) - allowed:
        raise ValueError("unsupported A2A artifact update fields")
    artifact = update.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) - {
        "artifactId",
        "name",
        "description",
        "parts",
        "metadata",
        "extensions",
    }:
        raise ValueError("invalid A2A artifact")
    _required_string(artifact.get("artifactId"), "A2A artifactId")
    parts = artifact.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= _MAX_PARTS:
        raise ValueError("A2A artifact requires parts")
    for part in parts:
        _normalize_part(part)
    for field in ("append", "lastChunk"):
        if field in update and not isinstance(update[field], bool):
            raise ValueError(f"A2A artifact update {field} must be boolean")
    if "metadata" in update:
        _json_map(update["metadata"], "A2A artifact update metadata")
    return normalized
