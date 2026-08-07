from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .discovery import (
    DiscoveryStore,
    build_discovery_signal,
    discovery_database_path,
)
from .agent import AGENT_ACTIONS, AgentStore, agent_database_path
from .connector import ConnectorAudit, EffectConnector, amesh_audit_path
from .route import RouteStore, route_database_path

from .adapter import adapter_names, load_adapter
from .model import (
    validate_action,
    validate_actor_key,
    validate_adapter_name,
    validate_effect,
)
from .policy import PermissionStore, amesh_database_path
from .relations import RelationshipHub
from .serve import amesh_outbound_dir, serve
from .signal import DirectorySignalSink


def default_home() -> Path:
    return Path(os.environ.get("AMESH_HOME", "~/.config/amesh")).expanduser()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _load_adapter(args: argparse.Namespace):
    return load_adapter(args.home, args.adapter)


def cmd_adapter_list(args: argparse.Namespace) -> int:
    adapters = []
    for name in adapter_names():
        try:
            adapter = load_adapter(args.home, name)
        except Exception as exc:
            adapters.append(
                {"name": name, "configured": False, "load_error": str(exc)}
            )
            continue
        try:
            adapters.append(adapter.descriptor())
        finally:
            adapter.close()
    _print_json({"adapters": adapters})
    return 0


def cmd_adapter_status(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.status())
    finally:
        adapter.close()
    return 0


def cmd_social_actor(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.actor(args.actor_key))
    finally:
        adapter.close()
    return 0


def cmd_social_label(args: argparse.Namespace) -> int:
    add = set(args.add) if args.add else set()
    remove = set(args.remove) if args.remove else set()
    if not add and not remove:
        raise ValueError("choose at least one --add or --remove label")
    adapter = _load_adapter(args)
    try:
        _print_json(
            adapter.set_labels(
                args.actor_key,
                add=add,
                remove=remove,
                source=args.source,
            )
        )
    finally:
        adapter.close()
    return 0


def cmd_social_project(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.project(limit=args.limit))
    finally:
        adapter.close()
    return 0


def cmd_social_relation(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.relation(args.actor_key))
    finally:
        adapter.close()
    return 0


def cmd_social_reply(args: argparse.Namespace) -> int:
    selected = int(args.text is not None) + int(bool(args.stdin))
    if selected != 1:
        raise ValueError("choose exactly one of --text or --stdin")
    content = args.text if args.text is not None else sys.stdin.read()
    adapter = _load_adapter(args)
    try:
        adapter.require_agent(
            args.agent_id,
            "reply",
            token=os.environ.get(args.agent_token_env, "")
            if args.agent_id != "operator"
            else "",
        )
        _print_json(adapter.reply(args.event_key, content))
    finally:
        adapter.close()
    return 0


def _agent_store(home: Path) -> AgentStore:
    return AgentStore(agent_database_path(home))


def cmd_agent_register(args: argparse.Namespace) -> int:
    store = _agent_store(args.home)
    try:
        _print_json(store.register(args.agent_id, args.name, scopes=tuple(args.scope)))
    finally:
        store.close()
    return 0


def cmd_agent_list(args: argparse.Namespace) -> int:
    store = _agent_store(args.home)
    try:
        _print_json({"agents": [record.to_dict() for record in store.list()]})
    finally:
        store.close()
    return 0


def cmd_agent_revoke(args: argparse.Namespace) -> int:
    store = _agent_store(args.home)
    try:
        _print_json({"agent_id": args.agent_id, "revoked": store.revoke(args.agent_id)})
    finally:
        store.close()
    return 0


def cmd_agent_grant(args: argparse.Namespace) -> int:
    store = _agent_store(args.home)
    try:
        _print_json(
            store.grant(
                args.agent_id,
                args.adapter,
                args.action,
                args.effect,
                reason=args.reason,
            )
        )
    finally:
        store.close()
    return 0


def cmd_social_poll(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.poll_once())
    finally:
        adapter.close()
    return 0


def cmd_social_inject(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(
            adapter.inject(
                args.author,
                args.text,
                channel=args.channel,
                bot=args.bot,
            )
        )
    finally:
        adapter.close()
    return 0


def cmd_adapter_setup(args: argparse.Namespace) -> int:
    adapter = _load_adapter(args)
    try:
        _print_json(adapter.setup())
    finally:
        adapter.close()
    return 0


def cmd_social_signals(args: argparse.Namespace) -> int:
    adapter = _require_adapter(args.adapter)
    sink = DirectorySignalSink(amesh_outbound_dir(args.home))
    signals = sink.list(platform=adapter, limit=args.limit)
    _print_json(
        {
            "platform": adapter,
            "count": len(signals),
            "signals": signals,
        }
    )
    return 0


def _discovery_store(home: Path) -> DiscoveryStore:
    return DiscoveryStore(discovery_database_path(home))


def cmd_discovery_profile(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json(
            store.set_profile(
                args.profile_id,
                topics=args.topic,
                capabilities=args.capability,
                languages=args.language,
                tenant=args.tenant,
                enabled=not args.disabled,
            )
        )
    finally:
        store.close()
    return 0


def cmd_discovery_subscribe(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json(
            store.add_subscription(
                args.subscription_id,
                profile_id=args.profile_id,
                intents=args.intent,
                topics=args.topic,
                capabilities=args.capability,
                languages=args.language,
                min_score=args.min_score,
                max_age_seconds=args.max_age,
                enabled=not args.disabled,
            )
        )
    finally:
        store.close()
    return 0


def cmd_discovery_subscriptions(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json({"subscriptions": store.subscriptions()})
    finally:
        store.close()
    return 0


def cmd_discovery_feed(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json(
            store.feed(
                args.subscription_id,
                after=args.after,
                limit=args.limit,
            )
        )
    finally:
        store.close()
    return 0


def cmd_discovery_feedback(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json(
            store.add_feedback(
                args.subscription_id,
                args.signal_id,
                args.verdict,
                note=args.note,
            )
        )
    finally:
        store.close()
    return 0


def cmd_discovery_status(args: argparse.Namespace) -> int:
    store = _discovery_store(args.home)
    try:
        _print_json(store.status())
    finally:
        store.close()
    return 0


def cmd_discovery_ingest(args: argparse.Namespace) -> int:
    value = json.loads(args.file.read_text(encoding="utf-8"))
    store = _discovery_store(args.home)
    try:
        _print_json(store.ingest(value, source_id=args.source))
    finally:
        store.close()
    return 0


def cmd_discovery_publish(args: argparse.Namespace) -> int:
    import time

    published_ms = int(time.time() * 1000)
    signal = build_discovery_signal(
        published_ms=published_ms,
        expires_ms=published_ms + args.ttl * 1000,
        intent=args.intent,
        summary=args.summary,
        topics=args.topic,
        capabilities=args.capability,
        languages=args.language,
        visibility=args.visibility,
        tenant=args.tenant,
        provenance={
            "source": args.source,
            "adapter": args.adapter_name,
            "revision": args.revision,
        },
    )
    sink = DirectorySignalSink(amesh_outbound_dir(args.home))
    signal_id = sink.emit(signal)
    _print_json(
        {
            "signal": signal,
            "signal_id": signal_id,
            "queued": True,
            "destination": args.destination,
            "outbox": str(amesh_outbound_dir(args.home)),
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    stop = asyncio.Event()

    def _terminate(_signum: int, _frame: Any) -> None:
        try:
            asyncio.get_running_loop().call_soon_threadsafe(stop.set)
        except RuntimeError:
            stop.set()

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, AttributeError):
        pass
    names = tuple(args.adapter) if args.adapter else tuple(adapter_names())
    try:
        result = asyncio.run(serve(args.home, names=names, stop=stop))
        _print_json(result)
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_mcp_server(args: argparse.Namespace) -> int:
    from .mcp_server import run_amesh_mcp

    run_amesh_mcp()
    return 0


def cmd_connector_serve(args: argparse.Namespace) -> int:
    connector = EffectConnector(
        args.home,
        host=args.host,
        port=args.port,
    ).start()
    print(
        f"amesh connector listening on {connector.host}:{connector.port}",
        file=sys.stderr,
    )
    try:
        connector.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_connector_audit(args: argparse.Namespace) -> int:
    store = ConnectorAudit(amesh_audit_path(args.home))
    try:
        _print_json({"audit": store.recent(limit=args.limit)})
    finally:
        store.close()
    return 0


def _route_store(home: Path) -> RouteStore:
    return RouteStore(route_database_path(home))


def cmd_route_status(args: argparse.Namespace) -> int:
    store = _route_store(args.home)
    try:
        _print_json(store.status())
    finally:
        store.close()
    return 0


def cmd_route_list(args: argparse.Namespace) -> int:
    store = _route_store(args.home)
    try:
        _print_json(
            {
                "routes": [
                    {
                        key: route[key]
                        for key in (
                            "route_id",
                            "destination",
                            "adapter",
                            "kind",
                            "state",
                            "attempts",
                            "next_retry_ms",
                            "expires_ms",
                            "created_ms",
                            "updated_ms",
                            "last_error",
                        )
                    }
                    for route in store.list(state=args.state, limit=args.limit)
                ]
            }
        )
    finally:
        store.close()
    return 0


def cmd_route_retry(args: argparse.Namespace) -> int:
    store = _route_store(args.home)
    try:
        _print_json(store.retry(args.route_id))
    finally:
        store.close()
    return 0


def cmd_route_flush(args: argparse.Namespace) -> int:
    from .serve import amesh_outbound_dir
    from .signal import DirectorySignalSink

    store = _route_store(args.home)
    sink = DirectorySignalSink(amesh_outbound_dir(args.home))
    try:
        _print_json(
            {
                "delivery": store.deliver_due(
                    lambda signal: sink.emit(dict(signal)),
                    limit=args.limit,
                ),
                "status": store.status(),
            }
        )
    finally:
        store.close()
    return 0


def cmd_route_policy(args: argparse.Namespace) -> int:
    store = _route_store(args.home)
    try:
        allowed = str(args.effect).strip().lower() == "allow"
        _print_json(store.set_policy(args.destination, args.adapter, allowed))
    finally:
        store.close()
    return 0


def cmd_route_policy_list(args: argparse.Namespace) -> int:
    store = _route_store(args.home)
    try:
        _print_json({"rules": store.policy_rules()})
    finally:
        store.close()
    return 0


def _require_adapter(name: str) -> str:
    name = validate_adapter_name(name)
    if name not in adapter_names():
        raise ValueError(f"unknown Amesh adapter: {name}")
    return name


def cmd_permit_add(args: argparse.Namespace) -> int:
    adapter = _require_adapter(args.adapter)
    actor_key = validate_actor_key(args.actor_key, wildcard=True)
    action = validate_action(args.action, wildcard=True)
    effect = validate_effect(args.effect)
    store = PermissionStore(amesh_database_path(args.home))
    try:
        rule = store.add_rule(
            adapter,
            actor_key,
            action,
            effect,
            reason=args.reason,
        )
        _print_json(rule.to_dict())
    finally:
        store.close()
    return 0


def cmd_permit_list(args: argparse.Namespace) -> int:
    adapter = args.adapter or ""
    if adapter:
        _require_adapter(adapter)
    store = PermissionStore(amesh_database_path(args.home))
    try:
        _print_json(
            {"rules": [rule.to_dict() for rule in store.rules(adapter=adapter)]}
        )
    finally:
        store.close()
    return 0


def cmd_permit_revoke(args: argparse.Namespace) -> int:
    _require_adapter(args.adapter)
    store = PermissionStore(amesh_database_path(args.home))
    try:
        removed = store.remove_rule(args.rule_id)
        _print_json({"rule_id": args.rule_id, "removed": removed})
    finally:
        store.close()
    return 0


def cmd_permit_decisions(args: argparse.Namespace) -> int:
    adapter = args.adapter or ""
    if adapter:
        _require_adapter(adapter)
    store = PermissionStore(amesh_database_path(args.home))
    try:
        _print_json(
            {
                "decisions": store.decisions(
                    adapter=adapter,
                    limit=args.limit,
                )
            }
        )
    finally:
        store.close()
    return 0


def cmd_relations_list(args: argparse.Namespace) -> int:
    hub = RelationshipHub(args.home)
    _print_json({"relationships": hub.list_records()})
    return 0


def cmd_relations_observe(args: argparse.Namespace) -> int:
    hub = RelationshipHub(args.home)
    _print_json(
        hub.observe_actor(
            args.actor,
            kind=args.kind,
            label=args.label or "",
            evidence=args.evidence or "",
            confidence=args.confidence,
        )
    )
    return 0


def cmd_relations_circle(args: argparse.Namespace) -> int:
    hub = RelationshipHub(args.home)
    _print_json(
        hub.set_circle(
            args.subject,
            args.circle,
            confidence=args.confidence,
            evidence_ref=args.evidence_ref,
            labels=tuple(args.label) if args.label else (),
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amesh",
        description="Independent social-security middleware for agents and platforms",
    )
    parser.add_argument("--version", action="version", version="Amesh 0.1.0")
    parser.add_argument(
        "--home",
        type=Path,
        default=default_home(),
        help="Amesh private state directory (default: $AMESH_HOME or ~/.config/amesh)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    adapter_list = sub.add_parser("adapter", help="inspect installed adapters")
    adapter_sub = adapter_list.add_subparsers(dest="adapter_command", required=True)
    adapter_list_cmd = adapter_sub.add_parser("list", help="list built-in adapters")
    adapter_list_cmd.set_defaults(func=cmd_adapter_list)
    adapter_status_cmd = adapter_sub.add_parser(
        "status", help="show one adapter's runtime status"
    )
    adapter_status_cmd.add_argument("adapter")
    adapter_status_cmd.set_defaults(func=cmd_adapter_status)

    adapter_setup_cmd = adapter_sub.add_parser(
        "setup", help="write an adapter's default configuration"
    )
    adapter_setup_cmd.add_argument("adapter")
    adapter_setup_cmd.set_defaults(func=cmd_adapter_setup)

    social = sub.add_parser("social", help="manage one adapter's social evidence")
    social_sub = social.add_subparsers(dest="social_command", required=True)

    actor_cmd = social_sub.add_parser("actor", help="show one actor's evaluation")
    actor_cmd.add_argument("adapter")
    actor_cmd.add_argument("actor_key")
    actor_cmd.set_defaults(func=cmd_social_actor)

    label_cmd = social_sub.add_parser("label", help="update operator labels")
    label_cmd.add_argument("adapter")
    label_cmd.add_argument("actor_key")
    label_cmd.add_argument("--add", action="append", default=[])
    label_cmd.add_argument("--remove", action="append", default=[])
    label_cmd.add_argument("--source", default="operator")
    label_cmd.set_defaults(func=cmd_social_label)

    project_cmd = social_sub.add_parser(
        "project", help="project ledger events into relations"
    )
    project_cmd.add_argument("adapter")
    project_cmd.add_argument("--limit", type=int, default=1000)
    project_cmd.set_defaults(func=cmd_social_project)

    relation_cmd = social_sub.add_parser(
        "relation", help="map one platform actor to its relationship record"
    )
    relation_cmd.add_argument("adapter")
    relation_cmd.add_argument("actor_key")
    relation_cmd.set_defaults(func=cmd_social_relation)

    reply_cmd = social_sub.add_parser("reply", help="send one operator reply")
    reply_cmd.add_argument("adapter")
    reply_cmd.add_argument("event_key")
    reply_cmd.add_argument("--text")
    reply_cmd.add_argument("--stdin", action="store_true")
    reply_cmd.add_argument("--agent-id", default="operator")
    reply_cmd.add_argument("--agent-token-env", default="AMESH_AGENT_TOKEN")
    reply_cmd.set_defaults(func=cmd_social_reply)

    poll_cmd = social_sub.add_parser("poll", help="run one single-shot ingest poll")
    poll_cmd.add_argument("adapter")
    poll_cmd.set_defaults(func=cmd_social_poll)

    inject_cmd = social_sub.add_parser(
        "inject", help="drop one message into a local spool adapter"
    )
    inject_cmd.add_argument("adapter")
    inject_cmd.add_argument("author")
    inject_cmd.add_argument("--text", required=True)
    inject_cmd.add_argument("--channel", default="")
    inject_cmd.add_argument("--bot", action="store_true")
    inject_cmd.set_defaults(func=cmd_social_inject)

    signals_cmd = social_sub.add_parser(
        "signals", help="list emitted signals in the outbound sink"
    )
    signals_cmd.add_argument("adapter")
    signals_cmd.add_argument("--limit", type=int, default=1000)
    signals_cmd.set_defaults(func=cmd_social_signals)

    discovery = sub.add_parser(
        "discovery",
        help="manage Amesh discovery profiles and feeds",
    )
    discovery_sub = discovery.add_subparsers(dest="discovery_command", required=True)

    profile_cmd = discovery_sub.add_parser(
        "profile", help="create or update one local discovery profile"
    )
    profile_cmd.add_argument("profile_id")
    profile_cmd.add_argument("--topic", action="append", default=[])
    profile_cmd.add_argument("--capability", action="append", default=[])
    profile_cmd.add_argument("--language", action="append", default=[])
    profile_cmd.add_argument("--tenant", default="")
    profile_cmd.add_argument("--disabled", action="store_true")
    profile_cmd.set_defaults(func=cmd_discovery_profile)

    subscribe_cmd = discovery_sub.add_parser(
        "subscribe", help="create or update one local signal subscription"
    )
    subscribe_cmd.add_argument("subscription_id")
    subscribe_cmd.add_argument("--profile-id", required=True)
    subscribe_cmd.add_argument("--intent", action="append", default=[])
    subscribe_cmd.add_argument("--topic", action="append", default=[])
    subscribe_cmd.add_argument("--capability", action="append", default=[])
    subscribe_cmd.add_argument("--language", action="append", default=[])
    subscribe_cmd.add_argument("--min-score", type=int, default=1)
    subscribe_cmd.add_argument("--max-age", type=int, default=7 * 86_400)
    subscribe_cmd.add_argument("--disabled", action="store_true")
    subscribe_cmd.set_defaults(func=cmd_discovery_subscribe)

    subscriptions_cmd = discovery_sub.add_parser(
        "subscriptions", help="list local discovery subscriptions"
    )
    subscriptions_cmd.set_defaults(func=cmd_discovery_subscriptions)

    feed_cmd = discovery_sub.add_parser(
        "feed", help="read a matched feed page using a durable cursor"
    )
    feed_cmd.add_argument("subscription_id")
    feed_cmd.add_argument("--after", type=int, default=0)
    feed_cmd.add_argument("--limit", type=int, default=50)
    feed_cmd.set_defaults(func=cmd_discovery_feed)

    feedback_cmd = discovery_sub.add_parser(
        "feedback", help="record immutable local feedback for one feed item"
    )
    feedback_cmd.add_argument("subscription_id")
    feedback_cmd.add_argument("signal_id")
    feedback_cmd.add_argument("verdict", choices=("useful", "not_relevant", "spam"))
    feedback_cmd.add_argument("--note", default="")
    feedback_cmd.set_defaults(func=cmd_discovery_feedback)

    status_cmd = discovery_sub.add_parser(
        "status", help="show local discovery store counts"
    )
    status_cmd.set_defaults(func=cmd_discovery_status)

    ingest_cmd = discovery_sub.add_parser(
        "ingest", help="ingest a source-identified Amesh signal JSON fixture"
    )
    ingest_cmd.add_argument("--source", required=True)
    ingest_cmd.add_argument("--file", type=Path, required=True)
    ingest_cmd.set_defaults(func=cmd_discovery_ingest)

    publish_cmd = discovery_sub.add_parser(
        "publish", help="publish a public-safe discovery signal"
    )
    publish_cmd.add_argument(
        "--intent", required=True, choices=("know", "need", "offer", "capability")
    )
    publish_cmd.add_argument("--summary", required=True)
    publish_cmd.add_argument("--topic", action="append", default=[])
    publish_cmd.add_argument("--capability", action="append", default=[])
    publish_cmd.add_argument("--language", action="append", default=[])
    publish_cmd.add_argument(
        "--visibility", default="public", choices=("public", "tenant")
    )
    publish_cmd.add_argument("--tenant", default="")
    publish_cmd.add_argument("--source", default="operator")
    publish_cmd.add_argument("--adapter", dest="adapter_name", default="amesh-cli")
    publish_cmd.add_argument("--revision", default="manual")
    publish_cmd.add_argument("--ttl", type=int, default=7 * 86_400)
    publish_cmd.add_argument("--destination", default="")
    publish_cmd.set_defaults(func=cmd_discovery_publish)

    agent = sub.add_parser(
        "agent", help="register agents and manage their explicit adapter grants"
    )
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_register = agent_sub.add_parser("register", help="register one local agent")
    agent_register.add_argument("agent_id")
    agent_register.add_argument("name")
    agent_register.add_argument(
        "--scope", action="append", choices=AGENT_ACTIONS, default=[]
    )
    agent_register.set_defaults(func=cmd_agent_register)
    agent_list = agent_sub.add_parser("list", help="list local agents without tokens")
    agent_list.set_defaults(func=cmd_agent_list)
    agent_revoke = agent_sub.add_parser("revoke", help="disable one local agent")
    agent_revoke.add_argument("agent_id")
    agent_revoke.set_defaults(func=cmd_agent_revoke)
    agent_grant = agent_sub.add_parser(
        "grant", help="grant one adapter action to an agent"
    )
    agent_grant.add_argument("agent_id")
    agent_grant.add_argument("adapter")
    agent_grant.add_argument("action", choices=AGENT_ACTIONS)
    agent_grant.add_argument("effect", choices=("allow", "deny"))
    agent_grant.add_argument("--reason", default="")
    agent_grant.set_defaults(func=cmd_agent_grant)

    permit = sub.add_parser(
        "permit", help="manage operator permission rules on adapters"
    )
    permit_sub = permit.add_subparsers(dest="permit_command", required=True)

    permit_add_cmd = permit_sub.add_parser("add", help="add one rule")
    permit_add_cmd.add_argument("adapter")
    permit_add_cmd.add_argument("actor_key")
    permit_add_cmd.add_argument("action")
    permit_add_cmd.add_argument("effect")
    permit_add_cmd.add_argument("--reason", default="")
    permit_add_cmd.set_defaults(func=cmd_permit_add)

    permit_list_cmd = permit_sub.add_parser("list", help="list rules")
    permit_list_cmd.add_argument("adapter", nargs="?", default="")
    permit_list_cmd.set_defaults(func=cmd_permit_list)

    permit_revoke_cmd = permit_sub.add_parser("revoke", help="remove one rule")
    permit_revoke_cmd.add_argument("adapter")
    permit_revoke_cmd.add_argument("rule_id")
    permit_revoke_cmd.set_defaults(func=cmd_permit_revoke)

    permit_decisions_cmd = permit_sub.add_parser(
        "decisions", help="show the permission decision audit"
    )
    permit_decisions_cmd.add_argument("adapter", nargs="?", default="")
    permit_decisions_cmd.add_argument("--limit", type=int, default=100)
    permit_decisions_cmd.set_defaults(func=cmd_permit_decisions)

    relations = sub.add_parser(
        "relations", help="manage the observer-local relationship model"
    )
    relations_sub = relations.add_subparsers(dest="relations_command", required=True)

    relations_list_cmd = relations_sub.add_parser(
        "list", help="list relationship records"
    )
    relations_list_cmd.set_defaults(func=cmd_relations_list)

    relations_observe_cmd = relations_sub.add_parser(
        "observe", help="observe one typed external Actor"
    )
    relations_observe_cmd.add_argument("actor")
    relations_observe_cmd.add_argument("--kind", required=True)
    relations_observe_cmd.add_argument("--label", default="")
    relations_observe_cmd.add_argument("--evidence", default="")
    relations_observe_cmd.add_argument("--confidence", type=int, default=50)
    relations_observe_cmd.set_defaults(func=cmd_relations_observe)

    relations_circle_cmd = relations_sub.add_parser(
        "circle", help="set one relationship circle explicitly"
    )
    relations_circle_cmd.add_argument("subject")
    relations_circle_cmd.add_argument("circle")
    relations_circle_cmd.add_argument("--confidence", type=int, required=True)
    relations_circle_cmd.add_argument("--evidence-ref", required=True)
    relations_circle_cmd.add_argument("--label", action="append", default=[])
    relations_circle_cmd.set_defaults(func=cmd_relations_circle)

    serve_cmd = sub.add_parser(
        "serve", help="host the configured adapters' background loops"
    )
    serve_cmd.add_argument(
        "--adapter",
        action="append",
        default=[],
        help="host only these adapters (default: all configured)",
    )
    serve_cmd.set_defaults(func=cmd_serve)

    mcp_cmd = sub.add_parser("mcp-server", help="run the Amesh stdio MCP server")
    mcp_cmd.set_defaults(func=cmd_mcp_server)

    connector = sub.add_parser(
        "connector", help="run the token-authenticated local effect connector"
    )
    connector_sub = connector.add_subparsers(dest="connector_command", required=True)

    connector_serve = connector_sub.add_parser(
        "serve", help="serve /v1/effects on loopback with bearer-token auth"
    )
    connector_serve.add_argument("--adapter", default="loopback")
    connector_serve.add_argument("--host", default="127.0.0.1")
    connector_serve.add_argument("--port", type=int, default=8765)
    connector_serve.set_defaults(func=cmd_connector_serve)

    connector_audit = connector_sub.add_parser(
        "audit", help="read the append-only connector request audit"
    )
    connector_audit.add_argument("--limit", type=int, default=100)
    connector_audit.set_defaults(func=cmd_connector_audit)

    route = sub.add_parser(
        "route", help="manage the durable route/outbox state machine"
    )
    route_sub = route.add_subparsers(dest="route_command", required=True)

    route_status = route_sub.add_parser("status", help="show route counts by state")
    route_status.set_defaults(func=cmd_route_status)

    route_list = route_sub.add_parser("list", help="list routes")
    route_list.add_argument("--state", default="")
    route_list.add_argument("--limit", type=int, default=100)
    route_list.set_defaults(func=cmd_route_list)

    route_retry = route_sub.add_parser("retry", help="reset one failed route to retry")
    route_retry.add_argument("route_id")
    route_retry.set_defaults(func=cmd_route_retry)

    route_flush = route_sub.add_parser(
        "flush", help="attempt delivery of due routes to the outbound sink"
    )
    route_flush.add_argument("--limit", type=int, default=100)
    route_flush.set_defaults(func=cmd_route_flush)

    route_policy = route_sub.add_parser(
        "policy", help="allow or deny one destination/adapter route"
    )
    route_policy.add_argument("destination")
    route_policy.add_argument("adapter")
    route_policy.add_argument("effect", choices=("allow", "deny"))
    route_policy.set_defaults(func=cmd_route_policy)

    route_policy_list = route_sub.add_parser(
        "policy-list", help="list destination/adapter policy rules"
    )
    route_policy_list.set_defaults(func=cmd_route_policy_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"amesh: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
