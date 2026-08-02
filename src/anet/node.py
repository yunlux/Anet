from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import ssl
import threading
import time
import re
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .actors import validate_actor_id
from .config import (
    AhubCarrierConfig,
    DirectDialerConfig,
    DirectoryCarrierConfig,
    NodeConfig,
    WebDAVCarrierConfig,
)
from .control_plane import ControlPlaneStore, ReachabilityRecord
from .companion_protocol import (
    APPROVAL_REQUEST_KIND,
    COMPANION_KINDS,
    validate_companion_endpoint_binding,
)
from .discord_social import (
    DiscordSocialBridge,
    DiscordSocialConfig,
    discord_social_config_path,
)
from .discord_relation_projection import DiscordRelationshipProjector
from .encoding import canonical_pack
from .identity import Identity, PeerCard
from .locator import usable_locators
from .packet import inspect_packet, now_ms, open_packet, seal_packet
from .peers import PeerBook
from .prekeys import (
    PreKeyBundle,
    generate_prekey_bundle,
    import_prekey_bundle,
    load_local_prekey_bundle,
)
from .relation_activity import RelationshipActivityFeed
from .relation_projection import RelationshipProjector
from .relationship_disclosures import (
    RELATIONSHIP_DISCLOSURE_KIND,
    RelationshipDisclosure,
    RelationshipDisclosureBook,
    validate_relationship_disclosure,
)
from .relationship_disclosure_schedules import (
    RelationshipDisclosureScheduleBook,
)
from .relationship_disclosure_recovery import (
    RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND,
    RelationshipDisclosureArchiveBook,
    RelationshipDisclosureGapNotice,
    RelationshipDisclosureGapNoticeBook,
    validate_relationship_disclosure_gap_notice,
)
from .reported_relationship_views import ReportedRelationshipViewProjector
from .relations import RelationshipBook
from .routing import AdaptiveRouter
from .scheduling import AdaptiveSchedule
from .social import DISCORD_SIGNAL_KIND, validate_discord_signal
from .store import PacketStore
from .transport import (
    classify_connection_error,
    client_tls_context,
    open_stdio_tls_connection,
    open_proxy_tls_connection,
    parse_tls_address,
    read_frame,
    server_tls_context,
    write_frame,
)

if TYPE_CHECKING:
    from .ahub_http import AhubHTTPClient

LOGGER = logging.getLogger("anet.node")
LINK_VERSION = 1
PREKEY_REQUEST_KIND = "network.prekey.request"
PREKEY_BUNDLE_KIND = "network.prekey.bundle"
PREKEY_CONTROL_KINDS = frozenset({PREKEY_REQUEST_KIND, PREKEY_BUNDLE_KIND})
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class AnetNode:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.identity = Identity.load(config.identity_path)
        self.peers = PeerBook(config.peers_path, own_node_id=self.identity.node_id)
        self.store = PacketStore(config.database_path)
        self.control = ControlPlaneStore(config.control_database_path)
        self._prekey_scope_warning = ""
        unscoped = self.store.unscoped_local_prekey_count()
        trusted_cards = self.peers.all()
        if unscoped and len(trusted_cards) == 1:
            result = self.store.scope_legacy_local_prekeys(trusted_cards[0].node_id)
            LOGGER.info(
                "scoped %d legacy one-time prekeys to sole peer %s",
                int(result["scoped"]),
                trusted_cards[0].node_id,
            )
        elif unscoped:
            self._prekey_scope_warning = (
                f"{unscoped} legacy prekeys are unscoped; exactly one pinned peer "
                "is required for automatic migration"
            )
            LOGGER.error(self._prekey_scope_warning)
        cert_path, key_path, fingerprint = self.identity.ensure_tls_material(config.home)
        self._tls_fingerprint = fingerprint
        self._server_context = server_tls_context(cert_path, key_path)
        self._client_context = client_tls_context()
        self._server: asyncio.AbstractServer | None = None
        self._started = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()
        self._peer_locks: dict[str, asyncio.Lock] = {}
        self.peer_state: dict[str, dict[str, Any]] = {}
        self._carrier_schedules: dict[str, AdaptiveSchedule] = {}
        self._carrier_seen_pending: dict[str, set[str]] = {}
        self._direct_schedules: dict[str, AdaptiveSchedule] = {}
        self._direct_seen_pending: dict[str, set[str]] = {}
        self._fallback_probe_tasks: dict[str, asyncio.Task[bool]] = {}
        self._fallback_schedules: dict[str, AdaptiveSchedule] = {}
        self._prekey_response_last_ms: dict[str, int] = {}
        self._control_session_id = secrets.token_hex(16)
        self._reachability_lock = threading.Lock()
        self._reachability_record: ReachabilityRecord | None = None
        self._discord_bridge: DiscordSocialBridge | None = None
        self._relationship_projector: RelationshipProjector | None = None
        self._relationship_disclosure_book: (
            RelationshipDisclosureBook | None
        ) = None
        self._relationship_gap_notice_book: (
            RelationshipDisclosureGapNoticeBook | None
        ) = None
        self._discord_relationship_projector: (
            DiscordRelationshipProjector | None
        ) = None
        self._loop_schedule = AdaptiveSchedule(
            config.sync_interval,
            config.sync_jitter,
            1.0,
        )
        self.router = AdaptiveRouter(self.store, config.routing)

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    @property
    def control_session_id(self) -> str:
        return self._control_session_id

    @property
    def local_card(self) -> PeerCard:
        return self.identity.card(
            addresses=self.config.effective_addresses(),
            capabilities=self.config.capabilities,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        try:
            relay_specs: list[tuple[AhubCarrierConfig, str]] = []
            relay_owners: set[tuple[str, str]] = set()
            for carrier in self.config.ahub_carriers:
                if not carrier.enabled or not carrier.live_relay_enabled:
                    continue
                for peer_id in carrier.peers:
                    ownership = (carrier.base_url, peer_id)
                    if ownership in relay_owners:
                        raise ValueError(
                            "duplicate live Relay owner for one Ahub/peer"
                        )
                    relay_owners.add(ownership)
                    relay_specs.append((carrier, peer_id))
            if relay_specs:
                from .carriers.ahub import current_node_descriptor

                current_node_descriptor(self)
            self.process_local_spool()
            self.maintain_prekeys()
            discord_config = discord_social_config_path(self.config.home)
            if discord_config.exists():
                social_config = DiscordSocialConfig.load(self.config.home)
                if social_config.enabled:
                    self._discord_bridge = DiscordSocialBridge.from_home(
                        self.config.home
                    )
                    destination = (
                        self._discord_bridge.config.destination_node_id
                    )
                    if destination:
                        self.peers.require(destination)
            if self.config.listen_enabled:
                self._server = await asyncio.start_server(
                    self._handle_connection,
                    host=self.config.listen_host,
                    port=self.config.listen_port,
                    ssl=self._server_context,
                )
            task = asyncio.create_task(self._sync_loop(), name=f"anet-sync-{self.node_id[:12]}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            if self._discord_bridge is not None:
                discord_task = asyncio.create_task(
                    self._discord_bridge.run(
                        self._stop,
                        self._queue_discord_signal,
                        self._project_discord_event,
                    ),
                    name=f"anet-discord-social-{self.node_id[:12]}",
                )
                self._tasks.add(discord_task)
                discord_task.add_done_callback(self._tasks.discard)
            for carrier, peer_id in relay_specs:
                relay_task = asyncio.create_task(
                    self._ahub_relay_listener_loop(
                        carrier,
                        peer_id,
                    ),
                    name=f"anet-relay-owner-{carrier.name}",
                )
                self._tasks.add(relay_task)
                relay_task.add_done_callback(self._tasks.discard)
            if self._server is not None:
                sockets = self._server.sockets or []
                LOGGER.info(
                    "Anet node %s listening on %s",
                    self.node_id,
                    [sock.getsockname() for sock in sockets],
                )
            else:
                LOGGER.info("Anet node %s running without an inbound listener", self.node_id)
        except Exception:
            self._started = False
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(
                    *list(self._tasks),
                    return_exceptions=True,
                )
            self._tasks.clear()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            if self._discord_bridge is not None:
                self._discord_bridge.close()
                self._discord_bridge = None
            raise

    async def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._started = False

    def close(self) -> None:
        if self._discord_bridge is not None:
            self._discord_bridge.close()
            self._discord_bridge = None
        self.control.close()
        self.store.close()

    async def run_forever(self) -> None:
        await self.start()
        await self._stop.wait()

    def request_stop(self) -> None:
        self._stop.set()

    def queue(
        self,
        destination_id: str,
        *,
        kind: str,
        body: Any,
        ttl_seconds: int = 86400,
        max_hops: int | None = None,
        causal: list[str] | tuple[str, ...] = (),
        codec: str = "msgpack",
        reply_to: str = "",
        qos: str = "normal",
    ) -> str:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind.startswith("network.prekey."):
            raise ValueError("prekey control kinds are reserved for the node runtime")
        if normalized_kind.startswith("companion."):
            if normalized_kind not in COMPANION_KINDS:
                raise ValueError("unsupported Companion kind")
            body = validate_companion_endpoint_binding(
                normalized_kind,
                body,
                sender_node_id=self.node_id,
                destination_node_id=destination_id,
            )
            if normalized_kind == APPROVAL_REQUEST_KIND:
                self.store.register_companion_approval_request(body)
        elif normalized_kind == DISCORD_SIGNAL_KIND:
            body = validate_discord_signal(body)
        elif normalized_kind == RELATIONSHIP_DISCLOSURE_KIND:
            body = validate_relationship_disclosure(
                body,
                sender_node_id=self.node_id,
                destination_node_id=destination_id,
            )
        elif normalized_kind == RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND:
            body = validate_relationship_disclosure_gap_notice(
                body,
                sender_node_id=self.node_id,
                destination_node_id=destination_id,
            )
        recipient = self.peers.require(destination_id)
        prekey_v2 = "one-time-prekeys-v2" in recipient.capabilities
        prekey_capable = prekey_v2 or "one-time-prekeys-v1" in recipient.capabilities
        reservation: dict[str, Any] | None = None
        if self.config.prekey_policy == "require" and not prekey_capable:
            raise RuntimeError(
                f"peer {destination_id} does not advertise one-time prekey support"
            )
        if self.config.prekey_policy != "disable" and prekey_capable:
            reservation = self.store.reserve_peer_prekey(
                destination_id,
                min_bundle_version=2 if prekey_v2 else 1,
            )
            if reservation is None and self.config.prekey_policy == "require":
                raise RuntimeError(
                    f"no unexpired one-time prekey is available for {destination_id}"
                )
        try:
            raw = seal_packet(
                self.identity,
                recipient,
                kind=kind,
                body=body,
                ttl_seconds=ttl_seconds,
                max_hops=max_hops or self.config.max_hops,
                causal=causal,
                codec=codec,
                reply_to=reply_to,
                padding_min=self.config.padding_min,
                qos=qos,
                recipient_prekey_public=(
                    bytes(reservation["public_key"]) if reservation else None
                ),
                recipient_prekey_id=(
                    str(reservation["prekey_id"]) if reservation else ""
                ),
                packet_version=3 if prekey_capable else 2,
            )
            info = inspect_packet(raw)
            if reservation is not None:
                self.store.bind_peer_prekey(
                    destination_id,
                    str(reservation["prekey_id"]),
                    str(reservation["reservation_id"]),
                    info.packet_id,
                )
            self.store.add_packet(raw, depth=0, origin="local")
            self._project_interaction(
                recipient,
                packet_id=info.packet_id,
                kind=kind,
                body=body,
                direction="outgoing",
                occurred_ms=info.created_ms,
            )
            return info.packet_id
        except BaseException:
            if reservation is not None:
                self.store.burn_peer_prekey(
                    destination_id,
                    str(reservation["prekey_id"]),
                    str(reservation["reservation_id"]),
                )
            raise

    def _queue_discord_signal(
        self,
        destination_id: str,
        kind: str,
        body: dict[str, Any],
    ) -> str:
        if kind != DISCORD_SIGNAL_KIND:
            raise ValueError("Discord social bridge attempted a reserved kind")
        return self.queue(
            destination_id,
            kind=kind,
            body=body,
            ttl_seconds=self._discord_bridge.config.signal_ttl_seconds
            if self._discord_bridge is not None
            else 7 * 86_400,
            qos="normal",
        )

    def dispatch_a2a_claim(
        self,
        claim: dict[str, Any],
        *,
        ttl_seconds: int = 86400,
        qos: str = "interactive",
        retry_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Seal one leased A2A intent and commit Packet + outbox atomically."""

        if not isinstance(claim, dict):
            raise ValueError("A2A dispatch claim must be an object")
        required = {
            "owner",
            "claim_token",
            "encryption_reservation_id",
            "destination_peer_id",
            "kind",
            "body",
        }
        if not required.issubset(claim):
            raise ValueError("incomplete A2A dispatch claim")
        owner = str(claim["owner"])
        claim_token = str(claim["claim_token"])
        reservation_id = str(claim["encryption_reservation_id"])
        destination_id = str(claim["destination_peer_id"])
        kind = str(claim["kind"]).strip().lower()
        if kind not in {"agent.task.request", "agent.task.cancel"}:
            raise ValueError(
                "A2A outbox may dispatch only Agent task requests or cancellations"
            )

        reservation: dict[str, Any] | None = None
        try:
            recipient = self.peers.require(destination_id)
            prekey_v2 = "one-time-prekeys-v2" in recipient.capabilities
            prekey_capable = (
                prekey_v2 or "one-time-prekeys-v1" in recipient.capabilities
            )
            if self.config.prekey_policy == "require" and not prekey_capable:
                raise RuntimeError(
                    f"peer {destination_id} does not advertise one-time prekey support"
                )
            if self.config.prekey_policy != "disable" and prekey_capable:
                reservation = self.store.reserve_peer_prekey(
                    destination_id,
                    reservation_id=reservation_id,
                    min_bundle_version=2 if prekey_v2 else 1,
                )
                if reservation is None and self.config.prekey_policy == "require":
                    raise RuntimeError(
                        f"no unexpired one-time prekey is available for {destination_id}"
                    )
            raw = seal_packet(
                self.identity,
                recipient,
                kind=kind,
                body=claim["body"],
                ttl_seconds=ttl_seconds,
                max_hops=self.config.max_hops,
                padding_min=self.config.padding_min,
                qos=qos,
                recipient_prekey_public=(
                    bytes(reservation["public_key"]) if reservation else None
                ),
                recipient_prekey_id=(
                    str(reservation["prekey_id"]) if reservation else ""
                ),
                packet_version=3 if prekey_capable else 2,
            )
            return self.store.commit_a2a_dispatch_packet(
                owner,
                claim_token,
                raw,
                prekey_id=(
                    str(reservation["prekey_id"]) if reservation else ""
                ),
                prekey_reservation_id=(
                    str(reservation["reservation_id"]) if reservation else ""
                ),
            )
        except BaseException as exc:
            try:
                self.store.retry_a2a_dispatch(
                    owner,
                    claim_token,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_seconds=retry_seconds,
                )
            except ValueError:
                pass
            raise

    def drain_a2a_dispatches(
        self,
        owner: str,
        *,
        limit: int = 16,
        lease_seconds: float = 60.0,
        retry_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Dispatch one bounded local outbox batch without exposing task bodies."""

        claims = self.store.claim_a2a_dispatches(
            owner,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        dispatched: list[str] = []
        failures: list[dict[str, str]] = []
        for claim in claims:
            try:
                result = self.dispatch_a2a_claim(
                    claim,
                    retry_seconds=retry_seconds,
                )
                dispatched.append(str(result["packet_id"]))
            except Exception as exc:
                failures.append(
                    {
                        "message_id": str(claim["message_id"]),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
        return {
            "claimed": len(claims),
            "dispatched": len(dispatched),
            "failed": len(failures),
            "packet_ids": dispatched,
            "failures": failures,
        }

    def _queue_prekey_control(
        self, destination_id: str, *, kind: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Queue a trusted runtime-only control object.

        It prefers a v2 one-time prekey, but may use a v3 static envelope when
        inventory is empty. The payload contains only signed public prekey
        material or a replenishment request, allowing recovery from zero stock
        without silently downgrading ordinary Agent messages.
        """
        if kind not in PREKEY_CONTROL_KINDS:
            raise ValueError("invalid prekey control kind")
        recipient = self.peers.require(destination_id)
        if "one-time-prekeys-v2" not in recipient.capabilities:
            raise ValueError("peer does not support peer-scoped prekeys")
        reservation = self.store.reserve_peer_prekey(
            destination_id, min_bundle_version=2
        )
        try:
            raw = seal_packet(
                self.identity,
                recipient,
                kind=kind,
                body=body,
                ttl_seconds=7 * 86400,
                max_hops=self.config.max_hops,
                codec="application/msgpack",
                padding_min=max(self.config.padding_min, 4096),
                qos="control",
                recipient_prekey_public=(
                    bytes(reservation["public_key"]) if reservation else None
                ),
                recipient_prekey_id=(
                    str(reservation["prekey_id"]) if reservation else ""
                ),
                packet_version=3,
            )
            info = inspect_packet(raw)
            if reservation is not None:
                self.store.bind_peer_prekey(
                    destination_id,
                    str(reservation["prekey_id"]),
                    str(reservation["reservation_id"]),
                    info.packet_id,
                )
            self.store.add_packet(raw, depth=0, origin="prekey-control")
            return {
                "packet_id": info.packet_id,
                "key_mode": info.key_mode,
                "prekey_id": info.prekey_id,
            }
        except BaseException:
            if reservation is not None:
                self.store.burn_peer_prekey(
                    destination_id,
                    str(reservation["prekey_id"]),
                    str(reservation["reservation_id"]),
                )
            raise

    def request_prekey_replenishment(
        self, destination_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        recipient = self.peers.require(destination_id)
        if "one-time-prekeys-v2" not in recipient.capabilities:
            return {
                "peer_id": destination_id,
                "requested": False,
                "reason": "peer does not advertise one-time-prekeys-v2",
            }
        inventory = self.store.peer_prekey_inventory(
            destination_id, min_bundle_version=2
        )
        if not force and inventory["available"] >= self.config.prekey_low_watermark:
            return {
                "peer_id": destination_id,
                "requested": False,
                "reason": "inventory is above the low watermark",
                **inventory,
            }
        current = now_ms()
        request_state = self.store.prekey_request_state(destination_id)
        same_generation = (
            request_state["known_generation"] == inventory["generation"]
        )
        interval_ms = int(self.config.prekey_request_interval * 1000)
        if (
            not force
            and same_generation
            and current - request_state["last_requested_ms"] < interval_ms
        ):
            return {
                "peer_id": destination_id,
                "requested": False,
                "reason": "request interval has not elapsed",
                **inventory,
            }
        request_id = hashlib.blake2s(
            canonical_pack(
                [
                    # Immutable deployed wire-domain label.
                    "ainet/prekey-request/v1",
                    self.node_id,
                    destination_id,
                    inventory["generation"],
                ]
            ),
            digest_size=16,
            person=b"anet-pkr",
        ).hexdigest()
        queued = self._queue_prekey_control(
            destination_id,
            kind=PREKEY_REQUEST_KIND,
            body={
                "v": 1,
                "request_id": request_id,
                "known_generation": inventory["generation"],
                "available": inventory["available"],
                "requested_count": self.config.prekey_batch_size,
            },
        )
        self.store.record_prekey_request(
            destination_id,
            known_generation=inventory["generation"],
            requested_ms=current,
        )
        return {
            "peer_id": destination_id,
            "requested": True,
            "request_id": request_id,
            "available": inventory["available"],
            "known_generation": inventory["generation"],
            **queued,
        }

    def maintain_prekeys(self) -> list[dict[str, Any]]:
        if not self.config.prekey_auto_enabled:
            return []
        results: list[dict[str, Any]] = []
        for card in self.peers.all():
            if "one-time-prekeys-v2" not in card.capabilities:
                continue
            try:
                result = self.request_prekey_replenishment(card.node_id)
                if result.get("requested"):
                    results.append(result)
            except Exception as exc:
                LOGGER.warning(
                    "automatic prekey request for %s failed: %s",
                    card.node_id,
                    exc,
                )
        return results

    @staticmethod
    def _prekey_request_body(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or int(body.get("v", 0)) != 1:
            raise ValueError("invalid prekey request")
        request_id = str(body.get("request_id", "")).strip().lower()
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid prekey request ID")
        known_generation = int(body.get("known_generation", -1))
        available = int(body.get("available", -1))
        requested_count = int(body.get("requested_count", 0))
        if known_generation < 0 or available < 0:
            raise ValueError("invalid prekey request inventory")
        if not 1 <= requested_count <= 1000:
            raise ValueError("invalid prekey request count")
        return {
            "request_id": request_id,
            "known_generation": known_generation,
            "available": available,
            "requested_count": requested_count,
        }

    def _handle_prekey_request(self, message: Any) -> dict[str, Any]:
        if self._prekey_scope_warning:
            raise RuntimeError(
                "cannot issue peer-scoped prekeys until legacy shared inventory "
                "is migrated or retired"
            )
        request = self._prekey_request_body(message.body)
        current = now_ms()
        bundle = load_local_prekey_bundle(
            self.identity,
            self.store,
            peer_id=message.sender_id,
        )
        latest_generation = bundle.generation if bundle is not None else 0
        if request["known_generation"] > latest_generation:
            raise ValueError(
                "prekey request claims a generation newer than the local issuer"
            )
        cache_key = f"{message.sender_id}:{request['request_id']}"
        last_response = self._prekey_response_last_ms.get(cache_key, 0)
        if current - last_response < 30_000:
            return {
                "peer_id": message.sender_id,
                "request_id": request["request_id"],
                "generation": latest_generation,
                "duplicate": True,
            }
        if (
            bundle is None
            or bundle.generation <= request["known_generation"]
            or bundle.expires_ms <= current
        ):
            bundle = generate_prekey_bundle(
                self.identity,
                self.store,
                peer_id=message.sender_id,
                count=self.config.prekey_batch_size,
                ttl_ms=int(self.config.prekey_ttl_days * 86400 * 1000),
            )
        queued = self._queue_prekey_control(
            message.sender_id,
            kind=PREKEY_BUNDLE_KIND,
            body={
                "v": 1,
                "request_id": request["request_id"],
                "bundle": bundle.to_dict(),
            },
        )
        self._prekey_response_last_ms[cache_key] = current
        if len(self._prekey_response_last_ms) > 1024:
            newest = sorted(
                self._prekey_response_last_ms.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:1024]
            self._prekey_response_last_ms = dict(newest)
        return {
            "peer_id": message.sender_id,
            "request_id": request["request_id"],
            "generation": bundle.generation,
            "count": len(bundle.keys),
            **queued,
        }

    def _handle_prekey_bundle(self, message: Any) -> dict[str, Any]:
        body = message.body
        if not isinstance(body, dict) or int(body.get("v", 0)) != 1:
            raise ValueError("invalid prekey bundle control object")
        request_id = str(body.get("request_id", "")).strip().lower()
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid prekey bundle request ID")
        bundle_value = body.get("bundle")
        if not isinstance(bundle_value, dict):
            raise ValueError("prekey bundle control object has no bundle")
        bundle = PreKeyBundle.from_dict(bundle_value)
        if bundle.version != 2:
            raise ValueError("in-band replenishment requires a v2 prekey bundle")
        card = self.peers.require(message.sender_id)
        if bundle.node_id != message.sender_id:
            raise ValueError("prekey bundle sender does not match message sender")
        result = import_prekey_bundle(
            bundle,
            card,
            self.store,
            recipient_node_id=self.node_id,
        )
        result["request_id"] = request_id
        return result

    async def sync_once(
        self,
        *,
        peer_ids: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        self.peers.reload()
        if not self.config.direct_enabled:
            return {
                "attempted": 0,
                "connected": 0,
                "errors": {},
                "peer_results": {},
                "disabled": True,
            }
        cards = [
            card
            for card in self.peers.all()
            if card.addresses and (peer_ids is None or card.node_id in peer_ids)
        ]
        if not cards:
            return {"attempted": 0, "connected": 0, "errors": {}, "peer_results": {}}
        results = await asyncio.gather(*(self._sync_peer(card) for card in cards), return_exceptions=True)
        connected = 0
        errors: dict[str, str] = {}
        peer_results: dict[str, dict[str, Any]] = {}
        for card, result in zip(cards, results, strict=True):
            if isinstance(result, Exception):
                errors[card.node_id] = str(result)
                peer_results[card.node_id] = {"connected": False, "error": str(result)}
            elif result:
                connected += 1
                peer_results[card.node_id] = {"connected": True, "error": ""}
            else:
                peer_results[card.node_id] = {"connected": False, "error": "busy"}
        return {
            "attempted": len(cards),
            "connected": connected,
            "errors": errors,
            "peer_results": peer_results,
        }

    async def _background_fallback_probe(self, card: PeerCard) -> bool:
        try:
            return await self._sync_peer(card)
        except Exception:
            return False

    def _schedule_fallback_probes(self, cards: list[PeerCard]) -> list[str]:
        current = time.monotonic()
        scheduled: list[str] = []
        for card in cards:
            schedule = self._fallback_schedules.setdefault(
                card.node_id,
                AdaptiveSchedule(
                    self.config.routing.fallback_probe_interval,
                    self.config.routing.fallback_probe_jitter,
                    self.config.routing.direct_idle_backoff_max,
                ),
            )
            existing = self._fallback_probe_tasks.get(card.node_id)
            if existing is not None and not existing.done():
                continue
            if existing is not None and existing.done():
                result = False
                with suppress(asyncio.CancelledError, Exception):
                    result = bool(existing.result())
                schedule.record(current, activity=result)
                self._fallback_probe_tasks.pop(card.node_id, None)
            if not schedule.due(current):
                continue
            task = asyncio.create_task(
                self._background_fallback_probe(card),
                name=f"anet-fallback-probe-{card.node_id[:12]}",
            )
            self._fallback_probe_tasks[card.node_id] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            scheduled.append(card.node_id)
        return scheduled

    async def probe(
        self,
        destination_id: str,
        *,
        timeout: float = 15.0,
        interval: float = 0.5,
        carrier_grace: float = 3.0,
        payload_bytes: int = 0,
        qos: str = "control",
    ) -> dict[str, Any]:
        timeout = max(0.5, min(float(timeout), 300.0))
        interval = max(0.2, min(float(interval), 10.0))
        carrier_grace = max(0.0, min(float(carrier_grace), 30.0))
        payload_bytes = max(0, min(int(payload_bytes), 4 * 1024 * 1024))
        started_monotonic = time.perf_counter()
        deadline = started_monotonic + timeout
        started_ms = now_ms()
        route_before = self.store.route(destination_id)
        packet_id = self.queue(
            destination_id,
            kind="network.probe",
            body={
                "nonce": os.urandom(16),
                "sent_ms": started_ms,
                "payload": os.urandom(payload_bytes),
            },
            ttl_seconds=max(30, int(timeout) + 30),
            qos=qos,
        )
        attempts = 0
        last_sync: dict[str, Any] = {}

        def success(receipt: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "packet_id": packet_id,
                "peer_id": destination_id,
                "qos": qos,
                "payload_bytes": payload_bytes,
                "elapsed_ms": round((time.perf_counter() - started_monotonic) * 1000.0, 3),
                "receipt": receipt,
                "delivery_paths": self.store.delivery_paths(packet_id),
                "route_before": route_before,
                "route_after": self.store.route(destination_id),
                "sync_attempts": attempts,
            }

        def failure() -> dict[str, Any]:
            return {
                "ok": False,
                "packet_id": packet_id,
                "peer_id": destination_id,
                "qos": qos,
                "payload_bytes": payload_bytes,
                "elapsed_ms": round((time.perf_counter() - started_monotonic) * 1000.0, 3),
                "delivery_paths": self.store.delivery_paths(packet_id),
                "route_before": route_before,
                "route_after": self.store.route(destination_id),
                "sync_attempts": attempts,
                "last_direct_errors": last_sync.get("direct", {}).get("errors", {}),
                "last_carrier_errors": last_sync.get("carrier_errors", {}),
            }

        while True:
            receipt = self.store.receipt(packet_id)
            if receipt is not None:
                return success(receipt)
            if time.perf_counter() >= deadline:
                return failure()
            attempts += 1
            last_sync = await self.adaptive_sync_once(force_carriers=True)
            receipt = self.store.receipt(packet_id)
            if receipt is not None:
                return success(receipt)

            delivery_paths = self.store.delivery_paths(packet_id)
            carrier_in_flight = any(
                item["path_id"] not in {"direct", "unknown"}
                and item["state"] in {"sent", "acked"}
                for item in delivery_paths
            )
            if carrier_in_flight and carrier_grace:
                in_flight_names = {
                    str(item["path_id"]).split(":", 1)[1]
                    for item in delivery_paths
                    if ":" in str(item["path_id"])
                    and item["path_id"] not in {"direct", "unknown"}
                    and item["state"] in {"sent", "acked"}
                }
                grace_deadline = min(deadline, time.perf_counter() + carrier_grace)
                while time.perf_counter() < grace_deadline:
                    grace_sync = await self.adaptive_sync_once(
                        force_carriers=True,
                        skip_direct=True,
                        carrier_names=in_flight_names,
                    )
                    last_sync["carrier_errors"] = grace_sync.get("carrier_errors", {})
                    receipt = self.store.receipt(packet_id)
                    if receipt is not None:
                        return success(receipt)
                    await asyncio.sleep(
                        min(0.1, max(0.0, grace_deadline - time.perf_counter()))
                    )

            if time.perf_counter() >= deadline:
                return failure()
            await asyncio.sleep(min(interval, max(0.0, deadline - time.perf_counter())))

    def _carriers_for_peer(self, peer_id: str):
        return tuple(
            carrier
            for carrier in (
                *self.config.directory_carriers,
                *self.config.webdav_carriers,
                *self.config.ahub_carriers,
            )
            if carrier.enabled and (not carrier.peers or peer_id in carrier.peers)
        )

    def _direct_schedule(self, peer_id: str) -> AdaptiveSchedule:
        return self._direct_schedules.setdefault(
            peer_id,
            AdaptiveSchedule(
                self.config.routing.direct_idle_probe_interval,
                self.config.routing.direct_probe_jitter,
                self.config.routing.direct_idle_backoff_max,
            ),
        )

    def _carrier_schedule(
        self,
        carrier: AhubCarrierConfig | DirectoryCarrierConfig | WebDAVCarrierConfig,
    ) -> AdaptiveSchedule:
        return self._carrier_schedules.setdefault(
            carrier.name,
            AdaptiveSchedule(
                carrier.interval,
                carrier.jitter,
                carrier.idle_backoff_max,
            ),
        )

    def _pending_ids(
        self,
        peer_id: str,
        *,
        qos_allow: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    ) -> set[str]:
        return {
            str(item["packet_id"])
            for item in self.store.pending_for_peer(
                peer_id,
                limit=self.config.max_batch,
                retry_after_ms=0,
                qos_allow=qos_allow,
            )
        }

    async def _sync_configured_ahub_relay(
        self,
        card: PeerCard,
        carrier: AhubCarrierConfig,
    ) -> dict[str, Any]:
        from .ahub_http import AhubHTTPClient, AhubHTTPError
        from .carriers.ahub import current_node_descriptor

        client = AhubHTTPClient(
            carrier.base_url,
            self.identity,
            timeout_seconds=carrier.timeout,
            allow_insecure_http=carrier.allow_insecure_http,
        )
        descriptor = current_node_descriptor(self)
        await asyncio.to_thread(client.publish_descriptor, descriptor)
        started = time.perf_counter()
        deadline = time.monotonic() + carrier.timeout
        while True:
            reservation = await asyncio.to_thread(
                client.relay_reservation,
                card.node_id,
            )
            try:
                await self.sync_ahub_relay_once(
                    card.node_id,
                    client,
                    reservation.reservation_id,
                    path_id=carrier.relay_path_id,
                )
                break
            except AhubHTTPError as exc:
                remaining = deadline - time.monotonic()
                if exc.category != "transport" or remaining <= 0:
                    raise
                await asyncio.sleep(
                    min(
                        carrier.relay_listener_retry_seconds,
                        remaining,
                    )
                )
        return {
            "connected": True,
            "path_id": carrier.relay_path_id,
            "latency_ms": round(
                (time.perf_counter() - started) * 1000,
                3,
            ),
        }

    async def adaptive_sync_once(
        self,
        *,
        force_carriers: bool = True,
        skip_direct: bool = False,
        carrier_names: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Probe direct paths, update routing, then poll/push configured carriers."""
        from .carriers.ahub import sync_ahub_once
        from .carriers.directory import sync_directory_once
        from .carriers.webdav import sync_webdav_once

        self.peers.reload()
        cards = self.peers.all()
        monotonic_now = time.monotonic()
        foreground_ids: set[str] = set()
        fallback_cards: list[PeerCard] = []
        direct_pending: dict[str, set[str]] = {}
        for card in cards:
            route = self.store.route(card.node_id)
            selected = str(route["selected_path"]) if route else ""
            if selected not in {"", "direct", "none"}:
                if self.config.direct_enabled and card.addresses:
                    fallback_cards.append(card)
            elif self.config.direct_enabled and card.addresses:
                pending_ids = self._pending_ids(card.node_id)
                previously_seen = self._direct_seen_pending.get(card.node_id, set())
                newly_pending = bool(pending_ids - previously_seen)
                self._direct_seen_pending[card.node_id] = pending_ids
                direct_pending[card.node_id] = pending_ids
                schedule = self._direct_schedule(card.node_id)
                if newly_pending:
                    schedule.reset_backoff()
                if schedule.due(
                    monotonic_now,
                    force=force_carriers,
                    newly_pending=newly_pending,
                ):
                    foreground_ids.add(card.node_id)
        if skip_direct:
            direct = {
                "attempted": 0,
                "connected": 0,
                "errors": {},
                "skipped": True,
                "background_probes": [],
            }
        else:
            direct = await self.sync_once(peer_ids=foreground_ids)
            direct["background_probes"] = self._schedule_fallback_probes(fallback_cards)
            completed_at = time.monotonic()
            for peer_id in foreground_ids:
                pending = direct_pending.get(peer_id, set())
                peer_result = direct.get("peer_results", {}).get(peer_id, {})
                connected = bool(peer_result.get("connected"))
                self._direct_schedule(peer_id).record(
                    completed_at,
                    activity=bool(pending and connected),
                    base_interval=(
                        self.config.routing.direct_retry_interval
                        if pending or not connected
                        else self.config.routing.direct_idle_probe_interval
                    ),
                )
        decisions = {
            card.node_id: self.router.decide(
                card.node_id,
                has_direct=bool(self.config.direct_enabled and card.addresses),
                carriers=self._carriers_for_peer(card.node_id),
            )
            for card in cards
        }
        carrier_results: list[dict[str, Any]] = []
        carrier_errors: dict[str, str] = {}
        carrier_schedules: list[dict[str, Any]] = []

        for carrier in (
            *self.config.directory_carriers,
            *self.config.webdav_carriers,
            *self.config.ahub_carriers,
        ):
            if not carrier.enabled:
                continue
            if carrier_names is not None and carrier.name not in carrier_names:
                continue
            pending_ids: set[str] = set()
            for card in cards:
                if carrier.peers and card.node_id not in carrier.peers:
                    continue
                qos_allow = decisions[card.node_id].push_qos.get(carrier.name, frozenset())
                if qos_allow:
                    pending_ids.update(self._pending_ids(card.node_id, qos_allow=qos_allow))
            previously_seen = self._carrier_seen_pending.get(carrier.name, set())
            newly_pending = bool(pending_ids - previously_seen)
            self._carrier_seen_pending[carrier.name] = pending_ids
            schedule = self._carrier_schedule(carrier)
            if newly_pending:
                schedule.reset_backoff()
            if not schedule.due(
                time.monotonic(),
                force=force_carriers,
                newly_pending=newly_pending,
            ):
                carrier_schedules.append(
                    {"name": carrier.name, "ran": False, **schedule.snapshot(time.monotonic())}
                )
                continue
            result_start = len(carrier_results)
            for card in cards:
                if carrier.peers and card.node_id not in carrier.peers:
                    continue
                qos_allow = decisions[card.node_id].push_qos.get(carrier.name, frozenset())
                started = time.perf_counter()
                path_id = carrier.path_id
                try:
                    common: dict[str, Any] = {
                        "peer_ids": [card.node_id],
                        "push_peer_ids": [card.node_id] if qos_allow else [],
                        "qos_allow": qos_allow,
                        "limit": self.config.max_batch,
                        "retry_after_ms": int(carrier.retry_seconds * 1000),
                    }
                    if isinstance(carrier, DirectoryCarrierConfig):
                        result = await asyncio.to_thread(
                            sync_directory_once,
                            self,
                            carrier.path,
                            path_id=path_id,
                            **common,
                        )
                    elif isinstance(carrier, WebDAVCarrierConfig):
                        result = await asyncio.to_thread(
                            sync_webdav_once,
                            self,
                            carrier,
                            **common,
                        )
                    elif isinstance(carrier, AhubCarrierConfig):
                        live_result: dict[str, Any] = {
                            "enabled": carrier.live_relay_enabled,
                            "attempted": False,
                            "connected": False,
                            "path_id": carrier.relay_path_id,
                            "error_category": "",
                        }
                        if (
                            carrier.live_relay_enabled
                            and carrier.mode != "receive-only"
                            and bool(qos_allow)
                        ):
                            live_result["attempted"] = True
                            relay_started = time.perf_counter()
                            try:
                                relay_result = (
                                    await self._sync_configured_ahub_relay(
                                        card,
                                        carrier,
                                    )
                                )
                                live_result.update(relay_result)
                                self.store.record_path_result(
                                    card.node_id,
                                    carrier.relay_path_id,
                                    success=True,
                                    latency_ms=float(
                                        relay_result["latency_ms"]
                                    ),
                                )
                            except Exception as exc:
                                from .ahub_http import AhubHTTPError

                                relay_latency_ms = (
                                    time.perf_counter() - relay_started
                                ) * 1000
                                error_category = (
                                    exc.category
                                    if isinstance(exc, AhubHTTPError)
                                    else type(exc).__name__
                                )
                                live_result["error_category"] = error_category
                                self.store.record_path_result(
                                    card.node_id,
                                    carrier.relay_path_id,
                                    success=False,
                                    latency_ms=relay_latency_ms,
                                    error=error_category,
                                )
                        result = await asyncio.to_thread(
                            sync_ahub_once,
                            self,
                            carrier,
                            path_id=path_id,
                            **common,
                        )
                        result["live_relay"] = live_result
                    else:  # pragma: no cover - config parser rejects unknown carrier types
                        raise TypeError("unsupported adaptive carrier config")
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    self.store.record_path_result(
                        card.node_id,
                        path_id,
                        success=True,
                        latency_ms=latency_ms,
                    )
                    result.update(
                        {
                            "name": carrier.name,
                            "peer_id": card.node_id,
                            "mode": carrier.mode,
                            "selected": decisions[card.node_id].selected_path == path_id,
                            "push_qos": sorted(qos_allow),
                            "latency_ms": round(latency_ms, 3),
                        }
                    )
                    carrier_results.append(result)
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    self.store.record_path_result(
                        card.node_id,
                        path_id,
                        success=False,
                        latency_ms=latency_ms,
                        error=str(exc),
                    )
                    carrier_errors[f"{carrier.name}:{card.node_id}"] = str(exc)

            round_results = carrier_results[result_start:]
            activity = any(
                int(item.get("pulled_packets", 0))
                + int(item.get("pulled_acks", 0))
                + int(item.get("pushed_packets", 0))
                > 0
                for item in round_results
            )
            schedule.record(time.monotonic(), activity=activity)
            snapshot = {"name": carrier.name, "ran": True, **schedule.snapshot(time.monotonic())}
            carrier_schedules.append(snapshot)
            for item in round_results:
                item["schedule"] = snapshot

        return {
            "direct": direct,
            "routes": [
                {
                    "peer_id": decision.peer_id,
                    "selected_path": decision.selected_path,
                    "reason": decision.reason,
                    "changed": decision.changed,
                    "push_qos": {key: sorted(value) for key, value in decision.push_qos.items()},
                }
                for decision in decisions.values()
            ],
            "carriers": carrier_results,
            "carrier_errors": carrier_errors,
            "schedules": {
                "direct": [
                    {"peer_id": peer_id, **schedule.snapshot(time.monotonic())}
                    for peer_id, schedule in sorted(self._direct_schedules.items())
                ],
                "fallback": [
                    {"peer_id": peer_id, **schedule.snapshot(time.monotonic())}
                    for peer_id, schedule in sorted(self._fallback_schedules.items())
                ],
                "carriers": carrier_schedules,
            },
        }

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                self.store.purge()
            with suppress(Exception):
                self.maintain_prekeys()
            try:
                self.run_relationship_disclosure_schedules_once()
            except Exception:
                LOGGER.exception(
                    "background relationship disclosure scheduling failed"
                )
            try:
                await self.adaptive_sync_once(force_carriers=False)
            except Exception:
                LOGGER.exception("background sync failed")
            try:
                delay = self._loop_schedule.record(time.monotonic(), activity=True)
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def run_relationship_disclosure_schedules_once(
        self,
        *,
        schedule_id: str = "",
        force: bool = False,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        """Prepare and queue due observer-local relationship disclosures."""

        current = now_ms() if now is None else int(now)
        schedules = RelationshipDisclosureScheduleBook(
            self.config.relationship_disclosure_schedules_path,
            own_actor_id=self.node_id,
        )
        relationships = RelationshipBook(
            self.config.relationships_path,
            own_actor_id=self.node_id,
        )
        relationships.reload()
        model = relationships.snapshot()
        if schedule_id:
            selected = (schedules.require(schedule_id),)
        else:
            selected = schedules.all()
        results: list[dict[str, Any]] = []
        for selected_item in selected:
            schedules.reload()
            item = schedules.require(selected_item.schedule_id)
            if item.state(now=current) != "active":
                if item.state(now=current) == "expired":
                    item = schedules.discard_expired_pending(
                        item.schedule_id,
                        now=current,
                    )
                results.append(
                    {
                        "schedule_id": item.schedule_id,
                        "state": item.state(now=current),
                        "queued": False,
                    }
                )
                continue
            if not force and not item.due(now=current):
                continue
            try:
                pending = item.pending
                if pending is None:
                    page = RelationshipActivityFeed.read(
                        model,
                        after=item.cursor,
                        limit=item.batch_limit,
                        subject_ref=item.subject_ref,
                    )
                    if (
                        not page.activities
                        and page.next_cursor == item.cursor
                    ):
                        schedules.record_idle(
                            item.schedule_id,
                            now=current,
                        )
                        results.append(
                            {
                                "schedule_id": item.schedule_id,
                                "state": "active",
                                "queued": False,
                                "activities": 0,
                            }
                        )
                        continue
                    disclosure = RelationshipDisclosure.create_series(
                        page,
                        audience_actor_id=item.audience_actor_id,
                        series_id=item.series_id,
                        sequence=item.next_sequence,
                        starts_after=item.cursor,
                        scope_subject_ref=item.subject_ref,
                        baseline=item.baseline,
                        now=current,
                    )
                    item = schedules.prepare(
                        item.schedule_id,
                        disclosure,
                        start_cursor=item.cursor,
                        now=current,
                    )
                    pending = item.pending
                if pending is None:
                    raise RuntimeError(
                        "relationship disclosure preparation was not persisted"
                    )
                packet_id = self.queue(
                    item.audience_actor_id,
                    kind=RELATIONSHIP_DISCLOSURE_KIND,
                    body=pending.disclosure.to_dict(),
                    ttl_seconds=item.packet_ttl_seconds,
                    qos="normal",
                )
                RelationshipDisclosureArchiveBook(
                    self.config.relationship_disclosure_archive_path,
                    own_actor_id=self.node_id,
                ).add(
                    item.schedule_id,
                    packet_id,
                    pending.disclosure,
                    archived_ms=current,
                )
                completed = schedules.record_success(
                    item.schedule_id,
                    packet_id,
                    now=current,
                )
                results.append(
                    {
                        "schedule_id": item.schedule_id,
                        "state": completed.state(now=current),
                        "queued": True,
                        "packet_id": packet_id,
                        "disclosure_id": pending.disclosure.disclosure_id,
                        "activities": len(pending.disclosure.activities),
                        "checkpoint": not pending.disclosure.activities,
                        "has_more": pending.disclosure.has_more,
                    }
                )
            except Exception as exc:
                schedules.record_failure(
                    item.schedule_id,
                    type(exc).__name__,
                    now=current,
                )
                results.append(
                    {
                        "schedule_id": item.schedule_id,
                        "state": "active",
                        "queued": False,
                        "error": type(exc).__name__,
                    }
                )
        return results

    def queue_relationship_disclosure_gap_notice(
        self,
        observer_actor_id: str,
        series_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Report visible missing pages without requesting data or authority."""

        observer = validate_actor_id(observer_actor_id)
        book = RelationshipDisclosureBook(
            self.config.relationship_disclosures_path,
            own_actor_id=self.node_id,
        )
        view = ReportedRelationshipViewProjector.project(
            book,
            sender_actor_id=observer,
            series_id=series_id,
            now=now,
        )
        analysis = next(
            item
            for item in view["provenance"]["series"]
            if item["series_id"] == view["selected_series_id"]
        )
        missing = tuple(int(item) for item in analysis["missing_sequences"])
        if not missing:
            raise ValueError("relationship disclosure series has no visible gap")
        notice = RelationshipDisclosureGapNotice.create(
            reporter_actor_id=self.node_id,
            observer_actor_id=observer,
            series_id=view["selected_series_id"],
            missing_sequences=missing,
            detected_through_sequence=int(analysis["last_sequence"]),
            now=now,
        )
        packet_id = self.queue(
            observer,
            kind=RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND,
            body=notice.to_dict(),
            ttl_seconds=7 * 86400,
            qos="control",
        )
        return {
            "queued": True,
            "packet_id": packet_id,
            "notice_id": notice.notice_id,
            "observer_actor_id": observer,
            "series_id": notice.series_id,
            "missing_sequences": list(notice.missing_sequences),
            "meaning": "delivery-gap-observed",
            "requested_action": "none",
            "authorization_effect": "none",
        }

    def retransmit_relationship_disclosure_gap(
        self,
        notice_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Retransmit exact archived pages only under their still-active schedule."""

        current = now_ms() if now is None else int(now)
        received = RelationshipDisclosureGapNoticeBook(
            self.config.relationship_disclosure_gap_notices_path,
            own_actor_id=self.node_id,
        ).require(notice_id)
        notice = received.notice
        schedules = RelationshipDisclosureScheduleBook(
            self.config.relationship_disclosure_schedules_path,
            own_actor_id=self.node_id,
        )
        candidates = [
            item
            for item in schedules.all()
            if item.series_id == notice.series_id
            and item.audience_actor_id == notice.reporter_actor_id
        ]
        if len(candidates) != 1:
            raise ValueError(
                "gap notice does not match one local disclosure schedule"
            )
        schedule = candidates[0]
        if schedule.state(now=current) != "active":
            raise PermissionError(
                "gap retransmission requires the original active schedule"
            )
        archive = RelationshipDisclosureArchiveBook(
            self.config.relationship_disclosure_archive_path,
            own_actor_id=self.node_id,
        )
        retransmitted: list[dict[str, Any]] = []
        unavailable: list[int] = []
        for sequence in notice.missing_sequences:
            archived = archive.find(notice.series_id, sequence)
            if (
                archived is None
                or archived.schedule_id != schedule.schedule_id
                or archived.disclosure.audience_actor_id
                != notice.reporter_actor_id
            ):
                unavailable.append(sequence)
                continue
            packet_id = self.queue(
                notice.reporter_actor_id,
                kind=RELATIONSHIP_DISCLOSURE_KIND,
                body=archived.disclosure.to_dict(),
                ttl_seconds=schedule.packet_ttl_seconds,
                qos="normal",
            )
            retransmitted.append(
                {
                    "sequence": sequence,
                    "disclosure_id": archived.disclosure.disclosure_id,
                    "packet_id": packet_id,
                }
            )
        return {
            "notice_id": notice.notice_id,
            "series_id": notice.series_id,
            "audience_actor_id": notice.reporter_actor_id,
            "schedule_id": schedule.schedule_id,
            "schedule_state": "active",
            "retransmitted": retransmitted,
            "unavailable_sequences": unavailable,
            "scope_unchanged": True,
            "series_advanced": False,
            "authorization_effect": "none",
        }

    async def _ahub_relay_listener_loop(
        self,
        carrier: AhubCarrierConfig,
        peer_id: str,
    ) -> None:
        from .ahub_http import AhubHTTPClient, AhubHTTPError
        from .carriers.ahub import current_node_descriptor

        client = AhubHTTPClient(
            carrier.base_url,
            self.identity,
            timeout_seconds=carrier.timeout,
            allow_insecure_http=carrier.allow_insecure_http,
        )
        schedule = AdaptiveSchedule(
            carrier.relay_listener_retry_seconds,
            carrier.jitter,
            carrier.idle_backoff_max,
        )
        while not self._stop.is_set():
            activity = False
            try:
                descriptor = current_node_descriptor(self)
                await asyncio.to_thread(
                    client.publish_descriptor,
                    descriptor,
                )
                reservation = await asyncio.to_thread(
                    client.reserve_relay,
                    peer_id,
                    ttl_ms=int(
                        carrier.relay_reservation_ttl_seconds * 1000
                    ),
                    max_duration_ms=int(
                        carrier.relay_session_seconds * 1000
                    ),
                    max_bytes_each_direction=(
                        carrier.relay_bytes_each_direction
                    ),
                )
                await self.serve_ahub_relay_once(
                    client,
                    reservation.reservation_id,
                    path_id=carrier.relay_path_id,
                )
                activity = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = (
                    exc.category
                    if isinstance(exc, AhubHTTPError)
                    else type(exc).__name__
                )
                LOGGER.warning(
                    "Ahub Relay listener failed carrier=%s category=%s",
                    carrier.name,
                    category,
                )
            delay = schedule.record(
                time.monotonic(),
                activity=activity,
                base_interval=carrier.relay_listener_retry_seconds,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _sync_peer(self, expected: PeerCard) -> bool:
        lock = self._peer_locks.setdefault(expected.node_id, asyncio.Lock())
        if lock.locked():
            return False
        async with lock:
            total_started = time.perf_counter()
            last_error = "no usable address"
            locators = usable_locators(
                expected.addresses, self.config.locator_contexts
            )
            dialers = self.config.effective_direct_dialers()
            attempts: list[tuple[tuple[Any, ...], DirectDialerConfig, Any]] = []
            current_ms = now_ms()
            retry_ms = int(self.config.routing.direct_retry_interval * 1000)
            for locator in locators:
                for dialer in dialers:
                    path_id = f"{dialer.path_prefix}:{locator.raw}"
                    metric = self.store.path_metric(expected.node_id, path_id) or {}
                    failures = int(metric.get("consecutive_failures", 0))
                    last_failure = int(metric.get("last_failure_ms", 0))
                    cooling = (
                        failures >= self.config.routing.direct_failure_threshold
                        and current_ms - last_failure < retry_ms
                    )
                    attempts.append(
                        (
                            (
                                1 if cooling else 0,
                                locator.priority + dialer.priority,
                                failures,
                                float(metric.get("ewma_rtt_ms", 0.0)) or float("inf"),
                                dialer.name,
                                locator.raw,
                            ),
                            dialer,
                            locator,
                        )
                    )
            attempts.sort(key=lambda item: item[0])
            race_width = self.config.routing.direct_race_width
            race_delay = self.config.routing.direct_race_delay

            async def run_candidate(
                index: int,
                dialer: DirectDialerConfig,
                locator: Any,
                delay: float,
            ) -> tuple[int, DirectDialerConfig, Any, float, str | None]:
                if delay:
                    await asyncio.sleep(delay)
                started = time.perf_counter()
                try:
                    await self._sync_address(expected, locator.raw, dialer)
                except Exception as exc:
                    return (
                        index,
                        dialer,
                        locator,
                        (time.perf_counter() - started) * 1000.0,
                        str(exc),
                    )
                return (
                    index,
                    dialer,
                    locator,
                    (time.perf_counter() - started) * 1000.0,
                    None,
                )

            for batch_start in range(0, len(attempts), race_width):
                batch = attempts[batch_start : batch_start + race_width]
                tasks = [
                    asyncio.create_task(
                        run_candidate(
                            batch_start + offset,
                            dialer,
                            locator,
                            race_delay * offset if race_width > 1 else 0.0,
                        )
                    )
                    for offset, (_, dialer, locator) in enumerate(batch)
                ]
                pending = set(tasks)
                try:
                    while pending:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        results = sorted(
                            (task.result() for task in done), key=lambda item: item[0]
                        )
                        successful: list[tuple[int, DirectDialerConfig, Any, float, str | None]] = []
                        for result in results:
                            _, dialer, locator, latency_ms, error = result
                            path_id = f"{dialer.path_prefix}:{locator.raw}"
                            if error is None:
                                self.store.record_path_result(
                                    expected.node_id,
                                    path_id,
                                    success=True,
                                    latency_ms=latency_ms,
                                )
                                successful.append(result)
                            else:
                                last_error = f"{dialer.name} -> {locator.raw}: {error}"
                                self.store.record_path_result(
                                    expected.node_id,
                                    path_id,
                                    success=False,
                                    latency_ms=latency_ms,
                                    error=error,
                                )
                        if successful:
                            _, dialer, locator, _, _ = successful[0]
                            path_id = f"{dialer.path_prefix}:{locator.raw}"
                            total_latency = (
                                time.perf_counter() - total_started
                            ) * 1000.0
                            self.store.record_path_result(
                                expected.node_id,
                                "direct",
                                success=True,
                                latency_ms=total_latency,
                            )
                            self.peer_state[expected.node_id] = {
                                "online": True,
                                "address": locator.raw,
                                "dialer": dialer.name,
                                "path_id": path_id,
                                "last_ok_ms": now_ms(),
                                "error": "",
                                "latency_ms": round(total_latency, 3),
                            }
                            return True
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            total_latency = (time.perf_counter() - total_started) * 1000.0
            self.store.record_path_result(
                expected.node_id,
                "direct",
                success=False,
                latency_ms=total_latency,
                error=last_error,
            )
            self.peer_state[expected.node_id] = {
                "online": False,
                "address": "",
                "dialer": "",
                "path_id": "",
                "last_ok_ms": self.peer_state.get(expected.node_id, {}).get("last_ok_ms", 0),
                "error": last_error,
                "latency_ms": round(total_latency, 3),
            }
            if not locators and expected.addresses:
                last_error = "no locator matches this node's host/LAN contexts"
                self.peer_state[expected.node_id]["error"] = last_error
            elif not dialers:
                last_error = "no enabled direct dialer"
                self.peer_state[expected.node_id]["error"] = last_error
            raise ConnectionError(last_error)

    async def _sync_address(
        self,
        expected: PeerCard,
        address: str,
        dialer: DirectDialerConfig,
    ) -> None:
        try:
            reader, writer = await self._open_dialer_connection(address, dialer)
        except Exception as exc:
            category = classify_connection_error(
                exc, proxied=dialer.proxy is not None
            )
            raise ConnectionError(f"{category}: {exc}") from exc
        await self._sync_stream(
            expected,
            reader,
            writer,
            path_id="direct",
        )

    async def _sync_stream(
        self,
        expected: PeerCard,
        reader: asyncio.StreamReader,
        writer: Any,
        *,
        path_id: str,
    ) -> None:
        try:
            try:
                remote = await self._client_handshake(reader, writer, expected)
            except Exception as exc:
                raise ConnectionError(f"identity_handshake: {exc}") from exc
            try:
                pending = self.store.pending_for_peer(
                    remote.node_id, limit=self.config.max_batch
                )
                outgoing_ids = [item["packet_id"] for item in pending]
                self.store.mark_attempt(
                    outgoing_ids, remote.node_id, path_id=path_id
                )
                await write_frame(
                    writer,
                    {
                        "t": "sync",
                        "items": [
                            {"raw": item["raw"], "depth": int(item["depth"]) + 1}
                            for item in pending
                        ],
                    },
                )
                reply = await read_frame(reader)
                if not isinstance(reply, dict) or reply.get("t") != "sync-reply":
                    raise ValueError("invalid sync reply")
                acked = [str(item) for item in reply.get("ack", [])]
                self.store.mark_acked(
                    [item for item in acked if item in outgoing_ids],
                    remote.node_id,
                    path_id=path_id,
                )
                received = await self._receive_items(
                    reply.get("items", []), remote.node_id
                )
                await write_frame(writer, {"t": "sync-ack", "ack": received})
            except Exception as exc:
                raise ConnectionError(f"sync_protocol: {exc}") from exc
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def sync_ahub_relay_once(
        self,
        peer_id: str,
        client: AhubHTTPClient,
        reservation_id: str,
        *,
        path_id: str = "ahub-relay",
    ) -> None:
        """Run the existing authenticated sync protocol over Ahub Relay."""

        from .ahub_transport import open_ahub_relay_tls_connection

        if client.identity.node_id != self.node_id:
            raise ValueError("Relay client identity does not own this Anet node")
        if not path_id.startswith("ahub-relay"):
            raise ValueError("Relay sync path ID must use ahub-relay prefix")
        expected = self.peers.require(peer_id)
        reader, writer = await open_ahub_relay_tls_connection(
            client,
            reservation_id,
            self._client_context,
        )
        await self._sync_stream(
            expected,
            reader,
            writer,
            path_id=path_id,
        )

    async def _open_dialer_connection(
        self,
        address: str,
        dialer: DirectDialerConfig,
    ) -> tuple[asyncio.StreamReader, Any]:
        host, port = parse_tls_address(address)
        reader: asyncio.StreamReader
        writer: Any
        if dialer.proxy is not None:
            reader, writer = await open_proxy_tls_connection(
                dialer.proxy, host, port, self._client_context
            )
        elif dialer.stdio is not None:
            reader, writer = await open_stdio_tls_connection(
                dialer.stdio, host, port, self._client_context
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=host,
                    port=port,
                    ssl=self._client_context,
                    server_hostname=None,
                ),
                timeout=5.0,
            )
        return reader, writer

    async def _health_address(
        self,
        expected: PeerCard,
        address: str,
        dialer: DirectDialerConfig,
    ) -> None:
        try:
            reader, writer = await self._open_dialer_connection(address, dialer)
        except Exception as exc:
            category = classify_connection_error(
                exc, proxied=dialer.proxy is not None
            )
            raise ConnectionError(f"{category}: {exc}") from exc
        try:
            try:
                remote = await self._client_handshake(reader, writer, expected)
            except Exception as exc:
                raise ConnectionError(f"identity_handshake: {exc}") from exc
            if "link-health-v1" not in remote.capabilities:
                raise ConnectionError(
                    "health_unsupported: peer does not advertise link-health-v1"
                )
            try:
                await write_frame(writer, {"t": "health"})
                reply = await read_frame(reader)
                if not isinstance(reply, dict) or reply.get("t") != "health-reply":
                    raise ValueError("invalid health reply")
            except Exception as exc:
                raise ConnectionError(f"health_protocol: {exc}") from exc
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def probe_dialers(
        self,
        peer_id: str,
        *,
        dialer_names: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        expected = self.peers.require(peer_id)
        locators = usable_locators(
            expected.addresses, self.config.locator_contexts
        )
        dialers = self.config.effective_direct_dialers()
        if dialer_names:
            requested = set(dialer_names)
            known = {dialer.name for dialer in dialers}
            unknown = requested - known
            if unknown:
                raise ValueError(
                    f"unknown enabled direct dialer(s): {', '.join(sorted(unknown))}"
                )
            dialers = tuple(
                dialer for dialer in dialers if dialer.name in requested
            )
        results: list[dict[str, Any]] = []
        for locator in locators:
            for dialer in dialers:
                started = time.perf_counter()
                error = ""
                try:
                    await self._health_address(expected, locator.raw, dialer)
                    healthy = True
                    category = "authenticated"
                except Exception as exc:
                    healthy = False
                    error = str(exc)[:1000]
                    category = error.split(":", 1)[0] if ":" in error else "unknown"
                latency_ms = (time.perf_counter() - started) * 1000.0
                path_id = f"health:{dialer.name}:{locator.raw}"
                self.store.record_path_result(
                    expected.node_id,
                    path_id,
                    success=healthy,
                    latency_ms=latency_ms,
                    error=error,
                )
                results.append(
                    {
                        "peer_id": expected.node_id,
                        "dialer": dialer.name,
                        "address": locator.raw,
                        "path_id": path_id,
                        "healthy": healthy,
                        "category": category,
                        "latency_ms": round(latency_ms, 3),
                        "error": error,
                    }
                )
        healthy_count = sum(1 for item in results if item["healthy"])
        return {
            "ok": bool(results) and healthy_count > 0,
            "all_healthy": bool(results) and healthy_count == len(results),
            "peer_id": expected.node_id,
            "tested": len(results),
            "healthy": healthy_count,
            "results": results,
        }

    async def serve_ahub_relay_once(
        self,
        client: AhubHTTPClient,
        reservation_id: str,
        *,
        path_id: str = "ahub-relay",
    ) -> None:
        from .ahub_transport import bridge_ahub_relay_to_tcp

        """Accept one Relay session through an unadvertised loopback TLS socket."""

        if client.identity.node_id != self.node_id:
            raise ValueError("Relay client identity does not own this Anet node")
        if not path_id.startswith("ahub-relay"):
            raise ValueError("Relay listener path ID must use ahub-relay prefix")

        connection_started = asyncio.Event()
        connection_done = asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            connection_started.set()
            try:
                await self._handle_connection(
                    reader,
                    writer,
                    path_id=path_id,
                )
            finally:
                connection_done.set()

        server = await asyncio.start_server(
            handle,
            host="127.0.0.1",
            port=0,
            ssl=self._server_context,
        )
        sockets = server.sockets or ()
        if len(sockets) != 1:
            server.close()
            await server.wait_closed()
            raise RuntimeError("Relay TLS bridge did not obtain one loopback socket")
        port = int(sockets[0].getsockname()[1])
        try:
            await bridge_ahub_relay_to_tcp(
                client,
                reservation_id,
                target_host="127.0.0.1",
                target_port=port,
            )
        finally:
            server.close()
            await server.wait_closed()
            if connection_started.is_set():
                await asyncio.wait_for(
                    connection_done.wait(),
                    timeout=5.0,
                )

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        path_id: str = "direct",
    ) -> None:
        peer_id = ""
        try:
            peer = await self._server_handshake(reader, writer)
            peer_id = peer.node_id
            request = await read_frame(reader)
            if isinstance(request, dict) and request.get("t") == "health":
                await write_frame(writer, {"t": "health-reply"})
                return
            if not isinstance(request, dict) or request.get("t") != "sync":
                raise ValueError("invalid sync request")
            accepted = await self._receive_items(request.get("items", []), peer.node_id)
            pending = self.store.pending_for_peer(peer.node_id, limit=self.config.max_batch)
            outgoing_ids = [item["packet_id"] for item in pending]
            self.store.mark_attempt(
                outgoing_ids,
                peer.node_id,
                path_id=path_id,
            )
            await write_frame(
                writer,
                {
                    "t": "sync-reply",
                    "ack": accepted,
                    "items": [
                        {"raw": item["raw"], "depth": int(item["depth"]) + 1}
                        for item in pending
                    ],
                },
            )
            final = await read_frame(reader)
            if not isinstance(final, dict) or final.get("t") != "sync-ack":
                raise ValueError("invalid final sync acknowledgement")
            acked = [str(item) for item in final.get("ack", [])]
            self.store.mark_acked(
                [item for item in acked if item in outgoing_ids],
                peer.node_id,
                path_id=path_id,
            )
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            LOGGER.debug("peer connection ended: %s", peer_id or "unknown", exc_info=True)
        except Exception:
            LOGGER.warning("rejected peer connection: %s", peer_id or "unknown", exc_info=True)
            with suppress(Exception):
                await write_frame(writer, {"t": "error", "error": "connection rejected"})
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _server_handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> PeerCard:
        server_nonce = os.urandom(32)
        challenge_fields = [
            LINK_VERSION,
            "challenge",
            self.node_id,
            server_nonce,
            self._tls_fingerprint,
        ]
        await write_frame(
            writer,
            {
                "t": "challenge",
                "v": LINK_VERSION,
                "card": self.local_card.to_wire(),
                "sn": server_nonce,
                "sfp": self._tls_fingerprint,
                "sig": self.identity.sign(canonical_pack(challenge_fields)),
            },
        )
        auth = await read_frame(reader)
        if not isinstance(auth, dict) or auth.get("t") != "auth":
            raise ValueError("missing peer authentication")
        card = PeerCard.from_wire(auth["card"])
        pinned = self.peers.require(card.node_id)
        self._require_same_keys(pinned, card)
        client_nonce = bytes(auth.get("cn", b""))
        if len(client_nonce) != 32 or bytes(auth.get("sn", b"")) != server_nonce:
            raise ValueError("invalid authentication challenge")
        auth_fields = [
            LINK_VERSION,
            "auth",
            card.node_id,
            self.node_id,
            server_nonce,
            client_nonce,
            self._tls_fingerprint,
        ]
        Ed25519PublicKey.from_public_bytes(card.sign_public).verify(
            bytes(auth.get("sig", b"")),
            canonical_pack(auth_fields),
        )
        ready_fields = [
            LINK_VERSION,
            "ready",
            self.node_id,
            card.node_id,
            server_nonce,
            client_nonce,
            self._tls_fingerprint,
        ]
        await write_frame(
            writer,
            {
                "t": "ready",
                "sn": server_nonce,
                "cn": client_nonce,
                "sig": self.identity.sign(canonical_pack(ready_fields)),
            },
        )
        return card

    async def _client_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        expected: PeerCard,
    ) -> PeerCard:
        ssl_object = writer.get_extra_info("ssl_object")
        if ssl_object is None:
            raise ValueError("TLS channel is missing")
        certificate = ssl_object.getpeercert(binary_form=True)
        observed_fingerprint = hashlib.sha256(certificate).digest()
        challenge = await read_frame(reader)
        if not isinstance(challenge, dict) or challenge.get("t") != "challenge":
            raise ValueError("missing server challenge")
        card = PeerCard.from_wire(challenge["card"])
        if card.node_id != expected.node_id:
            raise ValueError("connected server identity does not match address owner")
        self._require_same_keys(expected, card)
        server_nonce = bytes(challenge.get("sn", b""))
        server_fingerprint = bytes(challenge.get("sfp", b""))
        if len(server_nonce) != 32 or server_fingerprint != observed_fingerprint:
            raise ValueError("TLS channel is not bound to server identity")
        challenge_fields = [
            LINK_VERSION,
            "challenge",
            card.node_id,
            server_nonce,
            server_fingerprint,
        ]
        Ed25519PublicKey.from_public_bytes(card.sign_public).verify(
            bytes(challenge.get("sig", b"")),
            canonical_pack(challenge_fields),
        )
        client_nonce = os.urandom(32)
        auth_fields = [
            LINK_VERSION,
            "auth",
            self.node_id,
            card.node_id,
            server_nonce,
            client_nonce,
            server_fingerprint,
        ]
        await write_frame(
            writer,
            {
                "t": "auth",
                "card": self.local_card.to_wire(),
                "sn": server_nonce,
                "cn": client_nonce,
                "sig": self.identity.sign(canonical_pack(auth_fields)),
            },
        )
        ready = await read_frame(reader)
        if not isinstance(ready, dict) or ready.get("t") != "ready":
            raise ValueError("server did not complete authentication")
        if bytes(ready.get("sn", b"")) != server_nonce or bytes(ready.get("cn", b"")) != client_nonce:
            raise ValueError("server authentication nonce mismatch")
        ready_fields = [
            LINK_VERSION,
            "ready",
            card.node_id,
            self.node_id,
            server_nonce,
            client_nonce,
            server_fingerprint,
        ]
        Ed25519PublicKey.from_public_bytes(card.sign_public).verify(
            bytes(ready.get("sig", b"")),
            canonical_pack(ready_fields),
        )
        return card

    @staticmethod
    def _require_same_keys(expected: PeerCard, actual: PeerCard) -> None:
        if expected.sign_public != actual.sign_public or expected.box_public != actual.box_public:
            raise ValueError("peer keys differ from pinned identity")

    async def _receive_items(self, items: Any, peer_id: str) -> list[str]:
        if not isinstance(items, list) or len(items) > self.config.max_batch:
            raise ValueError("invalid sync item batch")
        accepted: list[str] = []
        for item in items:
            try:
                if not isinstance(item, dict):
                    raise ValueError("sync item must be a map")
                raw = bytes(item["raw"])
                depth = int(item["depth"])
                accepted.append(self.accept_carrier_packet(raw, depth=depth, peer_id=peer_id))
            except Exception:
                LOGGER.warning("rejected packet from %s", peer_id, exc_info=True)
        return accepted

    def accept_carrier_packet(self, raw: bytes, *, depth: int, peer_id: str) -> str:
        """Accept one packet from an authenticated carrier peer.

        Carriers authenticate their own link or mailbox frames, then hand the
        still end-to-end encrypted packet to this common validation path.
        """
        info = inspect_packet(raw)
        depth = int(depth)
        if depth < 1 or depth > info.max_hops:
            raise ValueError("invalid relay depth")
        self.store.add_packet(raw, depth=depth, origin="relay", received_from=peer_id)
        if info.destination_id == self.node_id:
            if self.store.packet_delivered(info.packet_id):
                return info.packet_id
            try:
                self._process_local_packet(raw)
            except (InvalidSignature, InvalidTag, ValueError) as exc:
                self.store.mark_local_rejected(
                    info.packet_id,
                    peer_id=peer_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                raise
        return info.packet_id

    def _process_local_packet(self, raw: bytes) -> None:
        info = inspect_packet(raw)
        prekey_private: bytes | None = None
        material: dict[str, Any] | None = None
        if info.key_mode == "opk":
            material = self.store.local_prekey_material(info.prekey_id)
            if material is None:
                raise ValueError("packet one-time prekey is unavailable")
            if info.created_ms > int(material["expires_ms"]):
                raise ValueError("packet was created after its one-time prekey expired")
            prekey_private = bytes(material["private_key"])
        message = open_packet(
            self.identity,
            raw,
            recipient_prekey_private=prekey_private,
        )
        if message.kind.startswith("companion."):
            if message.kind not in COMPANION_KINDS:
                raise ValueError("unsupported Companion kind")
            message = replace(
                message,
                body=validate_companion_endpoint_binding(
                    message.kind,
                    message.body,
                    sender_node_id=message.sender_id,
                    destination_node_id=self.node_id,
                ),
            )
        elif message.kind == DISCORD_SIGNAL_KIND:
            message = replace(
                message,
                body=validate_discord_signal(message.body),
            )
        elif message.kind == RELATIONSHIP_DISCLOSURE_KIND:
            message = replace(
                message,
                body=validate_relationship_disclosure(
                    message.body,
                    sender_node_id=message.sender_id,
                    destination_node_id=self.node_id,
                ),
            )
        elif message.kind == RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND:
            message = replace(
                message,
                body=validate_relationship_disclosure_gap_notice(
                    message.body,
                    sender_node_id=message.sender_id,
                    destination_node_id=self.node_id,
                ),
            )
        if (
            info.key_mode == "opk"
            and material is not None
            and str(material["peer_id"]) != message.sender_id
        ):
            raise ValueError(
                "packet sender is not authorized for this one-time prekey"
            )
        trusted = self.peers.is_trusted(
            message.sender_id,
            message.sender_sign_public,
            message.sender_box_public,
        )
        if trusted and message.kind == PREKEY_BUNDLE_KIND:
            # Import before committing the control Inbox record. If the process
            # crashes afterward, redelivery is safe because bundle import is
            # idempotent; the reverse ordering could lose replenishment forever.
            self._handle_prekey_bundle(message)
        if trusted and message.kind == RELATIONSHIP_DISCLOSURE_KIND:
            # Persist the idempotent observer projection before committing the
            # Inbox record. A crash between the two steps can then replay
            # safely; the reverse ordering could hide a delivered disclosure
            # from its dedicated observer view forever.
            self._store_relationship_disclosure(message)
        if (
            trusted
            and message.kind
            == RELATIONSHIP_DISCLOSURE_GAP_NOTICE_KIND
        ):
            self._store_relationship_disclosure_gap_notice(message)
        created = self.store.commit_local_message(
            message,
            trusted=trusted,
            visible=message.kind
            not in {"network.probe", "receipt", *PREKEY_CONTROL_KINDS},
        )
        if not created or not trusted:
            return
        self._project_interaction(
            self.peers.require(message.sender_id),
            packet_id=message.packet_id,
            kind=message.kind,
            body=message.body,
            direction="incoming",
            occurred_ms=message.created_ms,
        )
        if message.kind == PREKEY_REQUEST_KIND:
            self._handle_prekey_request(message)
            return
        if message.kind == PREKEY_BUNDLE_KIND:
            return
        if message.kind == "receipt":
            if isinstance(message.body, dict):
                packet_id = str(message.body.get("packet_id", ""))
                if len(packet_id) == 32:
                    self.store.record_receipt(packet_id, message.sender_id)
            return
        try:
            self.queue(
                message.sender_id,
                kind="receipt",
                body={"packet_id": message.packet_id},
                ttl_seconds=7 * 86400,
                max_hops=self.config.max_hops,
                causal=[message.packet_id],
                reply_to=message.packet_id,
                qos="control",
            )
        except Exception:
            LOGGER.warning(
                "message %s was accepted but its receipt could not be queued",
                message.packet_id,
                exc_info=True,
            )

    def _store_relationship_disclosure(self, message: Any) -> None:
        """Persist a trusted disclosure outside the local relation model."""

        if self._relationship_disclosure_book is None:
            self._relationship_disclosure_book = (
                RelationshipDisclosureBook(
                    self.config.relationship_disclosures_path,
                    own_actor_id=self.node_id,
                )
            )
        self._relationship_disclosure_book.add(
            RelationshipDisclosure.from_dict(message.body),
            packet_id=message.packet_id,
            sender_actor_id=message.sender_id,
            received_ms=now_ms(),
        )

    def _store_relationship_disclosure_gap_notice(self, message: Any) -> None:
        """Persist a trusted advisory notice outside local authorization."""

        if self._relationship_gap_notice_book is None:
            self._relationship_gap_notice_book = (
                RelationshipDisclosureGapNoticeBook(
                    self.config.relationship_disclosure_gap_notices_path,
                    own_actor_id=self.node_id,
                )
            )
        self._relationship_gap_notice_book.add(
            RelationshipDisclosureGapNotice.from_dict(message.body),
            packet_id=message.packet_id,
            sender_actor_id=message.sender_id,
            received_ms=now_ms(),
        )

    def _project_interaction(
        self,
        card: PeerCard,
        *,
        packet_id: str,
        kind: str,
        body: Any,
        direction: str,
        occurred_ms: int,
    ) -> None:
        """Best-effort social projection; it never controls packet acceptance."""

        try:
            relationship_projector = self._ensure_relationship_projectors()
            relationship_projector.project_packet(
                card,
                packet_id=packet_id,
                kind=kind,
                body=body,
                direction=direction,
                occurred_ms=occurred_ms,
            )
            if kind == DISCORD_SIGNAL_KIND and direction == "incoming":
                if self._discord_relationship_projector is None:
                    raise RuntimeError("Discord relationship projector is missing")
                self._discord_relationship_projector.project_signal(card, body)
        except Exception:
            LOGGER.warning(
                "packet %s succeeded but relationship evidence was not projected",
                packet_id,
                exc_info=True,
            )

    def _ensure_relationship_projectors(self) -> RelationshipProjector:
        if self._relationship_projector is None:
            self._relationship_projector = RelationshipProjector(
                RelationshipBook(
                    self.config.relationships_path,
                    own_actor_id=self.node_id,
                )
            )
        if self._discord_relationship_projector is None:
            self._discord_relationship_projector = DiscordRelationshipProjector(
                self._relationship_projector.book
            )
        return self._relationship_projector

    def _project_discord_event(self, event: dict[str, Any]) -> None:
        self._ensure_relationship_projectors()
        if self._discord_relationship_projector is None:
            raise RuntimeError("Discord relationship projector is missing")
        self._discord_relationship_projector.project_local_event(event)

    def process_local_spool(self) -> int:
        processed = 0
        for raw in self.store.local_packets(self.node_id):
            try:
                self._process_local_packet(raw)
                processed += 1
            except (InvalidSignature, InvalidTag, ValueError) as exc:
                info = inspect_packet(raw, allow_expired=True)
                self.store.mark_local_rejected(
                    info.packet_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                LOGGER.warning(
                    "rejected deterministic local spooled packet %s",
                    info.packet_id,
                )
            except Exception:
                LOGGER.warning("failed to process local spooled packet", exc_info=True)
        return processed

    def status(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.identity.label,
            "listen": (
                f"{self.config.listen_host}:{self.config.listen_port}"
                if self.config.listen_enabled
                else None
            ),
            "addresses": list(self.config.effective_addresses()),
            "direct_proxy": (
                self.config.direct_proxy.to_dict() if self.config.direct_proxy else None
            ),
            "direct_dialers": [
                dialer.to_dict() for dialer in self.config.effective_direct_dialers()
            ],
            "trusted_peers": len(self.peers.all()),
            "prekeys": {
                "policy": self.config.prekey_policy,
                "scope_warning": self._prekey_scope_warning,
                "auto": {
                    "enabled": self.config.prekey_auto_enabled,
                    "low_watermark": self.config.prekey_low_watermark,
                    "batch_size": self.config.prekey_batch_size,
                    "request_interval": self.config.prekey_request_interval,
                    "ttl_days": self.config.prekey_ttl_days,
                },
                **self.store.prekey_status(),
            },
            "sync_schedule": {
                "interval": self.config.sync_interval,
                "jitter": self.config.sync_jitter,
                "direct_retry_interval": self.config.routing.direct_retry_interval,
                "direct_race_width": self.config.routing.direct_race_width,
                "direct_race_delay": self.config.routing.direct_race_delay,
                "direct_idle_probe_interval": self.config.routing.direct_idle_probe_interval,
                "direct_probe_jitter": self.config.routing.direct_probe_jitter,
            },
            "store": self.store.status(),
            "peer_state": self.peer_state,
            "routes": self.store.routes(),
            "path_metrics": self.store.path_metrics(),
            "carriers": [
                {
                    "name": carrier.name,
                    "type": (
                        "directory"
                        if isinstance(carrier, DirectoryCarrierConfig)
                        else (
                            "webdav"
                            if isinstance(carrier, WebDAVCarrierConfig)
                            else "ahub"
                        )
                    ),
                    "mode": carrier.mode,
                    "enabled": carrier.enabled,
                    "priority": carrier.priority,
                    "interval": carrier.interval,
                    "jitter": carrier.jitter,
                    "idle_backoff_max": carrier.idle_backoff_max,
                    "peers": list(carrier.peers),
                }
                for carrier in (
                    *self.config.directory_carriers,
                    *self.config.webdav_carriers,
                    *self.config.ahub_carriers,
                )
            ],
        }
