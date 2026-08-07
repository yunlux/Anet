from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from ..adapter import PlatformAdapter
from ..identity import platform_actor_id
from ..model import validate_actor_key, validate_event_key
from ..relations import (
    ActorObservation,
    ActorProof,
    InteractionEvidence,
    RelationshipHub,
)
from .discord_backend import (
    DISCORD_SIGNAL_KIND,
    DiscordBridge,
    DiscordConfig,
    DiscordStore,
    discord_config_path,
    discord_database_path,
    discord_key_path,
)


class DiscordAdapter(PlatformAdapter):
    """Standalone Discord social adapter for the Amesh middleware.

    The adapter owns Discord API access, pseudonymous event storage and
    outbound reply reservations. It never creates agent grants or trusts a
    platform account as an identity for another system.
    """

    name = "discord"

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self._bridge: DiscordBridge | None = None
        self._store_handle: DiscordStore | None = None
        self._hub: RelationshipHub | None = None

    @property
    def configured(self) -> bool:
        return discord_config_path(self.home).exists()

    def _config(self) -> DiscordConfig:
        if not self.configured:
            raise ValueError("Discord adapter is not configured in this home")
        return DiscordConfig.load(self.home)

    def _open_store(self) -> DiscordStore | None:
        if not discord_database_path(self.home).exists():
            return None
        if self._store_handle is None:
            self._store_handle = DiscordStore(
                discord_database_path(self.home),
                discord_key_path(self.home),
            )
        return self._store_handle

    def _load_bridge(self) -> DiscordBridge:
        if self._bridge is None:
            self._bridge = DiscordBridge.from_home(self.home)
            self._store_handle = self._bridge.store
        return self._bridge

    def setup(self) -> dict[str, Any]:
        DiscordConfig(guild_id="0", channel_ids=("0",)).save(self.home)
        return self.descriptor()

    def descriptor(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "name": self.name,
                "configured": False,
                "enabled": False,
                "summary": "no discord.json configured",
            }
        config = self._config()
        return {
            "name": self.name,
            "configured": True,
            "enabled": config.enabled,
            "guild_id": config.guild_id,
            "channel_count": len(config.channel_ids),
            "destination_id": config.destination_id,
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
        counts: dict[str, Any] = {"actors": 0, "events": 0, "routed": 0, "replied": 0}
        runtime: dict[str, Any] = {
            "runtime_state": "never_run",
            "last_error_category": "",
            "consecutive_failures": 0,
            "next_retry_ms": 0,
        }
        store = self._open_store()
        if store is not None:
            counts.update(store.status())
            runtime.update(store.runtime_status())
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
            raise ValueError("Discord ledger has not ingested any events")
        stats = store.actor_stats(actor_key)
        if stats is None:
            raise ValueError("unknown Discord social actor")
        evaluation = config.policy.evaluate(stats, set(stats["labels"]))
        gated, reasons = self.permitted_actions(
            actor_key, list(evaluation["allowed_actions"])
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
        self, actor_key: str, *, add, remove, source: str = "operator"
    ) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        self._config()
        store = self._open_store()
        if store is None:
            raise ValueError("Discord ledger has not ingested any events")
        return store.update_labels(actor_key, add=add, remove=remove, source=source)

    def project(self, *, limit: int = 1000) -> dict[str, Any]:
        self._config()
        store = self._open_store()
        if store is None:
            raise ValueError("Discord ledger has not ingested any events")
        projections = [self.project_event(event) for event in store.events(limit=limit)]
        return {
            "events_examined": len(projections),
            "interactions_recorded": sum(1 for item in projections if item["recorded"]),
            "actors": sorted({item["actor_id"] for item in projections}),
            "note": "Discord evidence created no agent grant or authorization",
        }

    def project_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        hub = self._relationship_hub()
        actor_id = platform_actor_id(
            "discord",
            namespace_actor_id=hub.identity.identity_id,
            platform_actor_key=str(event["actor_key"]),
        )
        evidence_ref = f"discord-event:{event['event_key']}"
        subject = hub.book.observe_typed_actor(
            ActorObservation(
                actor_id=actor_id,
                actor_kind="account.discord",
                actor_label=f"Discord account · {actor_id[-6:]}",
                proof=ActorProof(
                    proof_type="discord.rest.v1",
                    scope="platform-observed",
                    issuer_actor_id=hub.identity.identity_id,
                    evidence_ref=evidence_ref,
                    observed_ms=int(event["created_ms"]),
                ),
            ),
            subject_confidence=50,
            now=int(event["created_ms"]),
        )
        recorded = hub.book.record_interaction(
            InteractionEvidence.create(
                actor_id=actor_id,
                subject_ref=subject.subject_ref,
                direction="incoming",
                facets={"message"},
                context="social.discord",
                outcome="received",
                evidence_ref=evidence_ref,
                occurred_ms=int(event["created_ms"]),
            )
        )
        relationship = hub.book.relationship(subject.subject_ref)
        if relationship is None:
            raise RuntimeError("Discord relationship disappeared")
        if relationship.circle == "public" and {
            "interaction:mention",
            "interaction:reply",
        }.intersection(event.get("event_labels", ())):
            relationship = hub.book.set_circle(
                subject.subject_ref,
                "known",
                confidence=25,
                evidence_ref=evidence_ref,
                labels=("interaction:directed", "platform:discord"),
                now=int(event["created_ms"]),
            )
        return {
            "actor_id": actor_id,
            "subject_ref": subject.subject_ref,
            "recorded": recorded,
            "circle": relationship.circle,
        }

    def relation(self, actor_key: str) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        hub = self._relationship_hub()
        actor_id = platform_actor_id(
            "discord",
            namespace_actor_id=hub.identity.identity_id,
            platform_actor_key=actor_key,
        )
        subject = hub.book.primary_subject(actor_id)
        return (
            {"observed": False, "actor_id": actor_id, "platform": self.name}
            if subject is None
            else {
                "observed": True,
                "actor_id": actor_id,
                "platform": self.name,
                "subject_ref": subject.subject_ref,
                "relationship": hub.book.relationship(subject.subject_ref).to_dict(),
            }
        )

    def _relationship_hub(self) -> RelationshipHub:
        if self._hub is None:
            self._hub = RelationshipHub(self.home)
        return self._hub

    def _gated_queue_signal(
        self, queue_signal: Callable[[str, str, dict[str, Any]], str] | None
    ):
        def _gate(destination_id: str, kind: str, body: dict[str, Any]) -> str:
            if kind != DISCORD_SIGNAL_KIND:
                raise ValueError("unsupported signal kind")
            actor_key = validate_actor_key(body["actor_key"])
            if self.permission_denies(actor_key, "surface"):
                self.record_permission_decision(
                    actor_key,
                    "surface",
                    "deny",
                    event_key=str(body.get("source_event_id", "")),
                )
                return hashlib.sha256(
                    f"amesh:blocked:{body.get('source_event_id', '')}".encode("utf-8")
                ).hexdigest()[:32]
            if queue_signal is None:
                return ""
            return queue_signal(destination_id, kind, body)

        return _gate

    def poll_once(self, queue_signal=None, project_event=None) -> dict[str, Any]:
        return self._load_bridge().poll_once(
            self._gated_queue_signal(queue_signal), project_event or self.project_event
        )

    async def run(self, stop: Any, queue_signal, project_event=None) -> None:
        await self._load_bridge().run(
            stop,
            self._gated_queue_signal(queue_signal),
            project_event or self.project_event,
        )

    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        event_key = validate_event_key(event_key)
        store = self._open_store()
        if store is None:
            raise ValueError("Discord ledger has not ingested any events")
        event = store.event(event_key)
        if event is None:
            raise ValueError("unknown Discord social event")
        if self.permission_denies(event["actor_key"], "reply"):
            self.record_permission_decision(
                event["actor_key"], "reply", "deny", event_key=event_key
            )
            raise PermissionError(
                f"reply blocked by an Amesh permission rule for {event['actor_key'][:8]}…"
            )
        return self._load_bridge().reply(event_key, content)

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
            self._store_handle = None
        elif self._store_handle is not None:
            self._store_handle.close()
            self._store_handle = None
        super().close()


__all__ = ["DiscordAdapter", "DiscordConfig", "DiscordStore", "DISCORD_SIGNAL_KIND"]
