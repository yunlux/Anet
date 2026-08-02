from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ahub_http import AhubHTTPClient
from ..config import AhubCarrierConfig
from ..control_plane import (
    GENESIS_DIGEST,
    NodeDescriptor,
    ReachabilityRecord,
    issue_node_descriptor,
    issue_reachability_record,
)
from ..encoding import atomic_json, b64d, b64e, canonical_pack
from ..identity import PeerCard
from ..packet import now_ms

if TYPE_CHECKING:
    from ..node import AnetNode


CONTROL_STATE_VERSION = 1
CONTROL_STATE_FILENAME = "control-state.json"
DESCRIPTOR_TTL_MS = 30 * 24 * 60 * 60 * 1000
DESCRIPTOR_REFRESH_MS = 7 * 24 * 60 * 60 * 1000
REACHABILITY_STATE_FILENAME = "reachability-state.json"
REACHABILITY_TTL_MS = 5 * 60 * 1000
REACHABILITY_REFRESH_MS = 60 * 1000
REACHABILITY_PROTOCOL_VERSIONS = ("anet/1",)


def _load_descriptor_state(path: Path) -> NodeDescriptor | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid local control-plane state") from exc
    if not isinstance(value, dict) or set(value) != {"version", "descriptor"}:
        raise ValueError("invalid local control-plane state")
    if value["version"] != CONTROL_STATE_VERSION:
        raise ValueError("unsupported local control-plane state")
    descriptor = value["descriptor"]
    if not isinstance(descriptor, dict):
        raise ValueError("invalid local control-plane descriptor")
    issued = descriptor.get("issued_ms")
    if isinstance(issued, bool) or not isinstance(issued, int):
        raise ValueError("invalid local control-plane descriptor time")
    return NodeDescriptor.from_dict(descriptor, now=issued)


def current_node_descriptor(node: AnetNode, *, current_ms: int | None = None) -> NodeDescriptor:
    """Load or advance the public descriptor revision owned by one node home."""
    current = now_ms() if current_ms is None else current_ms
    path = node.config.home / CONTROL_STATE_FILENAME
    previous = _load_descriptor_state(path)
    capabilities = tuple(sorted(set(node.config.capabilities)))
    refresh = (
        previous is None
        or previous.node_id != node.node_id
        or previous.capabilities != capabilities
        or previous.expires_ms - current <= DESCRIPTOR_REFRESH_MS
    )
    if not refresh:
        previous.verify(now=current)
        return previous
    if previous is not None and previous.node_id != node.node_id:
        raise ValueError("local control-plane state belongs to another node")
    descriptor = issue_node_descriptor(
        node.identity,
        capabilities=capabilities,
        sequence=1 if previous is None else previous.sequence + 1,
        previous_digest=(
            bytes(32) if previous is None else previous.digest
        ),
        issued_ms=current,
        ttl_ms=DESCRIPTOR_TTL_MS,
    )
    atomic_json(
        path,
        {
            "version": CONTROL_STATE_VERSION,
            "descriptor": descriptor.to_dict(),
        },
        private=True,
    )
    return descriptor


def validate_peer_reachability(
    peer: PeerCard,
    descriptor: NodeDescriptor,
    record: ReachabilityRecord | None,
    *,
    current_ms: int | None = None,
) -> ReachabilityRecord | None:
    """Validate an Ahub reachability response against a pinned PeerCard."""

    current = now_ms() if current_ms is None else current_ms
    descriptor.verify(now=current)
    if descriptor.node_id != peer.node_id:
        raise ValueError("reachability descriptor belongs to another peer")
    if (
        descriptor.sign_public != peer.sign_public
        or descriptor.box_public != peer.box_public
    ):
        raise ValueError("reachability descriptor keys do not match pinned peer")
    if record is not None:
        record.verify(descriptor, now=current)
    return record


def _load_reachability_checkpoint(
    path: Path, *, node_id: str
) -> tuple[int, bytes]:
    if not path.exists():
        return 0, GENESIS_DIGEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid local reachability state") from exc
    if not isinstance(value, dict) or set(value) != {
        "version",
        "node_id",
        "sequence",
        "last_digest",
    }:
        raise ValueError("invalid local reachability state")
    if value["version"] != CONTROL_STATE_VERSION:
        raise ValueError("unsupported local reachability state")
    if not isinstance(value["node_id"], str) or value["node_id"] != node_id:
        raise ValueError("local reachability state belongs to another node")
    sequence = value["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("invalid local reachability sequence")
    if not isinstance(value["last_digest"], str):
        raise ValueError("invalid local reachability digest")
    try:
        last_digest = b64d(value["last_digest"])
    except Exception as exc:
        raise ValueError("invalid local reachability digest") from exc
    if len(last_digest) != 32:
        raise ValueError("local reachability digest must be 32 bytes")
    return sequence, last_digest


def _save_reachability_checkpoint(
    path: Path,
    *,
    node_id: str,
    record: ReachabilityRecord,
) -> None:
    atomic_json(
        path,
        {
            "version": CONTROL_STATE_VERSION,
            "node_id": node_id,
            "sequence": record.sequence,
            "last_digest": b64e(record.digest),
        },
        private=True,
    )


def _reachability_inputs(node: AnetNode) -> tuple[tuple[str, ...], bytes]:
    candidates = tuple(sorted(set(node.config.effective_addresses())))
    capabilities = tuple(sorted(set(node.config.capabilities)))
    capability_digest = hashlib.sha256(
        canonical_pack(list(capabilities))
    ).digest()
    return candidates, capability_digest


def current_node_reachability(
    node: AnetNode,
    descriptor: NodeDescriptor,
    *,
    current_ms: int | None = None,
    relay_reservation: str = "",
) -> ReachabilityRecord:
    """Prepare a short-lived reachability record for the current process.

    The sequence checkpoint is committed separately, after the Ahub accepts
    the record. A failed HTTP publish can therefore retry the same revision
    instead of consuming a revision that the rendezvous never saw.
    """

    current = now_ms() if current_ms is None else current_ms
    candidates, capability_digest = _reachability_inputs(node)
    session_id = node.control_session_id
    lock = node._reachability_lock
    with lock:
        cached = node._reachability_record
        if (
            cached is not None
            and cached.descriptor_digest == descriptor.digest
            and cached.session_id == session_id
            and cached.protocol_versions == REACHABILITY_PROTOCOL_VERSIONS
            and cached.candidates == candidates
            and cached.relay_reservation == str(relay_reservation)
            and cached.capability_digest == capability_digest
            and cached.expires_ms - current > REACHABILITY_REFRESH_MS
        ):
            return cached

        checkpoint = node.config.home / REACHABILITY_STATE_FILENAME
        if checkpoint.exists():
            sequence, previous_digest = _load_reachability_checkpoint(
                checkpoint,
                node_id=node.node_id,
            )
        else:
            sequence, previous_digest = 0, GENESIS_DIGEST
        record = issue_reachability_record(
            node.identity,
            descriptor,
            protocol_versions=REACHABILITY_PROTOCOL_VERSIONS,
            candidates=candidates,
            relay_reservation=str(relay_reservation),
            capability_digest=capability_digest,
            sequence=sequence + 1,
            previous_digest=previous_digest,
            session_id=session_id,
            issued_ms=current,
            ttl_ms=REACHABILITY_TTL_MS,
        )
        node._reachability_record = record
        return record


def commit_node_reachability(node: AnetNode, record: ReachabilityRecord) -> bool:
    """Persist a successfully published reachability revision."""

    if record.node_id != node.node_id:
        raise ValueError("reachability record belongs to another node")
    path = node.config.home / REACHABILITY_STATE_FILENAME
    with node._reachability_lock:
        if path.exists():
            sequence, last_digest = _load_reachability_checkpoint(
                path,
                node_id=node.node_id,
            )
            if sequence > record.sequence:
                return False
            if sequence == record.sequence:
                if last_digest != record.digest:
                    raise ValueError("reachability checkpoint revision conflict")
                return False
            if (
                record.sequence != sequence + 1
                or record.previous_digest != last_digest
            ):
                raise ValueError("reachability checkpoint chain is not contiguous")
        elif record.sequence != 1 or record.previous_digest != GENESIS_DIGEST:
            raise ValueError("reachability checkpoint is missing its predecessor")
        _save_reachability_checkpoint(
            path,
            node_id=node.node_id,
            record=record,
        )
        return True


def sync_ahub_once(
    node: AnetNode,
    config: AhubCarrierConfig,
    *,
    peer_ids: tuple[str, ...] | list[str] = (),
    push_peer_ids: tuple[str, ...] | list[str] | None = None,
    qos_allow: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    limit: int = 128,
    retry_after_ms: int = 300_000,
    path_id: str | None = None,
) -> dict[str, Any]:
    """Pull then push one direct-to-destination pass through an Ahub."""
    selected = set(str(item) for item in peer_ids if str(item))
    cards = [
        card for card in node.peers.all() if not selected or card.node_id in selected
    ]
    known = {card.node_id for card in cards}
    if selected - known:
        raise KeyError(
            f"unknown Ahub peer(s): {', '.join(sorted(selected - known))}"
        )
    push_selected = (
        known
        if push_peer_ids is None
        else {str(item) for item in push_peer_ids if str(item)}
    )
    if push_selected - known:
        raise KeyError(
            "unknown Ahub push peer(s): "
            + ", ".join(sorted(push_selected - known))
        )
    batch_limit = max(1, min(int(limit), 100))
    effective_path = config.path_id if path_id is None else str(path_id)
    client = AhubHTTPClient(
        config.base_url,
        node.identity,
        timeout_seconds=config.timeout,
        allow_insecure_http=config.allow_insecure_http,
    )
    descriptor = current_node_descriptor(node)
    descriptor_changed = client.publish_descriptor(descriptor)
    reachability = current_node_reachability(node, descriptor)
    reachability_changed = client.publish_reachability(reachability)
    commit_node_reachability(node, reachability)
    peer_reachability: list[dict[str, Any]] = []
    peer_reachability_errors: dict[str, str] = {}
    for card in cards:
        try:
            peer_descriptor, peer_record = client.lookup(card.node_id)
            validated = node.set_peer_reachability(
                card,
                peer_descriptor,
                peer_record,
            )
            peer_reachability.append(
                {
                    "peer_id": card.node_id,
                    "available": validated is not None,
                    "sequence": (
                        None if validated is None else validated.sequence
                    ),
                    "candidates": (
                        [] if validated is None else list(validated.candidates)
                    ),
                }
            )
        except Exception as exc:
            peer_reachability_errors[card.node_id] = str(exc)[:1000]
    stats: dict[str, Any] = {
        "carrier": "ahub-v1",
        "base_origin": config.base_url,
        "peers": len(cards),
        "descriptor_published": descriptor_changed,
        "reachability_published": reachability_changed,
        "reachability_sequence": reachability.sequence,
        "peer_reachability": peer_reachability,
        "peer_reachability_errors": peer_reachability_errors,
        "pulled_acks": 0,
        "acknowledged_settlements": 0,
        "pulled_packets": 0,
        "settled_packets": 0,
        "pushed_packets": 0,
        "existing_packets": 0,
        "rejected": 0,
        "unsupported_relay_packets": 0,
    }

    for card in cards:
        settlements = client.settlements(
            limit=batch_limit,
            destination_id=card.node_id,
        )
        for settlement in settlements:
            raw = node.store.get_packet(settlement.packet_id)
            if raw is None:
                continue
            settlement.verify(card.sign_public)
            if (
                settlement.uploader_id != node.node_id
                or settlement.destination_id != card.node_id
                or settlement.packet_sha256 != hashlib.sha256(raw).digest()
            ):
                raise ValueError("destination settlement does not match local packet")
            if (
                node.store.delivery_path_state(
                    settlement.packet_id,
                    card.node_id,
                    effective_path,
                )
                != "acked"
            ):
                node.store.mark_acked(
                    [settlement.packet_id],
                    card.node_id,
                    path_id=effective_path,
                )
                stats["pulled_acks"] += 1
            if client.acknowledge_settlement(settlement.packet_id):
                stats["acknowledged_settlements"] += 1
        claims = client.claim(
            limit=batch_limit,
            lease_ms=int(config.claim_lease_seconds * 1000),
            uploader_id=card.node_id,
        )
        for claim in claims:
            try:
                if claim.uploader_id != card.node_id or claim.depth != 0:
                    raise ValueError("invalid Ahub claim metadata")
                accepted = node.accept_carrier_packet(
                    claim.raw,
                    depth=1,
                    peer_id=card.node_id,
                )
                if accepted != claim.packet_id:
                    raise ValueError("Ahub claim packet ID mismatch")
                stats["pulled_packets"] += 1
                if client.settle_claim(claim):
                    stats["settled_packets"] += 1
            except Exception:
                # Do not delete a claim that was not durably accepted locally.
                # Its short lease makes it available for diagnosis/retry.
                stats["rejected"] += 1

    for card in cards:
        if card.node_id not in push_selected:
            continue
        pending = node.store.pending_for_peer(
            card.node_id,
            limit=batch_limit,
            retry_after_ms=max(0, int(retry_after_ms)),
            path_id=effective_path,
            qos_allow=qos_allow,
        )
        for item in pending:
            if (
                str(item["destination_id"]) != card.node_id
                or int(item["depth"]) != 0
            ):
                stats["unsupported_relay_packets"] += 1
                continue
            node.store.mark_attempt(
                [item["packet_id"]],
                card.node_id,
                path_id=effective_path,
            )
            receipt = client.submit(bytes(item["raw"]))
            if (
                receipt.packet_id != item["packet_id"]
                or receipt.destination_id != card.node_id
            ):
                raise ValueError("Ahub returned a mismatched custody receipt")
            node.store.mark_custodied(
                [item["packet_id"]],
                card.node_id,
                path_id=effective_path,
            )
            if receipt.stored:
                stats["pushed_packets"] += 1
            else:
                stats["existing_packets"] += 1

    stats["store"] = node.store.status()
    return stats
