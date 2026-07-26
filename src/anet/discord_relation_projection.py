from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .actors import platform_actor_id
from .identity import PeerCard
from .relations import (
    ActorObservation,
    ActorProof,
    InteractionEvidence,
    RelationshipBook,
)
from .social import validate_discord_signal


@dataclass(frozen=True)
class DiscordActorProjection:
    actor_id: str
    subject_ref: str
    proof_scope: str
    recorded: bool
    circle: str


class DiscordRelationshipProjector:
    """Translate durable Discord evidence into observer-local Actor facts.

    This Adapter accepts an already-pseudonymized platform Actor key. It never
    receives or persists Discord account, guild, channel, username, or message
    content identifiers in the relationship model.
    """

    def __init__(self, book: RelationshipBook) -> None:
        self.book = book

    def project_local_event(
        self,
        event: Mapping[str, Any],
    ) -> DiscordActorProjection:
        return self._project(
            namespace_actor_id=self.book.own_actor_id,
            actor_key=str(event["actor_key"]),
            created_ms=int(event["created_ms"]),
            evidence_ref=f"discord-event:{event['event_key']}",
            labels=tuple(str(item) for item in event.get("event_labels", ())),
            proof_type="discord.rest.v10",
            proof_scope="platform-observed",
        )

    def project_signal(
        self,
        bridge: PeerCard,
        signal: Mapping[str, Any],
    ) -> DiscordActorProjection:
        bridge.verify()
        normalized = validate_discord_signal(dict(signal))
        if self.book.primary_subject(bridge.node_id) is None:
            raise ValueError("Discord signal bridge Actor is not observed")
        return self._project(
            namespace_actor_id=bridge.node_id,
            actor_key=normalized["actor_key"],
            created_ms=normalized["created_ms"],
            evidence_ref=(
                f"discord-signal:{normalized['source_event_id']}"
            ),
            labels=tuple(normalized["labels"]),
            proof_type="discord.signal.v1",
            proof_scope="bridge-attested",
        )

    def _project(
        self,
        *,
        namespace_actor_id: str,
        actor_key: str,
        created_ms: int,
        evidence_ref: str,
        labels: tuple[str, ...],
        proof_type: str,
        proof_scope: str,
    ) -> DiscordActorProjection:
        actor_id = platform_actor_id(
            "discord",
            namespace_actor_id=namespace_actor_id,
            platform_actor_key=actor_key,
        )
        subject = self.book.observe_typed_actor(
            ActorObservation(
                actor_id=actor_id,
                actor_kind="account.discord",
                actor_label=f"Discord account · {actor_id[-6:]}",
                proof=ActorProof(
                    proof_type=proof_type,
                    scope=proof_scope,
                    issuer_actor_id=namespace_actor_id,
                    evidence_ref=evidence_ref,
                    observed_ms=created_ms,
                ),
            ),
            subject_confidence=50,
            now=created_ms,
        )
        facets = {"message"}
        if "content:attachment" in labels:
            facets.add("artifact")
        interaction = InteractionEvidence.create(
            actor_id=actor_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=facets,
            context="social.discord",
            outcome="received",
            evidence_ref=evidence_ref,
            occurred_ms=created_ms,
        )
        recorded = self.book.record_interaction(interaction)
        relationship = self.book.relationship(subject.subject_ref)
        if relationship is None:
            raise RuntimeError("Discord Actor Subject has no relationship estimate")
        if (
            relationship.circle == "public"
            and {"interaction:mention", "interaction:reply"}.intersection(labels)
        ):
            relationship = self.book.set_circle(
                subject.subject_ref,
                "known",
                confidence=25,
                evidence_ref=evidence_ref,
                labels=("interaction:directed", "platform:discord"),
                now=created_ms,
            )
        return DiscordActorProjection(
            actor_id=actor_id,
            subject_ref=subject.subject_ref,
            proof_scope=proof_scope,
            recorded=recorded,
            circle=relationship.circle,
        )
