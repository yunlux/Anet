from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ahub_http import AhubHTTPClient
from ..config import AhubCarrierConfig
from ..control_plane import NodeDescriptor, issue_node_descriptor
from ..encoding import atomic_json
from ..packet import now_ms

if TYPE_CHECKING:
    from ..node import AnetNode


CONTROL_STATE_VERSION = 1
CONTROL_STATE_FILENAME = "control-state.json"
DESCRIPTOR_TTL_MS = 30 * 24 * 60 * 60 * 1000
DESCRIPTOR_REFRESH_MS = 7 * 24 * 60 * 60 * 1000


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
    stats: dict[str, Any] = {
        "carrier": "ahub-v1",
        "base_origin": config.base_url,
        "peers": len(cards),
        "descriptor_published": descriptor_changed,
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
