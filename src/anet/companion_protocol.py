from __future__ import annotations

import os
import re
import time
import math
from collections.abc import Mapping
from typing import Any


COMPANION_PROTOCOL = "anet.companion"
COMPANION_PROTOCOL_VERSION = 1

OBSERVATION_BATCH_KIND = "companion.observation.batch"
EPISODE_KIND = "companion.episode"
INTERVENTION_KIND = "companion.intervention"
USER_RESPONSE_KIND = "companion.user-response"
APPROVAL_REQUEST_KIND = "companion.approval.request"
APPROVAL_DECISION_KIND = "companion.approval.decision"
COMPANION_KINDS = frozenset(
    {
        OBSERVATION_BATCH_KIND,
        EPISODE_KIND,
        INTERVENTION_KIND,
        USER_RESPONSE_KIND,
        APPROVAL_REQUEST_KIND,
        APPROVAL_DECISION_KIND,
    }
)

DATA_LEVELS = frozenset({"operational", "personal-low"})
CONSENT_BASES = frozenset({"device-essential", "user-opt-in", "user-initiated"})
INTERVENTION_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
INTERVENTION_CATEGORIES = frozenset(
    {"notification", "reminder", "choice", "report"}
)
USER_DISPOSITIONS = frozenset(
    {"presented", "ignored", "snoozed", "accepted", "rejected", "answered"}
)
APPROVAL_DECISIONS = frozenset({"approved", "rejected"})

_OBJECT_TYPES = {
    OBSERVATION_BATCH_KIND: "observation_batch",
    EPISODE_KIND: "episode",
    INTERVENTION_KIND: "intervention",
    USER_RESPONSE_KIND: "user_response",
    APPROVAL_REQUEST_KIND: "approval_request",
    APPROVAL_DECISION_KIND: "approval_decision",
}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NODE_ID_RE = re.compile(r"^an1[a-z2-7]{17,}$")
_HUMAN_ID_RE = re.compile(r"^hu1[a-z2-7]{17,}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_RESOURCE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9:/_.@-]{0,511}$")

_MAX_BATCH_OBSERVATIONS = 256
_MAX_SOURCE_BATCHES = 64
_MAX_METRICS = 64
_MAX_OPTIONS = 16
_MAX_TEXT_CHARS = 4096
_MAX_SELF_REPORT_CHARS = 2048
_MAX_BODY_TTL_MS = 7 * 24 * 60 * 60 * 1000
_MAX_APPROVAL_REQUEST_TTL_MS = 15 * 60 * 1000
_MAX_APPROVAL_GRANT_TTL_MS = 60 * 60 * 1000
_MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

_OBSERVATION_TYPES = frozenset(
    {
        "device.battery",
        "device.network",
        "human.presence",
        "human.self-report",
        "device.app-category-window",
    }
)
_EPISODE_TYPES = frozenset(
    {
        "device.battery-window",
        "device.connectivity-window",
        "human.presence-window",
        "human.self-report",
        "device.app-category-window",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "audio",
        "audio_bytes",
        "image",
        "image_bytes",
        "video",
        "video_bytes",
        "raw",
        "raw_events",
        "precise_location",
        "latitude",
        "longitude",
        "chat_body",
        "message_body",
        "health_detail",
        "diagnosis",
        "emotion",
        "mood",
        "fatigue",
        "human_state",
        "state_hypothesis",
    }
)


def new_companion_id() -> str:
    return os.urandom(16).hex()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _exact(
    value: Any,
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a map")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _identifier(value: Any, *, name: str) -> str:
    normalized = str(value).strip().lower()
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"{name} must be 32 lowercase hexadecimal characters")
    return normalized


def _digest(value: Any, *, name: str) -> str:
    normalized = str(value).strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return normalized


def _node_id(value: Any, *, name: str = "device_node_id") -> str:
    normalized = str(value).strip().lower()
    if not _NODE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {name}")
    return normalized


def _human_id(value: Any) -> str:
    normalized = str(value).strip().lower()
    if not _HUMAN_ID_RE.fullmatch(normalized):
        raise ValueError("invalid human_id")
    return normalized


def _token(value: Any, *, name: str) -> str:
    normalized = str(value).strip().lower()
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"invalid {name}")
    return normalized


def _text(value: Any, *, name: str, required: bool = False, limit: int = _MAX_TEXT_CHARS) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{name} must be text")
    normalized = str(value).strip()
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if len(normalized) > limit:
        raise ValueError(f"{name} is too long")
    return normalized


def _integer(value: Any, *, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _timestamp(value: Any, *, name: str) -> int:
    return _integer(value, name=name, minimum=1)


def _lifetime(
    created_ms: Any,
    expires_ms: Any,
    *,
    max_ttl_ms: int = _MAX_BODY_TTL_MS,
) -> tuple[int, int]:
    created = _timestamp(created_ms, name="created_ms")
    expires = _timestamp(expires_ms, name="expires_ms")
    if expires <= created or expires - created > max_ttl_ms:
        raise ValueError("invalid object lifetime")
    return created, expires


def _base(
    *,
    object_type: str,
    object_id_name: str,
    object_id: str,
    created_ms: int | None,
    expires_ms: int | None,
    ttl_ms: int,
) -> dict[str, Any]:
    created = _now_ms() if created_ms is None else created_ms
    expires = created + ttl_ms if expires_ms is None else expires_ms
    created, expires = _lifetime(created, expires)
    return {
        "protocol": COMPANION_PROTOCOL,
        "version": COMPANION_PROTOCOL_VERSION,
        "object_type": object_type,
        object_id_name: _identifier(object_id or new_companion_id(), name=object_id_name),
        "created_ms": created,
        "expires_ms": expires,
    }


def _validate_base(
    kind: str,
    body: Any,
    *,
    object_id_name: str,
    additional_fields: set[str],
    max_ttl_ms: int = _MAX_BODY_TTL_MS,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError(f"{_OBJECT_TYPES[kind]} must be a map")
    if body.get("protocol") != COMPANION_PROTOCOL:
        raise ValueError("unsupported Companion protocol")
    if body.get("version") != COMPANION_PROTOCOL_VERSION:
        raise ValueError("unsupported Companion protocol version")
    if body.get("object_type") != _OBJECT_TYPES[kind]:
        raise ValueError("Companion kind/object_type mismatch")
    required = {
        "protocol",
        "version",
        "object_type",
        object_id_name,
        "created_ms",
        "expires_ms",
        *additional_fields,
    }
    value = _exact(body, required=required, name=_OBJECT_TYPES[kind])
    _identifier(value[object_id_name], name=object_id_name)
    _lifetime(value["created_ms"], value["expires_ms"], max_ttl_ms=max_ttl_ms)
    return value


def _data_level(value: Any) -> str:
    normalized = str(value).strip().lower()
    if normalized not in DATA_LEVELS:
        raise ValueError("P0 Companion only permits operational or personal-low data")
    return normalized


def _consent(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        required={"basis", "grant_id", "scope"},
        name="consent",
    )
    basis = str(item["basis"]).strip().lower()
    if basis not in CONSENT_BASES:
        raise ValueError("invalid consent basis")
    grant_id = str(item["grant_id"]).strip().lower()
    if basis == "user-opt-in":
        grant_id = _identifier(grant_id, name="consent grant_id")
    elif grant_id:
        raise ValueError("only user-opt-in consent may carry grant_id")
    if not isinstance(item["scope"], list) or not item["scope"]:
        raise ValueError("consent scope must be a non-empty list")
    scope = sorted({_token(entry, name="consent scope") for entry in item["scope"]})
    if len(scope) > 32:
        raise ValueError("too many consent scopes")
    return {"basis": basis, "grant_id": grant_id, "scope": scope}


def consent_evidence(
    *,
    basis: str,
    scope: list[str] | tuple[str, ...],
    grant_id: str = "",
) -> dict[str, Any]:
    return _consent({"basis": basis, "grant_id": grant_id, "scope": list(scope)})


def _reject_forbidden(value: Any, *, path: str = "value") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"forbidden Companion field: {path}.{normalized}")
            _reject_forbidden(child, path=f"{path}.{normalized}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden(child, path=f"{path}[{index}]")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("Companion objects cannot contain binary sensor payloads")


def _observation_value(observation_type: str, value: Any) -> dict[str, Any]:
    if observation_type == "device.battery":
        item = _exact(
            value,
            required={"percent", "charging"},
            name="battery observation",
        )
        percent = _integer(item["percent"], name="battery percent")
        if percent > 100 or not isinstance(item["charging"], bool):
            raise ValueError("invalid battery observation")
        return {"percent": percent, "charging": item["charging"]}
    if observation_type == "device.network":
        item = _exact(
            value,
            required={"transport", "metered"},
            name="network observation",
        )
        transport = str(item["transport"]).strip().lower()
        if transport not in {"offline", "wifi", "cellular", "ethernet", "other"}:
            raise ValueError("invalid network transport")
        if not isinstance(item["metered"], bool):
            raise ValueError("invalid network metered flag")
        return {"transport": transport, "metered": item["metered"]}
    if observation_type == "human.presence":
        item = _exact(value, required={"state"}, name="presence observation")
        state = str(item["state"]).strip().lower()
        if state not in {"present", "away", "unknown"}:
            raise ValueError("invalid presence state")
        return {"state": state}
    if observation_type == "human.self-report":
        item = _exact(
            value,
            required={"text", "format"},
            name="self-report observation",
        )
        if item["format"] != "plain":
            raise ValueError("self-report format must be plain")
        return {
            "text": _text(
                item["text"],
                name="self-report text",
                required=True,
                limit=_MAX_SELF_REPORT_CHARS,
            ),
            "format": "plain",
        }
    item = _exact(
        value,
        required={"category_durations_ms", "switch_count"},
        name="app category window",
    )
    durations = item["category_durations_ms"]
    if not isinstance(durations, dict) or not durations:
        raise ValueError("app category durations must be a non-empty map")
    if len(durations) > 32:
        raise ValueError("too many app categories")
    normalized_durations = {
        _token(key, name="app category"): _integer(
            duration,
            name="app category duration",
        )
        for key, duration in durations.items()
    }
    return {
        "category_durations_ms": dict(sorted(normalized_durations.items())),
        "switch_count": _integer(item["switch_count"], name="app switch count"),
    }


def _observation(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        required={"observation_id", "observed_ms", "type", "value"},
        name="observation",
    )
    observation_type = str(item["type"]).strip().lower()
    if observation_type not in _OBSERVATION_TYPES:
        raise ValueError("unsupported P0 observation type")
    normalized = {
        "observation_id": _identifier(
            item["observation_id"],
            name="observation_id",
        ),
        "observed_ms": _timestamp(item["observed_ms"], name="observed_ms"),
        "type": observation_type,
        "value": _observation_value(observation_type, item["value"]),
    }
    _reject_forbidden(normalized)
    return normalized


def observation_batch(
    *,
    source_node_id: str,
    window_start_ms: int,
    window_end_ms: int,
    data_level: str,
    consent: dict[str, Any],
    observations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    batch_id: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    value = _base(
        object_type="observation_batch",
        object_id_name="batch_id",
        object_id=batch_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    value.update(
        {
            "source_node_id": source_node_id,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "data_level": data_level,
            "consent": consent,
            "observations": list(observations),
        }
    )
    return _validate_observation_batch(value)


def _validate_observation_batch(body: Any) -> dict[str, Any]:
    value = _validate_base(
        OBSERVATION_BATCH_KIND,
        body,
        object_id_name="batch_id",
        additional_fields={
            "source_node_id",
            "window_start_ms",
            "window_end_ms",
            "data_level",
            "consent",
            "observations",
        },
    )
    window_start = _timestamp(value["window_start_ms"], name="window_start_ms")
    window_end = _timestamp(value["window_end_ms"], name="window_end_ms")
    if window_end < window_start or window_end > value["created_ms"]:
        raise ValueError("invalid observation window")
    if not isinstance(value["observations"], list) or not value["observations"]:
        raise ValueError("observations must be a non-empty list")
    if len(value["observations"]) > _MAX_BATCH_OBSERVATIONS:
        raise ValueError("too many observations")
    observations = [_observation(item) for item in value["observations"]]
    observed_ids = [item["observation_id"] for item in observations]
    if len(observed_ids) != len(set(observed_ids)):
        raise ValueError("duplicate observation_id")
    if any(
        item["observed_ms"] < window_start or item["observed_ms"] > window_end
        for item in observations
    ):
        raise ValueError("observation is outside its declared window")
    consent = _consent(value["consent"])
    types = {item["type"] for item in observations}
    required_basis = {
        "human.self-report": "user-initiated",
        "human.presence": "user-opt-in",
        "device.app-category-window": "user-opt-in",
    }
    for observation_type in types:
        expected = required_basis.get(observation_type, "device-essential")
        if consent["basis"] != expected:
            raise ValueError(
                f"{observation_type} requires {expected} consent basis"
            )
        if observation_type not in consent["scope"]:
            raise ValueError("consent scope does not cover observation type")
    return {
        **value,
        "source_node_id": _node_id(value["source_node_id"], name="source_node_id"),
        "window_start_ms": window_start,
        "window_end_ms": window_end,
        "data_level": _data_level(value["data_level"]),
        "consent": consent,
        "observations": observations,
    }


def _metric_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("Companion objects cannot contain binary sensor payloads")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("episode metric numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, name="episode metric", limit=512)
    if isinstance(value, list):
        if len(value) > 32:
            raise ValueError("episode metric list is too long")
        return [_metric_value(item) for item in value]
    raise ValueError("episode metrics must contain only bounded scalar values")


def episode(
    *,
    source_node_id: str,
    source_batch_ids: list[str] | tuple[str, ...],
    episode_type: str,
    window_start_ms: int,
    window_end_ms: int,
    data_level: str,
    consent: dict[str, Any],
    transform_version: str,
    metrics: dict[str, Any],
    episode_id: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    value = _base(
        object_type="episode",
        object_id_name="episode_id",
        object_id=episode_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    value.update(
        {
            "source_node_id": source_node_id,
            "source_batch_ids": list(source_batch_ids),
            "episode_type": episode_type,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "data_level": data_level,
            "consent": consent,
            "transform_version": transform_version,
            "metrics": metrics,
        }
    )
    return _validate_episode(value)


def _validate_episode(body: Any) -> dict[str, Any]:
    value = _validate_base(
        EPISODE_KIND,
        body,
        object_id_name="episode_id",
        additional_fields={
            "source_node_id",
            "source_batch_ids",
            "episode_type",
            "window_start_ms",
            "window_end_ms",
            "data_level",
            "consent",
            "transform_version",
            "metrics",
        },
    )
    source_ids = value["source_batch_ids"]
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError("source_batch_ids must be a non-empty list")
    source_ids = [_identifier(item, name="source batch ID") for item in source_ids]
    if len(source_ids) > _MAX_SOURCE_BATCHES or len(source_ids) != len(set(source_ids)):
        raise ValueError("invalid source_batch_ids")
    episode_type = str(value["episode_type"]).strip().lower()
    if episode_type not in _EPISODE_TYPES:
        raise ValueError("unsupported P0 episode type")
    window_start = _timestamp(value["window_start_ms"], name="window_start_ms")
    window_end = _timestamp(value["window_end_ms"], name="window_end_ms")
    if window_end < window_start or window_end > value["created_ms"]:
        raise ValueError("invalid episode window")
    if not isinstance(value["metrics"], dict) or len(value["metrics"]) > _MAX_METRICS:
        raise ValueError("invalid episode metrics")
    metrics = {
        _token(key, name="episode metric name"): _metric_value(metric)
        for key, metric in value["metrics"].items()
    }
    _reject_forbidden(metrics, path="metrics")
    consent = _consent(value["consent"])
    consent_contract = {
        "device.battery-window": ("device-essential", "device.battery"),
        "device.connectivity-window": ("device-essential", "device.network"),
        "human.presence-window": ("user-opt-in", "human.presence"),
        "human.self-report": ("user-initiated", "human.self-report"),
        "device.app-category-window": (
            "user-opt-in",
            "device.app-category-window",
        ),
    }
    expected_basis, expected_scope = consent_contract[episode_type]
    if consent["basis"] != expected_basis:
        raise ValueError(
            f"{episode_type} requires {expected_basis} consent basis"
        )
    if expected_scope not in consent["scope"]:
        raise ValueError("consent scope does not cover episode type")
    return {
        **value,
        "source_node_id": _node_id(value["source_node_id"], name="source_node_id"),
        "source_batch_ids": source_ids,
        "episode_type": episode_type,
        "window_start_ms": window_start,
        "window_end_ms": window_end,
        "data_level": _data_level(value["data_level"]),
        "consent": consent,
        "transform_version": _token(
            value["transform_version"],
            name="transform_version",
        ),
        "metrics": dict(sorted(metrics.items())),
    }


def intervention(
    *,
    human_id: str,
    target_device_id: str,
    category: str,
    priority: str,
    title: str,
    message: str,
    response_options: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    related_episode_ids: list[str] | tuple[str, ...] = (),
    intervention_id: str = "",
    dedupe_key: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    value = _base(
        object_type="intervention",
        object_id_name="intervention_id",
        object_id=intervention_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    value.update(
        {
            "human_id": human_id,
            "target_device_id": target_device_id,
            "category": category,
            "priority": priority,
            "title": title,
            "message": message,
            "dedupe_key": dedupe_key or value["intervention_id"],
            "response_options": list(response_options),
            "related_episode_ids": list(related_episode_ids),
        }
    )
    return _validate_intervention(value)


def _validate_intervention(body: Any) -> dict[str, Any]:
    value = _validate_base(
        INTERVENTION_KIND,
        body,
        object_id_name="intervention_id",
        additional_fields={
            "human_id",
            "target_device_id",
            "category",
            "priority",
            "title",
            "message",
            "dedupe_key",
            "response_options",
            "related_episode_ids",
        },
    )
    category = str(value["category"]).strip().lower()
    priority = str(value["priority"]).strip().lower()
    if category not in INTERVENTION_CATEGORIES:
        raise ValueError("invalid intervention category")
    if priority not in INTERVENTION_PRIORITIES:
        raise ValueError("invalid intervention priority")
    if not isinstance(value["response_options"], list):
        raise ValueError("response_options must be a list")
    if len(value["response_options"]) > _MAX_OPTIONS:
        raise ValueError("too many response options")
    options: list[dict[str, str]] = []
    for option in value["response_options"]:
        item = _exact(
            option,
            required={"action_id", "label"},
            name="response option",
        )
        options.append(
            {
                "action_id": _token(item["action_id"], name="action_id"),
                "label": _text(
                    item["label"],
                    name="response option label",
                    required=True,
                    limit=128,
                ),
            }
        )
    action_ids = [item["action_id"] for item in options]
    if len(action_ids) != len(set(action_ids)):
        raise ValueError("duplicate response action_id")
    related = value["related_episode_ids"]
    if not isinstance(related, list) or len(related) > 64:
        raise ValueError("invalid related_episode_ids")
    related_ids = [_identifier(item, name="related episode ID") for item in related]
    if len(related_ids) != len(set(related_ids)):
        raise ValueError("duplicate related episode ID")
    return {
        **value,
        "human_id": _human_id(value["human_id"]),
        "target_device_id": _node_id(value["target_device_id"]),
        "category": category,
        "priority": priority,
        "title": _text(value["title"], name="intervention title", required=True, limit=256),
        "message": _text(value["message"], name="intervention message", required=True),
        "dedupe_key": _identifier(value["dedupe_key"], name="dedupe_key"),
        "response_options": options,
        "related_episode_ids": related_ids,
    }


def user_response(
    *,
    intervention_id: str,
    human_id: str,
    device_node_id: str,
    disposition: str,
    action_id: str = "",
    text: str = "",
    response_id: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    value = _base(
        object_type="user_response",
        object_id_name="response_id",
        object_id=response_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=24 * 60 * 60 * 1000,
    )
    value.update(
        {
            "intervention_id": intervention_id,
            "human_id": human_id,
            "device_node_id": device_node_id,
            "disposition": disposition,
            "action_id": action_id,
            "text": text,
        }
    )
    return _validate_user_response(value)


def _validate_user_response(body: Any) -> dict[str, Any]:
    value = _validate_base(
        USER_RESPONSE_KIND,
        body,
        object_id_name="response_id",
        additional_fields={
            "intervention_id",
            "human_id",
            "device_node_id",
            "disposition",
            "action_id",
            "text",
        },
    )
    disposition = str(value["disposition"]).strip().lower()
    if disposition not in USER_DISPOSITIONS:
        raise ValueError("invalid user response disposition")
    action_id = str(value["action_id"]).strip().lower()
    if action_id:
        action_id = _token(action_id, name="action_id")
    text = _text(
        value["text"],
        name="user response text",
        limit=_MAX_SELF_REPORT_CHARS,
    )
    if disposition == "answered" and not action_id and not text:
        raise ValueError("answered response requires action_id or text")
    return {
        **value,
        "intervention_id": _identifier(
            value["intervention_id"],
            name="intervention_id",
        ),
        "human_id": _human_id(value["human_id"]),
        "device_node_id": _node_id(value["device_node_id"]),
        "disposition": disposition,
        "action_id": action_id,
        "text": text,
    }


def _approval_action(value: Any) -> dict[str, Any]:
    item = _exact(
        value,
        required={"capability", "resource", "parameters_digest", "summary"},
        name="approval action",
    )
    resource = str(item["resource"]).strip()
    if not _RESOURCE_RE.fullmatch(resource):
        raise ValueError("invalid approval resource")
    return {
        "capability": _token(item["capability"], name="approval capability"),
        "resource": resource,
        "parameters_digest": _digest(
            item["parameters_digest"],
            name="parameters_digest",
        ),
        "summary": _text(
            item["summary"],
            name="approval action summary",
            required=True,
            limit=512,
        ),
    }


def _approval_scope(value: Any, *, created_ms: int) -> dict[str, Any]:
    item = _exact(
        value,
        required={"mode", "max_uses", "grant_expires_ms"},
        name="approval scope",
    )
    mode = str(item["mode"]).strip().lower()
    if mode not in {"once", "bounded"}:
        raise ValueError("approval scope mode must be once or bounded")
    max_uses = _integer(item["max_uses"], name="approval max_uses", minimum=1)
    grant_expires = _timestamp(
        item["grant_expires_ms"],
        name="grant_expires_ms",
    )
    if grant_expires <= created_ms or grant_expires - created_ms > _MAX_APPROVAL_GRANT_TTL_MS:
        raise ValueError("invalid approval grant lifetime")
    if mode == "once" and max_uses != 1:
        raise ValueError("once approval must have max_uses=1")
    if mode == "bounded" and max_uses > 100:
        raise ValueError("bounded approval max_uses exceeds limit")
    return {
        "mode": mode,
        "max_uses": max_uses,
        "grant_expires_ms": grant_expires,
    }


def approval_request(
    *,
    human_id: str,
    device_node_id: str,
    action: dict[str, Any],
    scope: dict[str, Any],
    risk: str,
    request_id: str = "",
    nonce: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    value = _base(
        object_type="approval_request",
        object_id_name="request_id",
        object_id=request_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=5 * 60 * 1000,
    )
    value.update(
        {
            "human_id": human_id,
            "device_node_id": device_node_id,
            "action": action,
            "scope": scope,
            "risk": risk,
            "nonce": nonce or new_companion_id(),
        }
    )
    return _validate_approval_request(value)


def _validate_approval_request(body: Any) -> dict[str, Any]:
    value = _validate_base(
        APPROVAL_REQUEST_KIND,
        body,
        object_id_name="request_id",
        additional_fields={
            "human_id",
            "device_node_id",
            "action",
            "scope",
            "risk",
            "nonce",
        },
        max_ttl_ms=_MAX_APPROVAL_REQUEST_TTL_MS,
    )
    return {
        **value,
        "human_id": _human_id(value["human_id"]),
        "device_node_id": _node_id(value["device_node_id"]),
        "action": _approval_action(value["action"]),
        "scope": _approval_scope(value["scope"], created_ms=value["created_ms"]),
        "risk": _text(value["risk"], name="approval risk", required=True, limit=1024),
        "nonce": _identifier(value["nonce"], name="approval nonce"),
    }


def approval_decision(
    *,
    request: dict[str, Any],
    decision: str,
    reason: str = "",
    decision_id: str = "",
    created_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    normalized_request = _validate_approval_request(request)
    value = _base(
        object_type="approval_decision",
        object_id_name="decision_id",
        object_id=decision_id,
        created_ms=created_ms,
        expires_ms=expires_ms,
        ttl_ms=5 * 60 * 1000,
    )
    value.update(
        {
            "request_id": normalized_request["request_id"],
            "human_id": normalized_request["human_id"],
            "device_node_id": normalized_request["device_node_id"],
            "decision": decision,
            "reason": reason,
            "nonce": normalized_request["nonce"],
            "action": normalized_request["action"],
            "scope": normalized_request["scope"],
        }
    )
    normalized = _validate_approval_decision(value)
    validate_approval_decision_binding(normalized_request, normalized)
    return normalized


def _validate_approval_decision(body: Any) -> dict[str, Any]:
    value = _validate_base(
        APPROVAL_DECISION_KIND,
        body,
        object_id_name="decision_id",
        additional_fields={
            "request_id",
            "human_id",
            "device_node_id",
            "decision",
            "reason",
            "nonce",
            "action",
            "scope",
        },
        max_ttl_ms=_MAX_APPROVAL_REQUEST_TTL_MS,
    )
    decision = str(value["decision"]).strip().lower()
    if decision not in APPROVAL_DECISIONS:
        raise ValueError("invalid approval decision")
    return {
        **value,
        "request_id": _identifier(value["request_id"], name="request_id"),
        "human_id": _human_id(value["human_id"]),
        "device_node_id": _node_id(value["device_node_id"]),
        "decision": decision,
        "reason": _text(value["reason"], name="approval decision reason", limit=1024),
        "nonce": _identifier(value["nonce"], name="approval nonce"),
        "action": _approval_action(value["action"]),
        "scope": _approval_scope(value["scope"], created_ms=value["created_ms"]),
    }


def validate_approval_decision_binding(
    request: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    normalized_request = _validate_approval_request(request)
    normalized_decision = _validate_approval_decision(decision)
    for field in ("request_id", "human_id", "device_node_id", "nonce", "action", "scope"):
        if normalized_decision[field] != normalized_request[field]:
            raise ValueError(f"approval decision does not match request {field}")
    if normalized_decision["created_ms"] < normalized_request["created_ms"]:
        raise ValueError("approval decision predates its request")
    if normalized_decision["created_ms"] >= normalized_request["expires_ms"]:
        raise ValueError("approval decision was created after request expiry")
    return normalized_decision


def validate_companion_message(kind: str, body: Any) -> dict[str, Any]:
    normalized_kind = str(kind).strip().lower()
    validators = {
        OBSERVATION_BATCH_KIND: _validate_observation_batch,
        EPISODE_KIND: _validate_episode,
        INTERVENTION_KIND: _validate_intervention,
        USER_RESPONSE_KIND: _validate_user_response,
        APPROVAL_REQUEST_KIND: _validate_approval_request,
        APPROVAL_DECISION_KIND: _validate_approval_decision,
    }
    validator = validators.get(normalized_kind)
    if validator is None:
        raise ValueError("unsupported Companion kind")
    return validator(body)


def validate_companion_endpoint_binding(
    kind: str,
    body: Any,
    *,
    sender_node_id: str,
    destination_node_id: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    normalized_kind = str(kind).strip().lower()
    normalized = validate_companion_message(normalized_kind, body)
    sender = _node_id(sender_node_id, name="Packet sender_node_id")
    destination = _node_id(
        destination_node_id,
        name="Packet destination_node_id",
    )
    current = _now_ms() if now_ms is None else _timestamp(
        now_ms,
        name="current time",
    )
    if normalized["created_ms"] > current + _MAX_CLOCK_SKEW_MS:
        raise ValueError("Companion object creation time is too far in the future")
    if normalized["expires_ms"] <= current:
        raise ValueError("Companion object expired")
    source_fields = {
        OBSERVATION_BATCH_KIND: "source_node_id",
        EPISODE_KIND: "source_node_id",
        USER_RESPONSE_KIND: "device_node_id",
        APPROVAL_DECISION_KIND: "device_node_id",
    }
    target_fields = {
        INTERVENTION_KIND: "target_device_id",
        APPROVAL_REQUEST_KIND: "device_node_id",
    }
    source_field = source_fields.get(normalized_kind)
    if source_field and normalized[source_field] != sender:
        raise ValueError(
            f"{normalized_kind} {source_field} does not match Packet sender"
        )
    target_field = target_fields.get(normalized_kind)
    if target_field and normalized[target_field] != destination:
        raise ValueError(
            f"{normalized_kind} {target_field} does not match Packet destination"
        )
    return normalized
