"""Versioned result contract for persistent Anet deployment adapters."""

from __future__ import annotations

import copy
import re
from typing import Any


RECEIPT_KIND = "anet.deployment.receipt"
RECEIPT_SCHEMA_VERSION = 1
PLATFORMS = frozenset({"windows", "wsl", "linux", "macos", "termux"})
OUTCOMES = frozenset({"created", "reused"})
FEATURES = frozenset({"core", "mcp", "full"})
NODE_ID_PATTERN = re.compile(r"^an1[a-z2-7]{17,125}$")


class DeploymentReceiptError(ValueError):
    """Raised when a deployment adapter emits an incomplete receipt."""


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeploymentReceiptError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentReceiptError(f"{field} must be a non-empty string")
    return value


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeploymentReceiptError(f"{field} must be a string array")
    return value


def validate_deployment_receipt(value: object) -> dict[str, Any]:
    """Validate and return an isolated copy of one deployment receipt.

    The receipt proves only what the installer observed at completion time. It
    is not a substitute for a later reboot, reachability, or physical-device
    release gate.
    """

    receipt = _mapping(value, "receipt")
    if receipt.get("kind") != RECEIPT_KIND:
        raise DeploymentReceiptError(f"kind must be {RECEIPT_KIND}")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise DeploymentReceiptError(
            f"schema_version must be {RECEIPT_SCHEMA_VERSION}"
        )
    if receipt.get("ok") is not True:
        raise DeploymentReceiptError("ok must be true")

    platform = receipt.get("platform")
    if platform not in PLATFORMS:
        raise DeploymentReceiptError("platform is unsupported")
    if receipt.get("outcome") not in OUTCOMES:
        raise DeploymentReceiptError("outcome must be created or reused")

    runtime = _mapping(receipt.get("runtime"), "runtime")
    _text(runtime.get("version"), "runtime.version")
    if runtime.get("feature") not in FEATURES:
        raise DeploymentReceiptError("runtime.feature is unsupported")
    _text(runtime.get("runtime"), "runtime.runtime")
    _text(runtime.get("cli"), "runtime.cli")

    node = _mapping(receipt.get("node"), "node")
    _text(node.get("home"), "node.home")
    node_id = _text(node.get("node_id"), "node.node_id")
    if not NODE_ID_PATTERN.fullmatch(node_id):
        raise DeploymentReceiptError("node.node_id is incomplete")
    _text(node.get("listen_host"), "node.listen_host")
    port = node.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DeploymentReceiptError("node.port must be between 1 and 65535")
    _string_list(node.get("advertise"), "node.advertise")
    _string_list(node.get("locator_contexts"), "node.locator_contexts")

    control = _mapping(receipt.get("control"), "control")
    _text(control.get("url"), "control.url")
    if not isinstance(control.get("key_id"), str):
        raise DeploymentReceiptError("control.key_id must be a string")
    raw_key_ids = control.get("key_ids")
    if raw_key_ids is None:
        primary_key_id = str(control.get("key_id"))
        key_ids = [primary_key_id] if primary_key_id else []
    else:
        key_ids = _string_list(raw_key_ids, "control.key_ids")
    for index, key_id in enumerate(key_ids):
        _text(key_id, f"control.key_ids[{index}]")
    if len(key_ids) != len(set(key_ids)):
        raise DeploymentReceiptError("control.key_ids must be unique")
    if bool(control.get("key_id")) != bool(key_ids) or (
        key_ids and control.get("key_id") != key_ids[0]
    ):
        raise DeploymentReceiptError("control.key_id must be the first key_ids entry")
    if control.get("verified") is not True:
        raise DeploymentReceiptError("control.verified must be true")

    supervisor = _mapping(receipt.get("supervisor"), "supervisor")
    _text(supervisor.get("kind"), "supervisor.kind")
    _text(supervisor.get("name"), "supervisor.name")
    state = _text(supervisor.get("state"), "supervisor.state")
    if state not in {"active", "running"}:
        raise DeploymentReceiptError("supervisor.state is not running")
    if supervisor.get("autostart") is not True:
        raise DeploymentReceiptError("supervisor.autostart must be true")
    health = _mapping(supervisor.get("health"), "supervisor.health")
    if health.get("kind") != "anet.supervisor.health":
        raise DeploymentReceiptError("supervisor.health.kind is invalid")
    if health.get("schema_version") != 1:
        raise DeploymentReceiptError("supervisor.health.schema_version is invalid")
    if health.get("ok") is not True or health.get("fresh") is not True:
        raise DeploymentReceiptError("supervisor.health is not healthy")
    _text(health.get("instance_id"), "supervisor.health.instance_id")
    _text(health.get("boot_session_id"), "supervisor.health.boot_session_id")
    if health.get("sync_complete") is not True:
        raise DeploymentReceiptError("supervisor health sync is incomplete")
    if health.get("supervisor_process_alive") is not True:
        raise DeploymentReceiptError("supervisor process is not alive")
    if health.get("child_process_alive") is not True:
        raise DeploymentReceiptError("Anet server child process is not alive")

    _mapping(receipt.get("preflight"), "preflight")
    return copy.deepcopy(receipt)


def build_deployment_receipt(
    *,
    platform: str,
    outcome: str,
    runtime: dict[str, Any],
    node: dict[str, Any],
    control_url: str,
    control_key_id: str,
    control_key_ids: list[str] | None = None,
    supervisor: dict[str, Any],
    preflight: dict[str, Any],
    platform_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common receipt emitted by every persistent installer adapter."""

    key_ids = list(control_key_ids or ([control_key_id] if control_key_id else []))
    value: dict[str, Any] = {
        "kind": RECEIPT_KIND,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ok": True,
        "outcome": outcome,
        "platform": platform,
        "runtime": copy.deepcopy(runtime),
        "node": copy.deepcopy(node),
        "control": {
            "url": str(control_url),
            "key_id": str(control_key_id),
            "key_ids": key_ids,
            "verified": True,
        },
        "supervisor": copy.deepcopy(supervisor),
        "preflight": copy.deepcopy(preflight),
    }
    if platform_details:
        value["platform_details"] = copy.deepcopy(platform_details)
    return validate_deployment_receipt(value)
