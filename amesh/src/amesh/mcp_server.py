from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .discovery import (
    DiscoveryStore,
    build_discovery_signal,
    discovery_database_path,
)
from .agent import AgentStore, agent_database_path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings

from .adapter import PlatformAdapter, builtin_adapter_names, load_adapter
from .model import (
    validate_action,
    validate_actor_key,
    validate_adapter_name,
    validate_effect,
)
from .policy import PermissionStore, amesh_database_path
from .relations import RelationshipHub
from .serve import amesh_outbound_dir
from .signal import DirectorySignalSink

# The parent gateway passes a deliberately small, capability-scoped env to
# this process; disable FastMCP's ambient ``.env`` auto-load.
FastMCPSettings.model_config["env_file"] = None

server = FastMCP("amesh", log_level="ERROR")


def _home() -> Path:
    return (
        Path(os.environ.get("AMESH_HOME") or "~/.config/amesh").expanduser().resolve()
    )


def _safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _dump(value: Any) -> str:
    return json.dumps(_safe(value), ensure_ascii=False, separators=(",", ":"))


def _enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _require_reply_enabled() -> None:
    if not _enabled("AMESH_MCP_ALLOW_REPLY", default=False):
        raise PermissionError(
            "platform replies are outside the Amesh MCP process capability"
        )


def _require_discovery_publish_enabled() -> None:
    if not _enabled("AMESH_MCP_ALLOW_DISCOVERY_PUBLISH", default=False):
        raise PermissionError(
            "discovery publishing is outside the Amesh MCP process capability"
        )


def _load(name: str) -> PlatformAdapter:
    name = validate_adapter_name(name)
    if name not in builtin_adapter_names():
        raise ValueError(f"unknown Amesh adapter: {name}")
    return load_adapter(_home(), name)


def _discovery() -> DiscoveryStore:
    return DiscoveryStore(discovery_database_path(_home()))


def _agents() -> AgentStore:
    return AgentStore(agent_database_path(_home()))


@server.tool(
    name="amesh_adapters",
    description="List installed Amesh adapters and their non-secret configuration.",
)
async def amesh_adapters() -> str:
    rows = []
    for name in builtin_adapter_names():
        adapter = load_adapter(_home(), name)
        try:
            rows.append(adapter.descriptor())
        finally:
            adapter.close()
    return _dump({"adapters": rows})


@server.tool(
    name="amesh_agents",
    description="List local agents and their declared scopes without exposing bearer tokens.",
)
async def amesh_agents() -> str:
    store = _agents()
    try:
        return _dump({"agents": [record.to_dict() for record in store.list()]})
    finally:
        store.close()


@server.tool(
    name="amesh_agent_register",
    description="Register a local agent; the returned bearer token is shown only once.",
)
async def amesh_agent_register(
    agent_id: str, name: str, scopes: list[str] | None = None
) -> str:
    store = _agents()
    try:
        return _dump(store.register(agent_id, name, scopes=tuple(scopes or ())))
    finally:
        store.close()


@server.tool(
    name="amesh_agent_grant",
    description="Add an explicit allow or deny grant for one agent, adapter, and action.",
)
async def amesh_agent_grant(
    agent_id: str, adapter: str, action: str, effect: str, reason: str = ""
) -> str:
    store = _agents()
    try:
        return _dump(store.grant(agent_id, adapter, action, effect, reason=reason))
    finally:
        store.close()


@server.tool(
    name="amesh_agent_revoke",
    description="Disable one local agent without deleting its audit history.",
)
async def amesh_agent_revoke(agent_id: str) -> str:
    store = _agents()
    try:
        return _dump({"agent_id": agent_id, "revoked": store.revoke(agent_id)})
    finally:
        store.close()


@server.tool(
    name="amesh_adapter_status",
    description="Return one Amesh adapter's ledger counts, runtime state, and policy health.",
)
async def amesh_adapter_status(adapter: str) -> str:
    instance = _load(adapter)
    try:
        return _dump(instance.status())
    finally:
        instance.close()


@server.tool(
    name="amesh_adapter_setup",
    description="Write one adapter's default configuration file for the Amesh home.",
)
async def amesh_adapter_setup(adapter: str) -> str:
    instance = _load(adapter)
    try:
        return _dump(instance.setup())
    finally:
        instance.close()


@server.tool(
    name="amesh_social_actor",
    description="Show one platform actor's evidence, evaluation, and effective permission view.",
)
async def amesh_social_actor(adapter: str, actor_key: str) -> str:
    actor_key = validate_actor_key(actor_key)
    instance = _load(adapter)
    try:
        return _dump(instance.actor(actor_key))
    finally:
        instance.close()


@server.tool(
    name="amesh_social_labels",
    description="Update one actor's operator labels in the adapter ledger.",
)
async def amesh_social_labels(
    adapter: str,
    actor_key: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    source: str = "operator",
) -> str:
    actor_key = validate_actor_key(actor_key)
    instance = _load(adapter)
    try:
        return _dump(
            instance.set_labels(
                actor_key,
                add=set(add or ()),
                remove=set(remove or ()),
                source=source,
            )
        )
    finally:
        instance.close()


@server.tool(
    name="amesh_social_project",
    description="Fold durable ledger events into the observer-local relationship book.",
)
async def amesh_social_project(adapter: str, limit: int = 1000) -> str:
    instance = _load(adapter)
    try:
        return _dump(instance.project(limit=limit))
    finally:
        instance.close()


@server.tool(
    name="amesh_social_relation",
    description="Map one platform actor key to its observer-local relationship record.",
)
async def amesh_social_relation(adapter: str, actor_key: str) -> str:
    actor_key = validate_actor_key(actor_key)
    instance = _load(adapter)
    try:
        return _dump(instance.relation(actor_key))
    finally:
        instance.close()


@server.tool(
    name="amesh_social_poll",
    description="Run one single-shot ingest poll on an adapter.",
)
async def amesh_social_poll(adapter: str) -> str:
    instance = _load(adapter)
    try:
        return _dump(instance.poll_once())
    finally:
        instance.close()


@server.tool(
    name="amesh_social_inject",
    description="Drop one message into a local spool adapter (loopback).",
)
async def amesh_social_inject(
    adapter: str,
    author: str,
    text: str,
    channel: str = "",
    bot: bool = False,
) -> str:
    instance = _load(adapter)
    try:
        return _dump(instance.inject(author, text, channel=channel, bot=bot))
    finally:
        instance.close()


@server.tool(
    name="amesh_social_signals",
    description="List emitted signals in the outbound sink for one adapter.",
)
async def amesh_social_signals(adapter: str, limit: int = 1000) -> str:
    adapter = validate_adapter_name(adapter)
    if adapter not in builtin_adapter_names():
        raise ValueError(f"unknown Amesh adapter: {adapter}")
    sink = DirectorySignalSink(amesh_outbound_dir(_home()))
    signals = sink.list(platform=adapter, limit=limit)
    return _dump({"platform": adapter, "count": len(signals), "signals": signals})


@server.tool(
    name="amesh_discovery_profile",
    description="Read the observer-local discovery profile used by the matcher.",
)
async def amesh_discovery_profile(profile_id: str = "") -> str:
    store = _discovery()
    try:
        return _dump({"profile": store.profile(profile_id)})
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_profile_set",
    description="Create or update the observer-local discovery profile.",
)
async def amesh_discovery_profile_set(
    profile_id: str,
    topics: list[str] | None = None,
    capabilities: list[str] | None = None,
    languages: list[str] | None = None,
    tenant: str = "",
    enabled: bool = True,
) -> str:
    store = _discovery()
    try:
        return _dump(
            store.set_profile(
                profile_id,
                topics=topics or (),
                capabilities=capabilities or (),
                languages=languages or (),
                tenant=tenant,
                enabled=enabled,
            )
        )
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_subscribe",
    description="Create or update an observer-local discovery subscription.",
)
async def amesh_discovery_subscribe(
    subscription_id: str,
    profile_id: str,
    intents: list[str] | None = None,
    topics: list[str] | None = None,
    capabilities: list[str] | None = None,
    languages: list[str] | None = None,
    min_score: int = 1,
    max_age_seconds: int = 7 * 86_400,
    enabled: bool = True,
) -> str:
    store = _discovery()
    try:
        return _dump(
            store.add_subscription(
                subscription_id,
                profile_id=profile_id,
                intents=intents or (),
                topics=topics or (),
                capabilities=capabilities or (),
                languages=languages or (),
                min_score=min_score,
                max_age_seconds=max_age_seconds,
                enabled=enabled,
            )
        )
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_status",
    description="Return local discovery profile, feed and feedback counts.",
)
async def amesh_discovery_status() -> str:
    store = _discovery()
    try:
        return _dump(store.status())
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_subscriptions",
    description="List observer-local discovery subscriptions.",
)
async def amesh_discovery_subscriptions() -> str:
    store = _discovery()
    try:
        return _dump({"subscriptions": store.subscriptions()})
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_feed",
    description="Read a durable, cursor-based discovery feed page.",
)
async def amesh_discovery_feed(
    subscription_id: str,
    after: int = 0,
    limit: int = 50,
) -> str:
    store = _discovery()
    try:
        return _dump(store.feed(subscription_id, after=after, limit=limit))
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_feedback",
    description="Record immutable local feedback for one discovery signal.",
)
async def amesh_discovery_feedback(
    subscription_id: str,
    signal_id: str,
    verdict: str,
    note: str = "",
) -> str:
    store = _discovery()
    try:
        return _dump(
            store.add_feedback(
                subscription_id,
                signal_id,
                verdict,
                note=note,
            )
        )
    finally:
        store.close()


@server.tool(
    name="amesh_discovery_publish",
    description=(
        "Publish a public-safe discovery signal to the local Amesh outbox. "
        "Disabled unless AMESH_MCP_ALLOW_DISCOVERY_PUBLISH=1."
    ),
)
async def amesh_discovery_publish(
    intent: str,
    summary: str,
    topics: list[str] | None = None,
    capabilities: list[str] | None = None,
    languages: list[str] | None = None,
    visibility: str = "public",
    tenant: str = "",
    ttl_seconds: int = 7 * 86_400,
    destination: str = "",
) -> str:
    _require_discovery_publish_enabled()
    now = int(time.time() * 1000)
    signal = build_discovery_signal(
        published_ms=now,
        expires_ms=now + int(ttl_seconds) * 1000,
        intent=intent,
        summary=summary,
        topics=topics or (),
        capabilities=capabilities or (),
        languages=languages or (),
        visibility=visibility,
        tenant=tenant,
        provenance={
            "source": "amesh-mcp",
            "adapter": "mcp",
            "revision": "manual",
        },
    )
    sink = DirectorySignalSink(amesh_outbound_dir(_home()))
    signal_id = sink.emit(signal)
    return _dump({"signal_id": signal_id, "destination": destination, "signal": signal})


@server.tool(
    name="amesh_social_reply",
    description=(
        "Send one operator reply to a platform event after the adapter's "
        "evidence, threshold, and permission gates pass. Disabled unless "
        "AMESH_MCP_ALLOW_REPLY=1."
    ),
)
async def amesh_social_reply(
    adapter: str,
    event_key: str,
    content: str,
    agent_id: str = "operator",
    agent_token: str = "",
) -> str:
    _require_reply_enabled()
    instance = _load(adapter)
    try:
        instance.require_agent(agent_id, "reply", token=agent_token)
        return _dump(instance.reply(event_key, content))
    finally:
        instance.close()


@server.tool(
    name="amesh_permit_add",
    description="Add one operator permission rule refining the adapter evidence thresholds.",
)
async def amesh_permit_add(
    adapter: str,
    actor_key: str,
    action: str,
    effect: str,
    reason: str = "",
) -> str:
    adapter = validate_adapter_name(adapter)
    if adapter not in builtin_adapter_names():
        raise ValueError(f"unknown Amesh adapter: {adapter}")
    actor_key = validate_actor_key(actor_key, wildcard=True)
    action = validate_action(action, wildcard=True)
    effect = validate_effect(effect)
    store = PermissionStore(amesh_database_path(_home()))
    try:
        return _dump(
            store.add_rule(adapter, actor_key, action, effect, reason=reason).to_dict()
        )
    finally:
        store.close()


@server.tool(
    name="amesh_permit_list",
    description="List operator permission rules for one adapter (or all).",
)
async def amesh_permit_list(adapter: str = "") -> str:
    adapter = validate_adapter_name(adapter) if adapter else ""
    store = PermissionStore(amesh_database_path(_home()))
    try:
        return _dump(
            {"rules": [rule.to_dict() for rule in store.rules(adapter=adapter)]}
        )
    finally:
        store.close()


@server.tool(
    name="amesh_permit_revoke",
    description="Remove one operator permission rule.",
)
async def amesh_permit_revoke(adapter: str, rule_id: str) -> str:
    adapter = validate_adapter_name(adapter)
    store = PermissionStore(amesh_database_path(_home()))
    try:
        return _dump({"rule_id": rule_id, "removed": store.remove_rule(rule_id)})
    finally:
        store.close()


@server.tool(
    name="amesh_permit_decisions",
    description="Show the permission decision audit for one adapter.",
)
async def amesh_permit_decisions(adapter: str = "", limit: int = 100) -> str:
    adapter = validate_adapter_name(adapter) if adapter else ""
    store = PermissionStore(amesh_database_path(_home()))
    try:
        return _dump({"decisions": store.decisions(adapter=adapter, limit=limit)})
    finally:
        store.close()


@server.tool(
    name="amesh_relations",
    description="List the observer-local relationship records in the Amesh home.",
)
async def amesh_relations() -> str:
    hub = RelationshipHub(_home())
    return _dump({"relationships": hub.list_records()})


@server.tool(
    name="amesh_relations_circle",
    description="Set one relationship circle explicitly with an evidence reference.",
)
async def amesh_relations_circle(
    subject: str,
    circle: str,
    confidence: int,
    evidence_ref: str,
    labels: list[str] | None = None,
) -> str:
    hub = RelationshipHub(_home())
    return _dump(
        hub.set_circle(
            subject,
            circle,
            confidence=confidence,
            evidence_ref=evidence_ref,
            labels=tuple(labels or ()),
        )
    )


def run_amesh_mcp() -> None:
    server.run(transport="stdio")
