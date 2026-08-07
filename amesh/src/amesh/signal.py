from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Mapping

def canonical_pack(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


SIGNAL_PROTOCOL_PREFIX = "amesh.social"
SIGNAL_VERSION = 1
SIGNAL_ACTIONS = ("surface", "reply", "amplify", "connect_candidate")

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_RE = re.compile(r"^amesh\.social(?:\.[a-z0-9-]+)?$")


def build_signal(
    *,
    platform: str,
    adapter: str,
    source_event_id: str,
    actor_key: str,
    created_ms: int,
    expires_ms: int,
    content_level: str,
    content: str,
    labels: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    evaluation: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one bounded, platform-neutral Amesh social signal.

    The signal carries a pseudonymous actor key and content-limited payload; a
    raw platform identifier or message body never leaks into it. ``provenance``
    must include ``platform`` and ``adapter`` strings.
    """
    reputation = {
        "score": int(evaluation["reputation"]["score"]),
        "raw_score": int(evaluation["reputation"]["raw_score"]),
        "confidence": int(evaluation["reputation"]["confidence"]),
        "algorithm": str(evaluation["reputation"]["algorithm"]),
    }
    decision = {
        "action": str(evaluation["action"]),
        "allowed_actions": list(evaluation["allowed_actions"]),
        "reasons": list(evaluation["reasons"]),
        "policy_version": int(evaluation["policy_version"]),
    }
    provenance_fields = dict(provenance)
    provenance_fields.setdefault("platform", platform)
    provenance_fields.setdefault("adapter", adapter)
    seed = {
        "source_event_id": source_event_id,
        "actor_key": actor_key,
        "reputation": reputation,
        "decision": decision,
        "provenance": provenance_fields,
    }
    signal_id = hashlib.blake2s(
        canonical_pack(seed),
        digest_size=16,
        person=b"ameshsg1",
    ).hexdigest()
    value = {
        "protocol": f"{SIGNAL_PROTOCOL_PREFIX}.{platform}",
        "version": SIGNAL_VERSION,
        "signal_id": signal_id,
        "source_event_id": source_event_id,
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "actor_key": actor_key,
        "content_level": content_level,
        "content": content,
        "labels": sorted({str(item) for item in labels}),
        "reputation": reputation,
        "decision": decision,
        "provenance": provenance_fields,
    }
    return validate_signal(value)


def validate_signal(value: Any) -> dict[str, Any]:
    body = _exact_object(
        value,
        {
            "protocol",
            "version",
            "signal_id",
            "source_event_id",
            "created_ms",
            "expires_ms",
            "actor_key",
            "content_level",
            "content",
            "labels",
            "reputation",
            "decision",
            "provenance",
        },
        "Amesh social signal",
    )
    protocol = _string(body["protocol"], "protocol")
    if not _PROTOCOL_RE.fullmatch(protocol):
        raise ValueError("invalid Amesh social protocol")
    if _exact_int(body["version"], "version") != SIGNAL_VERSION:
        raise ValueError("unsupported Amesh social version")
    signal_id = _hex(body["signal_id"], 32, "signal_id")
    source_event_id = _hex(body["source_event_id"], 32, "source_event_id")
    actor_key = _hex(body["actor_key"], 64, "actor_key")
    created_ms = _positive_int(body["created_ms"], "created_ms")
    expires_ms = _positive_int(body["expires_ms"], "expires_ms")
    if not created_ms < expires_ms <= created_ms + 7 * 86_400_000:
        raise ValueError("invalid Amesh social signal lifetime")
    content_level = _string(body["content_level"], "content_level")
    if content_level not in {"metadata", "mention"}:
        raise ValueError("invalid Amesh social content level")
    content = _string(body["content"], "content")
    if len(content) > 2000:
        raise ValueError("Amesh social content is too long")
    if content_level == "metadata" and content:
        raise ValueError("metadata-only signal contains content")
    labels = _string_list(body["labels"], "labels", maximum=32, item_limit=128)
    if len(labels) != len(set(labels)):
        raise ValueError("Amesh social labels must be unique")

    reputation = _exact_object(
        body["reputation"],
        {"score", "raw_score", "confidence", "algorithm"},
        "Amesh social reputation",
    )
    normalized_reputation = {
        "score": _percentage(reputation["score"], "reputation score"),
        "raw_score": _percentage(reputation["raw_score"], "reputation raw_score"),
        "confidence": _percentage(reputation["confidence"], "reputation confidence"),
        "algorithm": _string(reputation["algorithm"], "reputation algorithm"),
    }

    decision = _exact_object(
        body["decision"],
        {"action", "allowed_actions", "reasons", "policy_version"},
        "Amesh social decision",
    )
    action = _string(decision["action"], "decision action")
    if action not in SIGNAL_ACTIONS and action != "observe":
        raise ValueError("invalid Amesh social decision action")
    allowed_actions = _string_list(
        decision["allowed_actions"],
        "allowed_actions",
        maximum=5,
    )
    if (
        not allowed_actions
        or allowed_actions[0] != "observe"
        or action != allowed_actions[-1]
        or any(
            item not in SIGNAL_ACTIONS and item != "observe" for item in allowed_actions
        )
        or allowed_actions != list(dict.fromkeys(allowed_actions))
    ):
        raise ValueError("invalid Amesh social allowed action sequence")
    reasons = _string_list(
        decision["reasons"],
        "decision reasons",
        maximum=32,
        item_limit=256,
    )
    policy_version = _exact_int(
        decision["policy_version"],
        "policy_version",
    )

    provenance = _exact_provenance(body["provenance"])
    return {
        "protocol": protocol,
        "version": SIGNAL_VERSION,
        "signal_id": signal_id,
        "source_event_id": source_event_id,
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "actor_key": actor_key,
        "content_level": content_level,
        "content": content,
        "labels": labels,
        "reputation": normalized_reputation,
        "decision": {
            "action": action,
            "allowed_actions": allowed_actions,
            "reasons": reasons,
            "policy_version": policy_version,
        },
        "provenance": provenance,
    }


def _exact_provenance(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Amesh social provenance must be an object")
    if "platform" not in value or "adapter" not in value:
        raise ValueError("Amesh social provenance requires platform and adapter")
    if len(value) > 8:
        raise ValueError("Amesh social provenance has too many fields")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", str(key)):
            raise ValueError("invalid Amesh social provenance field")
        result[str(key)] = _string(item, f"provenance {key}")
        if len(result[str(key)]) > 64:
            raise ValueError("Amesh social provenance field is too long")
    return result


class DirectorySignalSink:
    """Store-and-forward outbox for emitted social signals.

    Signals are written atomically with private permissions under
    ``<home>/amesh-outbound/<platform>-<signal_id>.json``. The sink never
    decrypts, re-orders, or validates payloads beyond a bounded shape check;
    each adapter is responsible for its own protocol validation.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def emit(self, body: Mapping[str, Any]) -> str:
        signal = _bounded_copy(body)
        platform = str(signal["provenance"]["platform"])
        signal_id = _hex(signal["signal_id"], 32, "signal_id")
        path = self.directory / f"{platform}-{signal_id}.json"
        _atomic_json(path, signal)
        return signal_id

    def list(self, *, platform: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("signal limit must be 1 to 10000")
        result: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                signal = validate_signal(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, json.JSONDecodeError, OSError):
                continue
            if platform and signal["provenance"]["platform"] != platform:
                continue
            result.append(signal)
            if len(result) >= int(limit):
                break
        return result

    def count(self) -> int:
        return len(self.list())


def _bounded_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    signal = dict(value)
    if "provenance" in signal and isinstance(signal["provenance"], Mapping):
        signal["provenance"] = dict(signal["provenance"])
    return signal


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = (
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _exact_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = fields - set(value)
    extra = set(value) - fields
    if missing:
        raise ValueError(f"{label} is missing fields: {','.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{label} has unknown fields: {','.join(sorted(extra))}")
    return value


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _exact_int(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _percentage(value: Any, label: str) -> int:
    result = _exact_int(value, label)
    if not 0 <= result <= 100:
        raise ValueError(f"{label} must be between 0 and 100")
    return result


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    maximum: int,
    item_limit: int = 128,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = []
    for item in value:
        text = _string(item, label)
        if not text or len(text) > item_limit:
            raise ValueError(f"{label} contains invalid text")
        result.append(text)
    return result


def _hex(value: Any, length: int, label: str) -> str:
    text = _string(value, label).strip().lower()
    pattern = _HEX32_RE if length == 32 else _HEX64_RE
    if not pattern.fullmatch(text):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters")
    return text
