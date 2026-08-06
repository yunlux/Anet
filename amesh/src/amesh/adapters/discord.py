from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from anet.actors import platform_actor_id
from anet.discord_social import (
    DISCORD_SIGNAL_KIND,
    DiscordSocialBridge,
    DiscordSocialConfig,
    DiscordSocialStore,
    discord_social_config_path,
    discord_social_database_path,
    discord_social_key_path,
)
from anet.discord_relation_projection import DiscordRelationshipProjector

from ..adapter import PlatformAdapter
from ..model import validate_actor_key, validate_event_key
from ..relations import RelationshipHub


class DiscordAdapter(PlatformAdapter):
    """First Amesh adapter: wraps the existing anet.discord_social bridge.

    Ingestion, reply gating, and the private SQLite ledger stay in
    ``anet.discord_social``. Amesh overlays operator permission rules, a
    permission-gated routing callback, and relationship projection. Read-only
    management commands never require a live bot token; only replies and the
    polling/run loops open the REST client.
    """

    name = "discord"

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self._bridge: DiscordSocialBridge | None = None
        self._store_handle: DiscordSocialStore | None = None
        self._hub: RelationshipHub | None = None

    @property
    def configured(self) -> bool:
        return discord_social_config_path(self.home).exists()

    def _config(self) -> DiscordSocialConfig:
        if not self.configured:
            raise ValueError("discord adapter is not configured in this home")
        return DiscordSocialConfig.load(self.home)

    def _open_store(self) -> DiscordSocialStore | None:
        path = discord_social_database_path(self.home)
        if not path.exists():
            return None
        if self._store_handle is None:
            self._store_handle = DiscordSocialStore(
                path,
                discord_social_key_path(self.home),
            )
        return self._store_handle

    def _load_bridge(self) -> DiscordSocialBridge:
        if self._bridge is not None:
            return self._bridge
        if not self.configured:
            raise ValueError("discord adapter is not configured in this home")
        self._bridge = DiscordSocialBridge.from_home(self.home)
        return self._bridge

    def descriptor(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "name": self.name,
                "configured": False,
                "enabled": False,
                "summary": "no discord-social.json configured",
            }
        config = self._config()
        return {
            "name": self.name,
            "configured": True,
            "enabled": config.enabled,
            "guild_id": config.guild_id,
            "channel_count": len(config.channel_ids),
            "destination_node_id": config.destination_node_id,
            "content_mode": config.content_mode,
            "token_env": config.token_env,
            "poll_interval_seconds": config.poll_interval_seconds,
            "signal_ttl_seconds": config.signal_ttl_seconds,
            "policy": config.policy.to_dict(),
        }

    def status(self) -> dict[str, Any]:
        if not self.configured:
            return {"name": self.name, "configured": False}
        config = self._config()
        counts = {
            "actors": 0,
            "events": 0,
            "routed": 0,
            "replied": 0,
        }
        runtime = {
            "runtime_state": "never_run",
            "last_error_category": "",
            "consecutive_failures": 0,
            "next_retry_ms": 0,
        }
        store = self._open_store()
        if store is not None:
            try:
                counts.update(store.status())
                runtime.update(store.runtime_status())
            finally:
                pass
        return {
            "name": self.name,
            "configured": True,
            "enabled": config.enabled,
            **runtime,
            **counts,
            "permission_rules": len(self.permission_rules()),
        }

    def actor(self, actor_key: str) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        config = self._config()
        store = self._open_store()
        if store is None:
            raise ValueError("discord ledger has not ingested any events")
        stats = store.actor_stats(actor_key)
        if stats is None:
            raise ValueError("unknown Discord social actor")
        evaluation = config.policy.evaluate(stats, set(stats["labels"]))
        gated, reasons = self.permitted_actions(
            actor_key,
            list(evaluation["allowed_actions"]),
        )
        return {
            **stats,
            "evaluation": evaluation,
            "permission": {
                "effective_allowed_actions": gated,
                "reasons": reasons,
                "rules": self.permission_rules(actor_key),
            },
        }

    def set_labels(
        self,
        actor_key: str,
        *,
        add: set[str] | frozenset[str],
        remove: set[str] | frozenset[str],
        source: str = "operator",
    ) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        self._config()
        store = self._open_store()
        if store is None:
            raise ValueError("discord ledger has not ingested any events")
        return store.update_labels(
            actor_key,
            add=add,
            remove=remove,
            source=source,
        )

    def project(self, *, limit: int = 1000) -> dict[str, Any]:
        self._config()
        store = self._open_store()
        if store is None:
            raise ValueError("discord ledger has not ingested any events")
        hub = self._relationship_hub()
        projector = DiscordRelationshipProjector(hub.book)
        events = store.events(limit=int(limit))
        projections = [projector.project_local_event(event) for event in events]
        return {
            "events_examined": len(projections),
            "interactions_recorded": sum(1 for item in projections if item.recorded),
            "actors": sorted({item.actor_id for item in projections}),
            "note": (
                "Discord evidence created no Anet peer trust, capability, "
                "context trust, or authorization"
            ),
        }

    def project_event(self, event: Mapping[str, Any]) -> Any:
        hub = self._relationship_hub()
        projector = DiscordRelationshipProjector(hub.book)
        return projector.project_local_event(event)

    def _relationship_hub(self) -> RelationshipHub:
        if self._hub is None:
            self._hub = RelationshipHub(self.home)
        return self._hub

    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        event_key = validate_event_key(event_key)
        store = self._open_store()
        if store is None:
            raise ValueError("discord ledger has not ingested any events")
        event = store.event(event_key)
        if event is None:
            raise ValueError("unknown social event")
        if self.permission_denies(event["actor_key"], "reply"):
            self.record_permission_decision(
                event["actor_key"],
                "reply",
                "deny",
                event_key=event_key,
            )
            raise PermissionError(
                f"reply blocked by a permission rule for {event['actor_key'][:8]}…"
            )
        return self._load_bridge().reply(event_key, content)

    def relation(self, actor_key: str) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        hub = self._relationship_hub()
        actor_id = platform_actor_id(
            "discord",
            namespace_actor_id=hub.identity.node_id,
            platform_actor_key=actor_key,
        )
        subject = hub.book.primary_subject(actor_id)
        if subject is None:
            return {
                "observed": False,
                "actor_id": actor_id,
                "platform": self.name,
            }
        estimate = hub.book.relationship(subject.subject_ref)
        return {
            "observed": True,
            "actor_id": actor_id,
            "platform": self.name,
            "subject_ref": subject.subject_ref,
            "relationship": estimate.to_dict() if estimate is not None else None,
        }

    def _gated_queue_signal(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None,
    ) -> Callable[[str, str, dict[str, Any]], str]:
        def _gate(destination_id: str, kind: str, body: dict[str, Any]) -> str:
            if kind != DISCORD_SIGNAL_KIND:
                raise ValueError("unsupported signal kind")
            actor_key = validate_actor_key(body["actor_key"])
            if self.permission_denies(actor_key, "surface"):
                self.record_permission_decision(
                    actor_key,
                    "surface",
                    "deny",
                    event_key=str(body["source_event_id"]),
                )
                return hashlib.sha256(
                    f"amesh:blocked:{body['source_event_id']}".encode("utf-8")
                ).hexdigest()[:32]
            if queue_signal is None:
                return ""
            return queue_signal(destination_id, kind, body)

        return _gate

    def poll_once(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None = None,
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        return self._load_bridge().poll_once(
            self._gated_queue_signal(queue_signal),
            project_event,
        )

    async def run(
        self,
        stop: Any,
        queue_signal: Callable[[str, str, dict[str, Any]], str],
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        await self._load_bridge().run(
            stop,
            self._gated_queue_signal(queue_signal),
            project_event,
        )

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.close()
        if self._store_handle is not None:
            self._store_handle.close()
        super().close()
