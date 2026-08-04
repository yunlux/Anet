from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from .ahub_http import AhubHTTPClient
from .bundle import create_bundle, import_bundle
from .carriers.ahub import validate_peer_reachability
from .carriers.directory import sync_directory_once
from .config import (
    AhubCarrierConfig,
    DirectoryCarrierConfig,
    DirectDialerConfig,
    DirectProxyConfig,
    NodeConfig,
    RoutingConfig,
    WebDAVCarrierConfig,
    initialize_node,
)
from .control_plane import (
    HUMAN_DEVICE_GRANT_TYPE,
    HUMAN_DEVICE_REVOCATION_TYPE,
    NODE_DESCRIPTOR_TYPE,
    ControlPlaneStore,
    HumanDeviceGrant,
    HumanDeviceRevocation,
    NodeDescriptor,
)
from .discord_social import (
    DiscordSocialBridge,
    DiscordSocialConfig,
    DiscordSocialStore,
    discord_social_config_path,
    discord_social_database_path,
    discord_social_key_path,
)
from .discord_relation_projection import DiscordRelationshipProjector
from .encoding import b64e
from .experiments import monitor_probes, run_probe_series
from .friendship import (
    FriendAcceptance,
    FriendInvite,
    decode_friend_code,
    encode_friend_code,
    read_friend_code,
    write_friend_code,
)
from .identity import Identity, PeerCard
from .locator import parse_locator, validate_locator_context
from .node import AnetNode
from .pairing import PairOffer, PairResponse
from .packet import inspect_packet
from .peers import PeerBook
from .prekeys import PreKeyBundle, generate_prekey_bundle, import_prekey_bundle
from .relation_activity import RelationshipActivityFeed
from .relation_advisor import RelationshipAdvisor, RelationshipSuggestion
from .relation_decisions import RelationshipDecisionManager
from .relationship_disclosures import (
    RELATIONSHIP_DISCLOSURE_KIND,
    RelationshipDisclosure,
    RelationshipDisclosureBook,
)
from .relationship_disclosure_schedules import (
    RelationshipDisclosureScheduleBook,
)
from .relationship_disclosure_recovery import (
    RelationshipDisclosureGapNoticeBook,
)
from .reported_relationship_views import (
    ReportedRelationshipViewProjector,
)
from .remote_control import (
    _normalise_trusted_keys,
    run_supervisor,
    sync_remote_control,
    verify_remote_control,
)
from .supervisor_health import inspect_supervisor_health
from .relationship_claims import (
    MutualRelationshipClaim,
    RelationshipClaimBook,
    RelationshipClaimWithdrawal,
    RelationshipProposal,
)
from .relations import ActorObservation, ActorProof, RELATION_CIRCLES, RelationshipBook
from .scheduling import AdaptiveSchedule
from .social import SocialPolicy, SocialThreshold
from .store import PacketStore
from .wake import WakeBridge


LOGGER = logging.getLogger(__name__)


def default_home() -> Path:
    return Path(os.environ.get("ANET_HOME", "~/.anet")).expanduser().resolve()


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": b64e(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True))


def _load_runtime(home: Path) -> tuple[NodeConfig, Identity, PeerBook, PacketStore]:
    config = NodeConfig.load(home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    store = PacketStore(config.database_path)
    if store.unscoped_local_prekey_count() and len(peers.all()) == 1:
        store.scope_legacy_local_prekeys(peers.all()[0].node_id)
    return config, identity, peers, store


def cmd_init(args: argparse.Namespace) -> int:
    config = initialize_node(
        args.home,
        label=args.label,
        listen_host=args.host,
        listen_port=args.port,
        advertise=args.advertise,
        locator_contexts=args.locator_context,
    )
    identity = Identity.load(config.identity_path)
    card = identity.card(
        addresses=config.effective_addresses(), capabilities=config.capabilities
    )
    card.save(config.home / "card.json")
    _print_json(
        {
            "initialized": True,
            "home": str(config.home),
            "node_id": identity.node_id,
            "card": str(config.home / "card.json"),
        }
    )
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    addresses = () if args.keys_only else config.effective_addresses()
    card = identity.card(addresses=addresses, capabilities=config.capabilities)
    if args.out:
        card.save(args.out)
        _print_json({"node_id": card.node_id, "card": str(Path(args.out).resolve())})
    else:
        _print_json(card.to_dict())
    return 0


def cmd_peer_add(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    card = PeerCard.load(args.card)
    peers.add(card)
    _print_json(
        {
            "added": card.node_id,
            "label": card.label,
            "addresses": list(card.addresses),
            "trusted_peers": len(peers.all()),
        }
    )
    return 0


def cmd_peer_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    _print_json([card.to_dict() for card in peers.all()])
    return 0


def cmd_peer_reachability(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    peer = peers.require(args.peer)
    carriers = [
        carrier
        for carrier in config.ahub_carriers
        if not args.carrier or carrier.name == args.carrier
    ]
    if not carriers:
        raise KeyError(f"unknown Ahub carrier: {args.carrier}")
    sources: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    dynamic_candidates: list[str] = []
    for carrier in carriers:
        if not carrier.enabled:
            errors[carrier.name] = "Ahub carrier is disabled"
            continue
        try:
            client = AhubHTTPClient(
                carrier.base_url,
                identity,
                timeout_seconds=carrier.timeout,
                allow_insecure_http=carrier.allow_insecure_http,
            )
            descriptor, record = client.lookup(peer.node_id)
            record = validate_peer_reachability(peer, descriptor, record)
            if record is not None:
                dynamic_candidates.extend(record.candidates)
            sources.append(
                {
                    "carrier": carrier.name,
                    "base_origin": carrier.base_url,
                    "descriptor": {
                        "sequence": descriptor.sequence,
                        "digest": b64e(descriptor.digest),
                        "issued_ms": descriptor.issued_ms,
                        "expires_ms": descriptor.expires_ms,
                        "capabilities": list(descriptor.capabilities),
                    },
                    "reachability": (
                        None if record is None else record.to_dict()
                    ),
                    "candidates": (
                        [] if record is None else list(record.candidates)
                    ),
                }
            )
        except Exception as exc:
            errors[carrier.name] = str(exc)[:1000]
    candidates = list(dict.fromkeys((*dynamic_candidates, *peer.addresses)))
    _print_json(
        {
            "ok": bool(sources),
            "peer_id": peer.node_id,
            "static_card_addresses": list(peer.addresses),
            "effective_candidates": candidates,
            "sources": sources,
            "errors": errors,
        }
    )
    return 0 if sources else 1


def cmd_peer_revoke(args: argparse.Namespace) -> int:
    peer_id = str(args.peer).strip()
    if str(args.confirm).strip() != peer_id:
        raise ValueError("--confirm must exactly match the peer Node ID")
    config, identity, peers, store = _load_runtime(args.home)
    try:
        record = peers.revoke(peer_id, reason=args.reason)
        cleanup = store.revoke_peer(peer_id)
        relation = RelationshipBook(
            config.relationships_path,
            own_actor_id=identity.node_id,
        ).revoke_actor(
            peer_id,
            evidence_ref=f"revocation:{record['revoked_ms']}",
        )
        _print_json(
            {
                "revoked": record,
                "cleanup": cleanup,
                "relationship": (
                    relation.to_dict() if relation is not None else None
                ),
                "revocations_file": str(peers.revocations_path.resolve()),
                "restart_required": False,
                "warning": (
                    "revocation stops new trust and queued work but cannot undo "
                    "side effects already executed by an Agent"
                ),
            }
        )
    finally:
        store.close()
    return 0


def cmd_peer_revocations(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    _print_json(peers.revocations())
    return 0


def cmd_control_import(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    try:
        value = json.loads(Path(args.object).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("control object must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("control object must be a JSON map")
    object_type = str(value.get("object_type", ""))
    with ControlPlaneStore(config.control_database_path) as control:
        if object_type == NODE_DESCRIPTOR_TYPE:
            descriptor = NodeDescriptor.from_dict(value)
            changed = control.accept_descriptor(descriptor)
            result = {
                "object_type": object_type,
                "node_id": descriptor.node_id,
                "sequence": descriptor.sequence,
                "changed": changed,
            }
        elif object_type in {
            HUMAN_DEVICE_GRANT_TYPE,
            HUMAN_DEVICE_REVOCATION_TYPE,
        }:
            device_node_id = str(value.get("device_node_id", ""))
            descriptor = control.current_descriptor(device_node_id)
            if descriptor is None:
                raise ValueError(
                    "import the current device NodeDescriptor before its human object"
                )
            if object_type == HUMAN_DEVICE_GRANT_TYPE:
                grant = HumanDeviceGrant.from_dict(value, descriptor)
                changed = control.accept_human_grant(grant, descriptor)
                result = {
                    "object_type": object_type,
                    "human_id": grant.human_id,
                    "device_node_id": grant.device_node_id,
                    "sequence": grant.sequence,
                    "capabilities": list(grant.capabilities),
                    "changed": changed,
                }
            else:
                revocation = HumanDeviceRevocation.from_dict(
                    value,
                    descriptor,
                )
                changed = control.accept_human_revocation(
                    revocation,
                    descriptor,
                )
                result = {
                    "object_type": object_type,
                    "human_id": revocation.human_id,
                    "device_node_id": revocation.device_node_id,
                    "sequence": revocation.sequence,
                    "revoked": True,
                    "changed": changed,
                }
        else:
            raise ValueError("unsupported control object type")
    _print_json(result)
    return 0


def cmd_control_device(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    with ControlPlaneStore(config.control_database_path) as control:
        revoked = control.is_human_device_revoked(
            args.human,
            args.device,
        )
        grant = None if revoked else control.human_device_grant(
            args.human,
            args.device,
        )
    _print_json(
        {
            "human_id": args.human,
            "device_node_id": args.device,
            "revoked": revoked,
            "authorized": grant is not None,
            "capabilities": list(grant.capabilities) if grant else [],
            "expires_ms": grant.expires_ms if grant else 0,
        }
    )
    return 0


def cmd_pair_offer(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    addresses = () if args.keys_only else config.effective_addresses()
    card = identity.card(addresses=addresses, capabilities=config.capabilities)
    offer = PairOffer.create(identity, card, ttl_seconds=args.ttl)
    offer.save(args.out)
    _print_json(
        {
            "offer_id": offer.offer_id,
            "node_id": identity.node_id,
            "expires_ms": offer.expires_ms,
            "path": str(Path(args.out).resolve()),
        }
    )
    return 0


def cmd_pair_accept(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    offer = PairOffer.load(args.offer)
    if offer.card.node_id == identity.node_id:
        raise ValueError("cannot accept a local pairing offer")
    addresses = () if args.keys_only else config.effective_addresses()
    card = identity.card(addresses=addresses, capabilities=config.capabilities)
    response = PairResponse.create(offer, identity, card)
    response.verify(offer)
    response.save(args.out)
    peers.add(offer.card)
    _print_json(
        {
            "accepted": offer.card.node_id,
            "offer_id": offer.offer_id,
            "response": str(Path(args.out).resolve()),
            "trusted_peers": len(peers.all()),
        }
    )
    return 0


def cmd_pair_complete(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    offer = PairOffer.load(args.offer)
    if (
        offer.card.node_id != identity.node_id
        or offer.card.sign_public != identity.sign_public
        or offer.card.box_public != identity.box_public
    ):
        raise ValueError("pairing offer was not created by this local identity")
    response = PairResponse.load(args.response)
    response.verify(offer)
    peers.add(response.card)
    _print_json(
        {
            "completed": response.card.node_id,
            "label": response.card.label,
            "offer_id": offer.offer_id,
            "addresses": list(response.card.addresses),
            "trusted_peers": len(peers.all()),
        }
    )
    return 0


def cmd_friend_qr(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    addresses = () if args.keys_only else config.effective_addresses()
    card = identity.card(addresses=addresses, capabilities=config.capabilities)
    invite = FriendInvite.create(
        identity,
        card,
        ttl_seconds=args.ttl,
    )
    payload = encode_friend_code(invite)
    write_friend_code(payload, args.out)
    _print_json(
        {
            "type": "friend_invite",
            "offer_id": invite.offer.offer_id,
            "node_id": identity.node_id,
            "expires_ms": invite.offer.expires_ms,
            "path": str(Path(args.out).resolve()),
            "relationship": invite.relationship,
        }
    )
    return 0


def cmd_friend_scan(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    payload = read_friend_code(args.source)
    code = decode_friend_code(payload)
    if isinstance(code, FriendInvite):
        if code.offer.card.node_id == identity.node_id:
            raise ValueError("cannot scan a local friend invite")
        if args.out is None:
            raise ValueError("--out is required when accepting a friend invite")
        addresses = () if args.keys_only else config.effective_addresses()
        local_card = identity.card(
            addresses=addresses,
            capabilities=config.capabilities,
        )
        acceptance = FriendAcceptance.create(code, identity, local_card)
        response_payload = encode_friend_code(acceptance)
        # Render the response before changing persistent trust. Missing optional
        # QR support or an unwritable output therefore fails without mutation.
        write_friend_code(response_payload, args.out)
        peers.add(code.offer.card)
        relation = relationships.confirm_friend(
            code.offer.card,
            evidence_ref=f"friend:{code.offer.offer_id}:accepted",
        )
        _print_json(
            {
                "type": "friend_acceptance",
                "accepted": code.offer.card.node_id,
                "offer_id": code.offer.offer_id,
                "response": str(Path(args.out).resolve()),
                "circle": relation.circle,
                "subject_ref": relation.subject_ref,
                "trusted_peers": len(peers.all()),
            }
        )
        return 0

    if args.out is not None:
        raise ValueError("--out is only used when accepting a friend invite")
    invite = code.invite
    if (
        invite.offer.card.node_id != identity.node_id
        or invite.offer.card.sign_public != identity.sign_public
        or invite.offer.card.box_public != identity.box_public
    ):
        raise ValueError("friend acceptance is not for this local identity")
    peers.add(code.response.card)
    relation = relationships.confirm_friend(
        code.response.card,
        evidence_ref=f"friend:{invite.offer.offer_id}:completed",
    )
    _print_json(
        {
            "type": "friend_completed",
            "completed": code.response.card.node_id,
            "offer_id": invite.offer.offer_id,
            "circle": relation.circle,
            "subject_ref": relation.subject_ref,
            "trusted_peers": len(peers.all()),
        }
    )
    return 0


def _local_relation_model(
    config: NodeConfig,
    identity: Identity,
) -> dict[str, Any]:
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    model = relationships.snapshot()
    claims = RelationshipClaimBook(config.relationship_claims_path)
    model["mutual_relationship_claims"] = [
            {
                "claim_id": claim.claim_id,
                "participant_actor_ids": [
                    claim.proposal.proposer_card.node_id,
                    claim.accepter_card.node_id,
                ],
                "participant_subject_refs": [
                    {
                        "actor_id": actor_id,
                        "subject_ref": (
                            subject.subject_ref if subject is not None else None
                        ),
                    }
                    for actor_id in (
                        claim.proposal.proposer_card.node_id,
                        claim.accepter_card.node_id,
                    )
                    for subject in (relationships.primary_subject(actor_id),)
                ],
                "circle": claim.circle,
                "labels": list(claim.labels),
                "accepted_ms": claim.accepted_ms,
                "active": claims.is_active(claim.claim_id),
                "withdrawals": [
                    {
                        "withdrawal_id": withdrawal.withdrawal_id,
                        "withdrawing_actor_id": withdrawal.withdrawing_card.node_id,
                        "withdrawn_ms": withdrawal.withdrawn_ms,
                    }
                    for withdrawal in claims.withdrawals_for(claim.claim_id)
                ],
                "authorization_effect": "none",
            }
        for claim in claims.all()
    ]
    model["relationship_suggestions"] = [
        item.to_dict() for item in RelationshipAdvisor.advise(model)
    ]
    model["relationship_activity"] = RelationshipActivityFeed.read(
        model,
        limit=500,
        tail=True,
    ).to_dict()
    return model


def cmd_relation_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    if args.model:
        _print_json(_local_relation_model(config, identity))
    else:
        relationships = RelationshipBook(
            config.relationships_path,
            own_actor_id=identity.node_id,
        )
        _print_json([record.to_dict() for record in relationships.all()])
    return 0


def cmd_relation_observe_actor(args: argparse.Namespace) -> int:
    """Add one explicitly local, opaque external Actor observation.

    A Node Actor must arrive through a verified Peer Card. This narrow command
    exists for sources such as a locally known person or external Agent that
    do not own an Anet Node yet.  Its proof is deliberately operator-attested,
    so it cannot be confused with platform or cryptographic verification.
    """

    actor_id = str(args.actor).strip().lower()
    if not actor_id.startswith("act_"):
        raise ValueError(
            "relation-observe-actor accepts only opaque typed act_ Actor IDs; "
            "observe Node Actors through their signed Peer Card"
        )
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    current = int(time.time() * 1000)
    observation = ActorObservation(
        actor_id=actor_id,
        actor_kind=args.kind,
        actor_label=(
            str(args.label).strip()
            if args.label is not None
            else f"{args.kind} · {actor_id[-6:]}"
        ),
        proof=ActorProof(
            proof_type="operator.local.v1",
            scope="operator-attested",
            issuer_actor_id=identity.node_id,
            evidence_ref=args.evidence,
            observed_ms=current,
        ),
    )
    subject = relationships.observe_typed_actor(
        observation,
        subject_confidence=args.confidence,
        now=current,
    )
    actor = relationships.actor(actor_id)
    if actor is None:
        raise RuntimeError("observed Actor was not persisted")
    _print_json(
        {
            "actor": actor.to_dict(),
            "subject": subject.to_dict(),
            "relationship": relationships.relationship(subject.subject_ref).to_dict(),
            "proof_scope": "operator-attested",
            "identity_assertion": "none",
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_actor_revoke(args: argparse.Namespace) -> int:
    """Withdraw one locally observed external Actor without erasing a Subject."""

    actor_id = str(args.actor).strip().lower()
    if not actor_id.startswith("act_"):
        raise ValueError(
            "relation-actor-revoke accepts only opaque typed act_ Actor IDs; "
            "use peer-revoke for a signed Node Actor"
        )
    if args.confirm != actor_id:
        raise ValueError("--confirm must exactly match the Actor ID")
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    before = relationships.actor(actor_id)
    if before is None:
        raise ValueError("Actor is not observed in this local relationship book")
    relation = relationships.revoke_actor(
        actor_id,
        evidence_ref=args.reason,
    )
    actor = relationships.actor(actor_id)
    if actor is None:
        raise RuntimeError("revoked Actor was not persisted")
    _print_json(
        {
            "actor": actor.to_dict(),
            "relationship": relation.to_dict() if relation is not None else None,
            "already_revoked": before.state == "revoked",
            "subject_changed": False,
            "circle_changed": False,
            "trust_changed": False,
            "peerbook_changed": False,
            "authorization_effect": "none",
        }
    )
    return 0


def _suggestion_command(item: RelationshipSuggestion) -> list[str]:
    evidence = f"suggestion:{item.suggestion_id}"
    if item.suggestion_type == "circle.advance":
        command = [
            "relation-circle",
            item.subject_ref,
            item.proposed_circle,
            "--confidence",
            str(item.confidence),
            "--evidence",
            evidence,
        ]
        return command
    if (
        item.suggestion_type == "context-trust.review"
        and item.proposed_estimate is not None
    ):
        return [
            "relation-trust",
            item.subject_ref,
            item.context,
            "--estimate",
            str(item.proposed_estimate),
            "--confidence",
            str(item.confidence),
            "--evidence",
            evidence,
        ]
    raise ValueError("unsupported relationship suggestion")


def cmd_relation_suggest(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    suggestions = RelationshipAdvisor.advise(
        relationships.snapshot(),
        subject_ref=args.subject or "",
    )
    _print_json(
        {
            "suggestions": [
                {
                    **item.to_dict(),
                    "decision_commands": {
                        "accept": [
                            "relation-decide",
                            item.suggestion_id,
                            "accepted",
                            "--reason",
                            "<RATIONALE_CODE>",
                        ],
                        "reject": [
                            "relation-decide",
                            item.suggestion_id,
                            "rejected",
                            "--reason",
                            "<RATIONALE_CODE>",
                        ],
                    },
                    "proposed_mutation": _suggestion_command(item),
                }
                for item in suggestions
            ],
            "note": (
                "Suggestions do not change relationships. Use relation-decide "
                "for an auditable current-basis decision; neither a suggestion "
                "nor its decision changes PeerBook trust or authorization"
            ),
        }
    )
    return 0


def cmd_relation_activity(args: argparse.Namespace) -> int:
    wait_seconds = float(args.wait)
    if not 0 <= wait_seconds <= 30:
        raise ValueError("relationship activity wait must be 0-30 seconds")
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    deadline = time.monotonic() + wait_seconds
    while True:
        relationships.reload()
        page = RelationshipActivityFeed.read(
            relationships.snapshot(),
            after=args.after or "",
            limit=args.limit,
            subject_ref=args.subject or "",
        )
        if page.activities or time.monotonic() >= deadline:
            _print_json(page.to_dict())
            return 0
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def cmd_relation_disclose(args: argparse.Namespace) -> int:
    if not 1 <= int(args.limit) <= 100:
        raise ValueError("relationship disclosure limit must be 1-100")
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    page = RelationshipActivityFeed.read(
        relationships.snapshot(),
        after=args.after or "",
        limit=args.limit,
        subject_ref=args.subject or "",
    )
    if not page.activities:
        raise ValueError("no new relationship activity to disclose")
    disclosure = RelationshipDisclosure.create(
        page,
        audience_actor_id=args.destination,
    )
    node = AnetNode(config)
    try:
        packet_id = node.queue(
            args.destination,
            kind=RELATIONSHIP_DISCLOSURE_KIND,
            body=disclosure.to_dict(),
            ttl_seconds=args.ttl,
            qos="normal",
        )
    finally:
        node.close()
    _print_json(
        {
            "queued": packet_id,
            "destination": args.destination,
            "kind": RELATIONSHIP_DISCLOSURE_KIND,
            "disclosure_id": disclosure.disclosure_id,
            "activities": len(disclosure.activities),
            "next_cursor": disclosure.next_cursor,
            "has_more": disclosure.has_more,
            "privacy": "content-free",
            "visibility": "audience-private",
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_disclosure_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    book = RelationshipDisclosureBook(
        config.relationship_disclosures_path,
        own_actor_id=identity.node_id,
    )
    _print_json(
        {
            "observer_actor_id": identity.node_id,
            "received": [
                item.to_dict()
                for item in book.all(
                    sender_actor_id=args.sender or "",
                    limit=args.limit,
                )
            ],
            "projection_into_local_relations": False,
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_reported_view(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    book = RelationshipDisclosureBook(
        config.relationship_disclosures_path,
        own_actor_id=identity.node_id,
    )
    _print_json(
        ReportedRelationshipViewProjector.project(
            book,
            sender_actor_id=args.sender,
            series_id=args.series or "",
            subject_ref=args.subject or "",
            include_activities=args.include_activities,
            activity_limit=args.limit,
        )
    )
    return 0


def cmd_relation_dashboard(args: argparse.Namespace) -> int:
    """Export one local model and optionally one separately attributed report."""

    if args.series and not args.reported:
        raise ValueError("--series requires --reported")
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    reported_view = None
    if args.reported:
        disclosures = RelationshipDisclosureBook(
            config.relationship_disclosures_path,
            own_actor_id=identity.node_id,
        )
        reported_view = ReportedRelationshipViewProjector.project(
            disclosures,
            sender_actor_id=args.reported,
            series_id=args.series or "",
            include_activities=False,
        )
    _print_json(
        {
            "version": 1,
            "type": "anet.relationship-dashboard.v1",
            "observer_actor_id": identity.node_id,
            "local_model": _local_relation_model(config, identity),
            "reported_view": reported_view,
            "privacy": "local-dashboard-file",
            "projection_into_local_relations": False,
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_disclosure_schedule_add(
    args: argparse.Namespace,
) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    PeerBook(
        config.peers_path,
        own_node_id=identity.node_id,
    ).require(args.destination)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    model = relationships.snapshot()
    subject_ref = args.subject or ""
    if subject_ref:
        # Validate that this is an active observer-local Subject reference.
        RelationshipActivityFeed.read(
            model,
            subject_ref=subject_ref,
            limit=1,
        )
    cursor = ""
    if not args.include_history:
        cursor = RelationshipActivityFeed.read(
            model,
            limit=1,
            tail=True,
        ).next_cursor
    book = RelationshipDisclosureScheduleBook(
        config.relationship_disclosure_schedules_path,
        own_actor_id=identity.node_id,
    )
    item = book.create(
        args.destination,
        cursor=cursor,
        subject_ref=subject_ref,
        interval_seconds=args.interval,
        batch_limit=args.limit,
        packet_ttl_seconds=args.packet_ttl,
        lifetime_seconds=args.lifetime,
        baseline=(
            "history-start"
            if args.include_history
            else "current-cursor"
        ),
    )
    _print_json(
        {
            **item.to_dict(),
            "state": item.state(),
            "history_mode": (
                "explicit-replay" if args.include_history else "start-now"
            ),
            "note": (
                "This observer-local instruction permits only bounded "
                "relationship disclosure. The audience cannot pull or "
                "expand its scope."
            ),
        }
    )
    return 0


def cmd_relation_disclosure_schedule_list(
    args: argparse.Namespace,
) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    book = RelationshipDisclosureScheduleBook(
        config.relationship_disclosure_schedules_path,
        own_actor_id=identity.node_id,
    )
    _print_json(
        {
            "observer_actor_id": identity.node_id,
            "schedules": [
                {**item.to_dict(), "state": item.state()}
                for item in book.all()
            ],
            "audience_pull": False,
            "authorization_effect": "disclosure-only",
        }
    )
    return 0


def cmd_relation_disclosure_schedule_revoke(
    args: argparse.Namespace,
) -> int:
    if args.confirm != args.schedule:
        raise ValueError(
            "schedule revocation confirmation must exactly match schedule ID"
        )
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    book = RelationshipDisclosureScheduleBook(
        config.relationship_disclosure_schedules_path,
        own_actor_id=identity.node_id,
    )
    item = book.revoke(
        args.schedule,
        reason=args.reason or "",
    )
    _print_json({**item.to_dict(), "state": item.state()})
    return 0


def cmd_relation_disclosure_schedule_run(
    args: argparse.Namespace,
) -> int:
    config = NodeConfig.load(args.home)
    node = AnetNode(config)
    try:
        results = node.run_relationship_disclosure_schedules_once(
            schedule_id=args.schedule or "",
            force=bool(args.schedule),
        )
    finally:
        node.close()
    _print_json(
        {
            "results": results,
            "forced": bool(args.schedule),
            "audience_pull": False,
        }
    )
    return 0


def cmd_relation_disclosure_gap_notice(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    node = AnetNode(config)
    try:
        result = node.queue_relationship_disclosure_gap_notice(
            args.sender,
            args.series,
        )
    finally:
        node.close()
    _print_json(result)
    return 0


def cmd_relation_disclosure_gap_notice_list(
    args: argparse.Namespace,
) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    book = RelationshipDisclosureGapNoticeBook(
        config.relationship_disclosure_gap_notices_path,
        own_actor_id=identity.node_id,
    )
    _print_json(
        {
            "observer_actor_id": identity.node_id,
            "received": [
                item.to_dict()
                for item in book.all(
                    reporter_actor_id=args.reporter or "",
                    limit=args.limit,
                )
            ],
            "meaning": "delivery-gap-observed",
            "requested_action": "none",
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_disclosure_gap_retransmit(
    args: argparse.Namespace,
) -> int:
    config = NodeConfig.load(args.home)
    node = AnetNode(config)
    try:
        result = node.retransmit_relationship_disclosure_gap(args.notice)
    finally:
        node.close()
    _print_json(result)
    return 0


def cmd_relation_decide(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    record = RelationshipDecisionManager.decide(
        relationships,
        args.suggestion,
        args.decision,
        rationale=args.reason,
    )
    _print_json(record.to_dict())
    return 0


def cmd_relation_decision_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    _print_json(
        [
            item.to_dict()
            for item in relationships.suggestion_decisions(
                subject_ref=args.subject or "",
            )
        ]
    )
    return 0


def cmd_relation_link(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    subject = relationships.link_actor(
        args.actor,
        args.subject,
        confidence=args.confidence,
        evidence_ref=args.evidence,
    )
    _print_json(subject.to_dict())
    return 0


def cmd_relation_circle(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    relationship = relationships.set_circle(
        args.subject,
        args.circle,
        confidence=args.confidence,
        evidence_ref=args.evidence,
        labels=args.label,
    )
    _print_json(relationship.to_dict())
    return 0


def cmd_relation_trust(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    relationship = relationships.set_context_trust(
        args.subject,
        args.context,
        estimate=args.estimate,
        confidence=args.confidence,
        evidence_ref=args.evidence,
    )
    _print_json(relationship.to_dict())
    return 0


def cmd_relation_end(args: argparse.Namespace) -> int:
    return _cmd_relation_state(
        args,
        state="ended",
        action="ended",
        mutate=lambda relationships, subject_ref: relationships.end_relationship(
            subject_ref,
            evidence_ref=args.reason,
        ),
    )


def cmd_relation_pause(args: argparse.Namespace) -> int:
    return _cmd_relation_state(
        args,
        state="dormant",
        action="paused",
        mutate=lambda relationships, subject_ref: relationships.pause_relationship(
            subject_ref,
            evidence_ref=args.reason,
        ),
    )


def _cmd_relation_state(
    args: argparse.Namespace,
    *,
    state: str,
    action: str,
    mutate: Any,
) -> int:
    subject_ref = str(args.subject).strip()
    if args.confirm != subject_ref:
        raise ValueError("--confirm must exactly match the Subject reference")
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    existing = relationships.relationship(subject_ref)
    if existing is None:
        raise ValueError("Subject hypothesis is not present in this local relationship book")
    relationship = mutate(relationships, subject_ref)
    _print_json(
        {
            "relationship": relationship.to_dict(),
            f"already_{action}": existing.state == state,
            "subject_changed": False,
            "actors_changed": False,
            "claims_changed": False,
            "trust_changed": False,
            "peerbook_changed": False,
            "authorization_effect": "none",
        }
    )
    return 0


def cmd_relation_propose(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    proposal = RelationshipProposal.create(
        identity,
        identity.card(),
        peer_actor_id=args.peer,
        circle=args.circle,
        labels=args.label,
        ttl_seconds=args.ttl,
    )
    proposal.save(args.out)
    _print_json(
        {
            "proposal_id": proposal.proposal_id,
            "peer_actor_id": proposal.peer_actor_id,
            "circle": proposal.circle,
            "labels": list(proposal.labels),
            "expires_ms": proposal.expires_ms,
            "path": str(Path(args.out).resolve()),
            "warning": (
                "the proposal is public signed relationship evidence, "
                "not trust or authorization"
            ),
        }
    )
    return 0


def _project_mutual_relationship_claim(
    config: NodeConfig,
    identity: Identity,
    claim: MutualRelationshipClaim,
) -> dict[str, Any]:
    claim.verify()
    peer_card = claim.peer_card_for(identity.node_id)
    claim_book = RelationshipClaimBook(config.relationship_claims_path)
    stored = claim_book.add(claim)
    relationship = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    ).confirm_mutual_relationship(
        peer_card,
        claim.circle,
        evidence_ref=f"mutual:{claim.claim_id}",
        labels=claim.labels,
    )
    return {
        "claim_id": claim.claim_id,
        "stored": stored,
        "peer_actor_id": peer_card.node_id,
        "circle": relationship.circle,
        "subject_ref": relationship.subject_ref,
        "labels": list(claim.labels),
        "trust_changed": False,
        "capabilities_granted": [],
    }


def cmd_relation_accept(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    proposal = RelationshipProposal.load(args.proposal)
    claim = MutualRelationshipClaim.create(
        proposal,
        identity,
        identity.card(),
    )
    claim.save(args.out)
    result = _project_mutual_relationship_claim(config, identity, claim)
    result["path"] = str(Path(args.out).resolve())
    _print_json(result)
    return 0


def cmd_relation_import(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    claim = MutualRelationshipClaim.load(args.claim)
    _print_json(_project_mutual_relationship_claim(config, identity, claim))
    return 0


def cmd_relation_claim_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    claims = RelationshipClaimBook(config.relationship_claims_path)
    _print_json(
        [
            {
                "claim_id": claim.claim_id,
                "proposer_actor_id": claim.proposal.proposer_card.node_id,
                "accepter_actor_id": claim.accepter_card.node_id,
                "circle": claim.circle,
                "labels": list(claim.labels),
                "accepted_ms": claim.accepted_ms,
                "active": claims.is_active(claim.claim_id),
                "withdrawals": [
                    {
                        "withdrawal_id": withdrawal.withdrawal_id,
                        "withdrawing_actor_id": withdrawal.withdrawing_card.node_id,
                        "withdrawn_ms": withdrawal.withdrawn_ms,
                    }
                    for withdrawal in claims.withdrawals_for(claim.claim_id)
                ],
            }
            for claim in claims.all()
        ]
    )
    return 0


def _record_mutual_relationship_withdrawal(
    config: NodeConfig,
    identity: Identity,
    claim: MutualRelationshipClaim,
    withdrawal: RelationshipClaimWithdrawal,
) -> dict[str, Any]:
    claim_book = RelationshipClaimBook(config.relationship_claims_path)
    stored = claim_book.add_withdrawal(withdrawal)
    peer_card = claim.peer_card_for(identity.node_id)
    relationship = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    ).record_mutual_relationship_withdrawal(
        peer_card,
        claim_id=claim.claim_id,
        withdrawing_actor_id=withdrawal.withdrawing_card.node_id,
        evidence_ref=f"mutual-withdrawal:{withdrawal.withdrawal_id}",
    )
    return {
        "claim_id": claim.claim_id,
        "withdrawal_id": withdrawal.withdrawal_id,
        "stored": stored,
        "active": claim_book.is_active(claim.claim_id),
        "withdrawing_actor_id": withdrawal.withdrawing_card.node_id,
        "peer_actor_id": peer_card.node_id,
        "subject_ref": relationship.subject_ref,
        "relationship_changed": False,
        "trust_changed": False,
        "capabilities_granted": [],
        "authorization_effect": "none",
    }


def cmd_relation_claim_withdraw(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    claim_book = RelationshipClaimBook(config.relationship_claims_path)
    claim = claim_book.claim(args.claim_id)
    if claim is None:
        raise ValueError("relationship claim is not stored locally")
    withdrawal = next(
        (
            item
            for item in claim_book.withdrawals_for(claim.claim_id)
            if item.withdrawing_card.node_id == identity.node_id
        ),
        None,
    )
    if withdrawal is None:
        withdrawal = RelationshipClaimWithdrawal.create(
            claim,
            identity,
            identity.card(),
        )
    withdrawal.save(args.out)
    result = _record_mutual_relationship_withdrawal(
        config,
        identity,
        claim,
        withdrawal,
    )
    result["path"] = str(Path(args.out).resolve())
    _print_json(result)
    return 0


def cmd_relation_claim_withdraw_import(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    claim_book = RelationshipClaimBook(config.relationship_claims_path)
    value = json.loads(Path(args.withdrawal).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("relationship claim withdrawal must be a JSON object")
    claim_id = str(value.get("claim_id", ""))
    claim = claim_book.claim(claim_id)
    if claim is None:
        raise ValueError("relationship claim withdrawal references an unknown local claim")
    withdrawal = RelationshipClaimWithdrawal.load(args.withdrawal, claim)
    _print_json(
        _record_mutual_relationship_withdrawal(
            config,
            identity,
            claim,
            withdrawal,
        )
    )
    return 0


def _subject_transition_output(
    relationships: RelationshipBook,
    transition: Any,
) -> dict[str, Any]:
    replacements = []
    for subject_ref in transition.replacement_subject_refs:
        subject = relationships.subject(subject_ref)
        relationship = relationships.relationship(subject_ref)
        if subject is None or relationship is None:
            raise RuntimeError("Subject transition replacement was not persisted")
        replacements.append(
            {
                "subject": subject.to_dict(),
                "relationship": relationship.to_dict(),
            }
        )
    return {
        "transition": transition.to_dict(),
        "replacements": replacements,
    }


def cmd_subject_supersede(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    transition = relationships.supersede_subject(
        args.subject,
        confidence=args.confidence,
        evidence_ref=args.evidence,
        labels=args.label,
    )
    _print_json(_subject_transition_output(relationships, transition))
    return 0


def cmd_subject_merge(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    transition = relationships.merge_subjects(
        args.subjects,
        confidence=args.confidence,
        evidence_ref=args.evidence,
        inherit_subject_ref=args.inherit,
        labels=args.label,
    )
    _print_json(_subject_transition_output(relationships, transition))
    return 0


def cmd_subject_split(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    relationships = RelationshipBook(
        config.relationships_path,
        own_actor_id=identity.node_id,
    )
    groups = [
        tuple(item.strip() for item in raw.split(",") if item.strip())
        for raw in args.group
    ]
    transition = relationships.split_subject(
        args.subject,
        groups,
        confidence=args.confidence,
        evidence_ref=args.evidence,
        inherit_group=(
            args.inherit_group - 1
            if args.inherit_group is not None
            else None
        ),
        labels=args.label,
    )
    _print_json(_subject_transition_output(relationships, transition))
    return 0


def cmd_prekey_generate(args: argparse.Namespace) -> int:
    _, identity, peers, store = _load_runtime(args.home)
    try:
        if store.unscoped_local_prekey_count():
            raise ValueError(
                "legacy unscoped prekeys must be migrated before generating "
                "peer-scoped inventory"
            )
        peer_id = str(args.peer).strip()
        if not peer_id:
            cards = peers.all()
            if len(cards) != 1:
                raise ValueError(
                    "--peer is required unless exactly one trusted peer is pinned"
                )
            peer_id = cards[0].node_id
        peers.require(peer_id)
        bundle = generate_prekey_bundle(
            identity,
            store,
            peer_id=peer_id,
            count=args.count,
            ttl_ms=int(args.ttl_days * 86400 * 1000),
        )
        bundle.save(args.path)
        _print_json(
            {
                "generated": len(bundle.keys),
                "generation": bundle.generation,
                "intended_peer_id": bundle.intended_peer_id,
                "expires_ms": bundle.expires_ms,
                "bundle_hash": bundle.bundle_hash,
                "path": str(Path(args.path).resolve()),
            }
        )
    finally:
        store.close()
    return 0


def cmd_prekey_import(args: argparse.Namespace) -> int:
    _, identity, peers, store = _load_runtime(args.home)
    try:
        bundle = PreKeyBundle.load(args.path)
        card = peers.require(bundle.node_id)
        result = import_prekey_bundle(
            bundle,
            card,
            store,
            recipient_node_id=identity.node_id,
        )
        result["path"] = str(Path(args.path).resolve())
        _print_json(result)
    finally:
        store.close()
    return 0


def cmd_prekey_status(args: argparse.Namespace) -> int:
    config, _, _, store = _load_runtime(args.home)
    try:
        result = store.prekey_status(args.peer)
        result["policy"] = config.prekey_policy
        result["auto"] = {
            "enabled": config.prekey_auto_enabled,
            "low_watermark": config.prekey_low_watermark,
            "batch_size": config.prekey_batch_size,
            "request_interval": config.prekey_request_interval,
            "ttl_days": config.prekey_ttl_days,
        }
        result["scope_warning"] = (
            "legacy unscoped prekeys require an explicit single-peer migration"
            if store.unscoped_local_prekey_count()
            else ""
        )
        _print_json(result)
    finally:
        store.close()
    return 0


def cmd_prekey_policy(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    updated = replace(config, prekey_policy=args.policy)
    updated.save()
    _print_json(
        {
            "prekey_policy": updated.prekey_policy,
            "restart_required": True,
        }
    )
    return 0


def cmd_prekey_config(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    updated = replace(
        config,
        prekey_auto_enabled=(
            config.prekey_auto_enabled if args.auto is None else bool(args.auto)
        ),
        prekey_low_watermark=(
            config.prekey_low_watermark
            if args.low_watermark is None
            else max(1, min(int(args.low_watermark), 999))
        ),
        prekey_batch_size=(
            config.prekey_batch_size
            if args.batch_size is None
            else max(1, min(int(args.batch_size), 1000))
        ),
        prekey_request_interval=(
            config.prekey_request_interval
            if args.request_interval is None
            else max(30.0, min(float(args.request_interval), 86400.0))
        ),
        prekey_ttl_days=(
            config.prekey_ttl_days
            if args.ttl_days is None
            else max(1.0, min(float(args.ttl_days), 365.0))
        ),
    )
    updated.save()
    _print_json(
        {
            "prekey_auto_enabled": updated.prekey_auto_enabled,
            "prekey_low_watermark": updated.prekey_low_watermark,
            "prekey_batch_size": updated.prekey_batch_size,
            "prekey_request_interval": updated.prekey_request_interval,
            "prekey_ttl_days": updated.prekey_ttl_days,
            "restart_required": True,
        }
    )
    return 0


def cmd_prekey_replenish(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        _print_json(
            node.request_prekey_replenishment(args.peer, force=True)
        )
    finally:
        node.close()
    return 0


def cmd_prekey_migrate(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    store = PacketStore(config.database_path)
    try:
        count = store.unscoped_local_prekey_count()
        cards = peers.all()
        if not count:
            _print_json({"migrated": 0, "reason": "no active unscoped prekeys"})
            return 0
        if len(cards) == 1:
            result = store.scope_legacy_local_prekeys(cards[0].node_id)
            result["mode"] = "scope-to-sole-peer"
            _print_json(result)
            return 0
        if not args.retire_shared:
            raise ValueError(
                "multiple peers make v1 ownership ambiguous; rerun with "
                "--retire-shared to erase active shared private keys and "
                "baseline fresh v2 generations for every pinned peer"
            )
        if not cards:
            raise ValueError("cannot retire shared prekeys without pinned peers")
        result = store.retire_unscoped_local_prekeys(
            [card.node_id for card in cards]
        )
        result["mode"] = "retire-shared"
        result["warning"] = (
            "in-flight packets using retired v1 keys can no longer be decrypted; "
            "restart the node to request fresh peer-scoped v2 bundles"
        )
        _print_json(result)
    finally:
        store.close()
    return 0


def _message_body(args: argparse.Namespace) -> tuple[Any, str]:
    selected = sum(
        bool(item)
        for item in (
            args.text is not None,
            args.json_body is not None,
            args.file,
            args.stdin,
        )
    )
    if selected != 1:
        raise ValueError(
            "choose exactly one of --text, --json-body, --file, or --stdin"
        )
    if args.text is not None:
        return args.text, "text/plain; charset=utf-8"
    if args.json_body is not None:
        return json.loads(args.json_body), "application/msgpack"
    if args.file:
        path = Path(args.file)
        return {
            "name": path.name,
            "content": path.read_bytes(),
        }, "application/octet-stream"
    raw = sys.stdin.buffer.read()
    if args.stdin_format == "json":
        return json.loads(raw.decode("utf-8")), "application/msgpack"
    if args.stdin_format == "text":
        return raw.decode("utf-8"), "text/plain; charset=utf-8"
    return raw, "application/octet-stream"


def cmd_send(args: argparse.Namespace) -> int:
    body, codec = _message_body(args)
    node = AnetNode(NodeConfig.load(args.home))
    try:
        packet_id = node.queue(
            args.destination,
            kind=args.kind,
            body=body,
            ttl_seconds=args.ttl,
            max_hops=args.max_hops,
            causal=args.causal,
            codec=codec,
            reply_to=args.reply_to,
            qos=args.qos,
        )
        raw = node.store.get_packet(packet_id)
        if raw is None:  # pragma: no cover - queue persists before returning
            raise RuntimeError("queued packet is missing from the local store")
        info = inspect_packet(raw)
        _print_json(
            {
                "queued": packet_id,
                "destination": args.destination,
                "kind": args.kind,
                "packet_version": info.version,
                "key_mode": info.key_mode,
                "prekey_id": info.prekey_id,
                "forward_secrecy": info.key_mode == "opk",
                "forward_secrecy_scope": (
                    "captured transport ciphertext after recipient prekey erasure"
                    if info.key_mode == "opk"
                    else "none"
                ),
            }
        )
    finally:
        node.close()
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    config, _, _, store = _load_runtime(args.home)
    try:
        messages = store.list_inbox(
            limit=args.limit,
            unread_only=args.unread,
            include_untrusted=not args.trusted_only,
        )
        _print_json(messages)
        if args.mark_read:
            store.mark_read([item["packet_id"] for item in messages])
    finally:
        store.close()
    return 0


def cmd_consumer_open(args: argparse.Namespace) -> int:
    _, _, _, store = _load_runtime(args.home)
    try:
        _print_json(
            store.open_consumer_group(
                args.group,
                start=args.start,
                kind_prefix=args.kind_prefix,
                sender_id=args.sender,
                trusted_only=args.trusted_only,
                include_transient=args.include_transient,
            )
        )
    finally:
        store.close()
    return 0


def cmd_consumer_claim(args: argparse.Namespace) -> int:
    _, _, _, store = _load_runtime(args.home)
    try:
        _print_json(
            store.claim_consumer_messages(
                args.group,
                args.owner,
                limit=args.limit,
                lease_seconds=args.lease_seconds,
            )
        )
    finally:
        store.close()
    return 0


def cmd_consumer_settle(args: argparse.Namespace) -> int:
    _, _, _, store = _load_runtime(args.home)
    try:
        if args.action == "ack":
            result = store.acknowledge_claim(args.group, args.owner, args.claim_token)
        else:
            result = store.reject_claim(
                args.group,
                args.owner,
                args.claim_token,
                retry_seconds=args.retry_seconds,
                error=args.error,
            )
        _print_json(result)
    finally:
        store.close()
    return 0


def cmd_consumer_renew(args: argparse.Namespace) -> int:
    _, _, _, store = _load_runtime(args.home)
    try:
        _print_json(
            store.renew_claim(
                args.group,
                args.owner,
                args.claim_token,
                lease_seconds=args.lease_seconds,
            )
        )
    finally:
        store.close()
    return 0


def cmd_consumer_status(args: argparse.Namespace) -> int:
    _, _, _, store = _load_runtime(args.home)
    try:
        _print_json(store.consumer_group_status(args.group))
    finally:
        store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        _print_json(node.status())
    finally:
        node.close()
    return 0


def cmd_direct_proxy(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    if args.clear and args.url:
        raise ValueError("proxy URL and --clear are mutually exclusive")
    if not args.url and not args.clear and (
        args.allow_remote or args.username_env or args.password_env
    ):
        raise ValueError("proxy options require a proxy URL")
    if args.clear:
        proxy = None
    elif args.url:
        proxy = DirectProxyConfig.from_dict(
            {
                "url": args.url,
                "allow_remote": args.allow_remote,
                "username_env": args.username_env,
                "password_env": args.password_env,
            }
        )
    else:
        proxy = config.direct_proxy
    if args.clear or args.url:
        replace(config, direct_proxy=proxy, direct_dialers=()).save()
    _print_json(
        {
            "direct_proxy": proxy.to_dict() if proxy else None,
            "restart_required": bool(args.clear or args.url),
        }
    )
    return 0


def cmd_dialer_add(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    dialers = list(config.direct_dialers or config.effective_direct_dialers())
    if any(item.name == args.name for item in dialers):
        raise ValueError(f"direct dialer already exists: {args.name}")
    if args.type == "raw":
        if (
            args.url
            or args.allow_remote
            or args.username_env
            or args.password_env
            or args.executable
            or args.arg
            or args.env
        ):
            raise ValueError("raw dialer does not accept proxy or stdio options")
        value = {
            "name": args.name,
            "type": "raw",
            "priority": args.priority,
            "enabled": args.enabled,
        }
    elif args.type == "stdio":
        if args.url or args.allow_remote or args.username_env or args.password_env:
            raise ValueError("stdio dialer does not accept proxy options")
        if not args.executable:
            raise ValueError("stdio dialer requires --executable")
        value = {
            "name": args.name,
            "type": "stdio",
            "executable": args.executable,
            "args": args.arg,
            "env": args.env,
            "startup_timeout": args.startup_timeout,
            "priority": args.priority,
            "enabled": args.enabled,
        }
    else:
        if args.executable or args.arg or args.env:
            raise ValueError("SOCKS dialer does not accept stdio options")
        if not args.url:
            raise ValueError("SOCKS dialer requires --url")
        value = {
            "name": args.name,
            "type": args.type,
            "url": args.url,
            "priority": args.priority,
            "enabled": args.enabled,
            "allow_remote": args.allow_remote,
            "username_env": args.username_env,
            "password_env": args.password_env,
        }
    dialer = DirectDialerConfig.from_dict(value)
    dialers.append(dialer)
    updated = replace(
        config,
        direct_proxy=None,
        direct_dialers=tuple(sorted(dialers, key=lambda item: item.name)),
    )
    updated.save()
    _print_json({"added": dialer.to_dict(), "restart_required": True})
    return 0


def cmd_dialer_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    _print_json(
        {
            "source": "explicit" if config.direct_dialers else "legacy-compatible",
            "dialers": [
                item.to_dict() for item in config.effective_direct_dialers()
            ],
        }
    )
    return 0


def cmd_dialer_set(args: argparse.Namespace) -> int:
    if args.priority is None and args.enabled is None:
        raise ValueError("dialer-set requires --priority or --enabled/--no-enabled")
    config = NodeConfig.load(args.home)
    dialers = list(config.direct_dialers or config.effective_direct_dialers())
    for index, dialer in enumerate(dialers):
        if dialer.name != args.name:
            continue
        dialers[index] = DirectDialerConfig.from_dict(
            replace(
                dialer,
                priority=dialer.priority if args.priority is None else args.priority,
                enabled=dialer.enabled if args.enabled is None else args.enabled,
            ).to_dict()
        )
        updated = replace(
            config,
            direct_proxy=None,
            direct_dialers=tuple(sorted(dialers, key=lambda item: item.name)),
        )
        updated.save()
        _print_json(
            {
                "updated": dialers[index].to_dict(),
                "restart_required": True,
            }
        )
        return 0
    raise ValueError(f"unknown direct dialer: {args.name}")


def cmd_dialer_remove(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    remaining = tuple(
        item for item in config.direct_dialers if item.name != args.name
    )
    if len(remaining) == len(config.direct_dialers):
        raise ValueError(f"unknown explicit direct dialer: {args.name}")
    replace(config, direct_dialers=remaining).save()
    _print_json(
        {
            "removed": args.name,
            "effective_dialers": [
                item.to_dict()
                for item in replace(config, direct_dialers=remaining).effective_direct_dialers()
            ],
            "restart_required": True,
        }
    )
    return 0


def cmd_locator_config(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    if args.clear_advertise and args.advertise is not None:
        raise ValueError("--advertise and --clear-advertise are mutually exclusive")
    contexts = set(config.locator_contexts)
    additions = {validate_locator_context(item) for item in args.add_context}
    removals = {validate_locator_context(item) for item in args.remove_context}
    contexts.update(additions)
    contexts.difference_update(removals)
    if args.clear_advertise:
        advertise: tuple[str, ...] = ()
    elif args.advertise is not None:
        advertise = tuple(parse_locator(item).raw for item in args.advertise)
    else:
        advertise = config.advertise
    changed = (
        tuple(sorted(contexts)) != config.locator_contexts
        or advertise != config.advertise
    )
    updated = replace(
        config,
        locator_contexts=tuple(sorted(contexts)),
        advertise=advertise,
    )
    if changed:
        updated.save()
        identity = Identity.load(updated.identity_path)
        identity.card(
            addresses=updated.effective_addresses(),
            capabilities=updated.capabilities,
        ).save(updated.home / "card.json")
    _print_json(
        {
            "locator_contexts": list(updated.locator_contexts),
            "advertise": list(updated.advertise),
            "effective_addresses": list(updated.effective_addresses()),
            "card_updated": changed,
            "restart_required": changed,
            "note": "contexts are routing hints; peer identity is still cryptographic",
        }
    )
    return 0


async def _sync(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = await node.adaptive_sync_once()
        result["store"] = node.store.status()
        _print_json(result)
        unavailable = any(
            route["selected_path"] == "none" for route in result["routes"]
        )
        selected_carrier_errors = {
            key: value
            for key, value in result.get("carrier_errors", {}).items()
            if any(
                route["selected_path"] == f"directory:{key.split(':', 1)[0]}"
                for route in result["routes"]
            )
        }
        return 0 if not unavailable and not selected_carrier_errors else 1
    finally:
        await node.stop()
        node.close()


def cmd_sync(args: argparse.Namespace) -> int:
    return asyncio.run(_sync(args))


async def _probe(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = await node.probe(
            args.destination,
            timeout=args.timeout,
            interval=args.interval,
            carrier_grace=args.carrier_grace,
            payload_bytes=args.payload_bytes,
            qos=args.qos,
        )
        _print_json(result)
        return 0 if result["ok"] else 2
    finally:
        await node.stop()
        node.close()


def cmd_probe(args: argparse.Namespace) -> int:
    return asyncio.run(_probe(args))


async def _dialer_probe(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = await node.probe_dialers(
            args.destination,
            dialer_names=tuple(args.dialer),
        )
        _print_json(result)
        if args.require_all:
            return 0 if result["all_healthy"] else 1
        return 0 if result["ok"] else 1
    finally:
        node.close()


def cmd_dialer_probe(args: argparse.Namespace) -> int:
    return asyncio.run(_dialer_probe(args))


async def _benchmark(args: argparse.Namespace) -> int:
    if not 0.0 <= args.min_success_rate <= 1.0:
        raise ValueError("--min-success-rate must be between 0 and 1")
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = await run_probe_series(
            node,
            args.destination,
            count=args.count,
            timeout=args.timeout,
            spacing=args.spacing,
            carrier_grace=args.carrier_grace,
            payload_bytes=args.payload_bytes,
            qos=args.qos,
            output_path=args.out,
        )
        _print_json(result["summary"])
        return 0 if result["summary"]["success_rate"] >= args.min_success_rate else 2
    finally:
        await node.stop()
        node.close()


def cmd_benchmark(args: argparse.Namespace) -> int:
    return asyncio.run(_benchmark(args))


async def _monitor(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        summary = await monitor_probes(
            node,
            args.destination,
            output_path=args.out,
            stop_event=stop_event,
            interval=args.interval,
            jitter=args.jitter,
            timeout=args.timeout,
            carrier_grace=args.carrier_grace,
            payload_bytes=args.payload_bytes,
            qos=args.qos,
            max_observations=args.max_observations,
        )
        _print_json(summary)
        return 0
    finally:
        await node.stop()
        node.close()


def cmd_monitor(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_monitor(args))
    except KeyboardInterrupt:
        return 130


def cmd_carrier_sync(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = sync_directory_once(
            node,
            args.root,
            peer_ids=args.peer,
            limit=args.limit,
            retry_after_ms=int(args.retry_seconds * 1000),
        )
        _print_json(result)
    finally:
        node.close()
    return 0


async def _carrier_serve(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    schedule = AdaptiveSchedule(args.interval, args.jitter, args.idle_backoff_max)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            result = await asyncio.to_thread(
                sync_directory_once,
                node,
                args.root,
                peer_ids=args.peer,
                limit=args.limit,
                retry_after_ms=int(args.retry_seconds * 1000),
            )
            if (
                result["pulled_packets"]
                or result["pulled_acks"]
                or result["pushed_packets"]
            ):
                LOGGER.info(
                    "directory carrier: pulled=%d acked=%d pushed=%d pending=%d",
                    result["pulled_packets"],
                    result["pulled_acks"],
                    result["pushed_packets"],
                    result["store"]["pending"],
                )
            activity = bool(
                result["pulled_packets"]
                or result["pulled_acks"]
                or result["pushed_packets"]
            )
            delay = schedule.record(time.monotonic(), activity=activity)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
    finally:
        node.close()
    return 0


def cmd_carrier_serve(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_carrier_serve(args))
    except KeyboardInterrupt:
        return 130


def cmd_carrier_add(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    if args.live_relay and args.type != "ahub":
        raise ValueError("--live-relay is valid only for Ahub carriers")
    all_carriers = (
        *config.directory_carriers,
        *config.webdav_carriers,
        *config.ahub_carriers,
    )
    if any(carrier.name == args.name for carrier in all_carriers):
        raise ValueError(f"carrier already exists: {args.name}")
    known_peers = {card.node_id for card in PeerBook(config.peers_path).all()}
    unknown = set(args.peer) - known_peers
    if unknown:
        raise ValueError(f"unknown configured peer(s): {', '.join(sorted(unknown))}")
    common = {
        "name": args.name,
        "peers": args.peer,
        "mode": args.mode,
        "interval": args.interval
        if args.interval is not None
        else (5.0 if args.type == "webdav" else 2.0),
        "jitter": args.jitter,
        "idle_backoff_max": args.idle_backoff_max,
        "retry_seconds": args.retry_seconds,
        "priority": args.priority
        if args.priority is not None
        else (
            100
            if args.type == "directory"
            else (50 if args.type == "ahub" else 200)
        ),
        "enabled": True,
    }
    if args.type == "directory":
        carrier = DirectoryCarrierConfig.from_dict(
            {"type": "directory", "path": args.target, **common},
            home=config.home,
        )
        config = replace(
            config, directory_carriers=(*config.directory_carriers, carrier)
        )
    elif args.type == "webdav":
        carrier = WebDAVCarrierConfig.from_dict(
            {
                "type": "webdav",
                "base_url": args.target,
                "timeout": args.timeout,
                "bearer_env": args.bearer_env,
                "username_env": args.username_env,
                "password_env": args.password_env,
                "allow_insecure_http": args.allow_insecure_http,
                **common,
            }
        )
        config = replace(config, webdav_carriers=(*config.webdav_carriers, carrier))
    else:
        if args.bearer_env or args.username_env or args.password_env:
            raise ValueError(
                "Ahub authentication uses the node identity, not HTTP credentials"
            )
        carrier = AhubCarrierConfig.from_dict(
            {
                "type": "ahub",
                "base_url": args.target,
                "timeout": args.timeout,
                "claim_lease_seconds": args.claim_lease_seconds,
                "allow_insecure_http": args.allow_insecure_http,
                "live_relay_enabled": args.live_relay,
                "relay_reservation_ttl_seconds": (
                    args.relay_reservation_ttl_seconds
                ),
                "relay_session_seconds": args.relay_session_seconds,
                "relay_bytes_each_direction": (
                    args.relay_bytes_each_direction
                ),
                "relay_listener_retry_seconds": (
                    args.relay_listener_retry_seconds
                ),
                **common,
            }
        )
        config = replace(
            config, ahub_carriers=(*config.ahub_carriers, carrier)
        )
    config.save()
    _print_json({"added": carrier.to_dict(), "restart_required": True})
    return 0


def cmd_carrier_list(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    _print_json(
        [
            carrier.to_dict()
            for carrier in (
                *config.directory_carriers,
                *config.webdav_carriers,
                *config.ahub_carriers,
            )
        ]
    )
    return 0


def cmd_carrier_remove(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    remaining_directory = tuple(
        carrier for carrier in config.directory_carriers if carrier.name != args.name
    )
    remaining_webdav = tuple(
        carrier for carrier in config.webdav_carriers if carrier.name != args.name
    )
    remaining_ahub = tuple(
        carrier for carrier in config.ahub_carriers if carrier.name != args.name
    )
    if len(remaining_directory) == len(config.directory_carriers) and len(
        remaining_webdav
    ) == len(config.webdav_carriers) and len(remaining_ahub) == len(
        config.ahub_carriers
    ):
        raise ValueError(f"unknown carrier: {args.name}")
    replace(
        config,
        directory_carriers=remaining_directory,
        webdav_carriers=remaining_webdav,
        ahub_carriers=remaining_ahub,
    ).save()
    _print_json({"removed": args.name, "restart_required": True})
    return 0


def cmd_routing_config(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    routing = RoutingConfig(
        direct_failure_threshold=(
            args.failure_threshold
            if args.failure_threshold is not None
            else config.routing.direct_failure_threshold
        ),
        direct_recovery_threshold=(
            args.recovery_threshold
            if args.recovery_threshold is not None
            else config.routing.direct_recovery_threshold
        ),
        carrier_failure_threshold=(
            args.carrier_failure_threshold
            if args.carrier_failure_threshold is not None
            else config.routing.carrier_failure_threshold
        ),
        carrier_recovery_threshold=(
            args.carrier_recovery_threshold
            if args.carrier_recovery_threshold is not None
            else config.routing.carrier_recovery_threshold
        ),
        carrier_replica_count=(
            args.carrier_replica_count
            if args.carrier_replica_count is not None
            else config.routing.carrier_replica_count
        ),
        direct_retry_interval=(
            args.direct_retry_interval
            if args.direct_retry_interval is not None
            else config.routing.direct_retry_interval
        ),
        direct_race_width=(
            args.direct_race_width
            if args.direct_race_width is not None
            else config.routing.direct_race_width
        ),
        direct_race_delay=(
            args.direct_race_delay
            if args.direct_race_delay is not None
            else config.routing.direct_race_delay
        ),
        direct_idle_probe_interval=(
            args.direct_idle_probe_interval
            if args.direct_idle_probe_interval is not None
            else config.routing.direct_idle_probe_interval
        ),
        direct_probe_jitter=(
            args.direct_probe_jitter
            if args.direct_probe_jitter is not None
            else config.routing.direct_probe_jitter
        ),
        direct_idle_backoff_max=(
            args.direct_idle_backoff_max
            if args.direct_idle_backoff_max is not None
            else config.routing.direct_idle_backoff_max
        ),
        fallback_probe_interval=(
            args.fallback_probe_interval
            if args.fallback_probe_interval is not None
            else config.routing.fallback_probe_interval
        ),
        fallback_probe_jitter=(
            args.fallback_probe_jitter
            if args.fallback_probe_jitter is not None
            else config.routing.fallback_probe_jitter
        ),
        switch_cooldown=(
            args.cooldown
            if args.cooldown is not None
            else config.routing.switch_cooldown
        ),
    )
    updated = replace(
        config,
        routing=RoutingConfig.from_dict(routing.to_dict()),
        sync_interval=(
            config.sync_interval
            if args.sync_interval is None
            else max(0.2, args.sync_interval)
        ),
        sync_jitter=(
            config.sync_jitter
            if args.sync_jitter is None
            else max(0.0, min(args.sync_jitter, 0.9))
        ),
        listen_enabled=(config.listen_enabled if args.listen is None else args.listen),
        direct_enabled=(config.direct_enabled if args.direct is None else args.direct),
    )
    updated.save()
    _print_json(
        {
            "listen_enabled": updated.listen_enabled,
            "direct_enabled": updated.direct_enabled,
            "sync_interval": updated.sync_interval,
            "sync_jitter": updated.sync_jitter,
            "routing": updated.routing.to_dict(),
            "restart_required": True,
        }
    )
    return 0


async def _serve(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, node.request_stop)
    try:
        await node.run_forever()
    finally:
        await node.stop()
        node.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 130


def cmd_wake_bridge(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "")
    bridge = WakeBridge(
        home=args.home,
        group=args.group,
        endpoint=args.endpoint,
        token=token,
        poll_seconds=args.poll_seconds,
        rearm_seconds=args.rearm_seconds,
        startup_delay_seconds=args.startup_delay_seconds,
        start=args.start,
    )
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_bundle_export(args: argparse.Namespace) -> int:
    config, _, _, store = _load_runtime(args.home)
    try:
        _print_json(create_bundle(store, args.path, destination_id=args.destination))
    finally:
        store.close()
    return 0


def cmd_bundle_import(args: argparse.Namespace) -> int:
    node = AnetNode(NodeConfig.load(args.home))
    try:
        result = import_bundle(node.store, args.path)
        result["local_processed"] = node.process_local_spool()
        _print_json(result)
    finally:
        node.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    card = identity.card(
        addresses=config.effective_addresses(), capabilities=config.capabilities
    )
    card.verify()
    cert_path, key_path, fingerprint = identity.ensure_tls_material(config.home)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    store = PacketStore(config.database_path)
    try:
        locator_warnings: list[str] = []
        for address in config.effective_addresses():
            locator = parse_locator(address)
            try:
                is_loopback = ipaddress.ip_address(locator.host).is_loopback
            except ValueError:
                is_loopback = locator.host.lower() == "localhost"
            if locator.scope == "host" and is_loopback:
                locator_warnings.append(
                    "host-scoped loopback locator is local to this runtime; "
                    "do not use it for Windows/WSL direct connectivity"
                )
            if locator.scope == "legacy" and is_loopback:
                locator_warnings.append(
                    "legacy loopback locator has no host zone; physical peers may "
                    "try their own loopback before falling back"
                )
            if locator.context and locator.context not in config.locator_contexts:
                locator_warnings.append(
                    f"advertised locator context is not local: {locator.context}"
                )
        effective_dialers = config.effective_direct_dialers()
        dialer_warnings: list[str] = []
        if config.direct_enabled and not effective_dialers:
            dialer_warnings.append(
                "direct transport is enabled but no direct dialer is enabled"
            )
        result = {
            "ok": True,
            "node_id": identity.node_id,
            "home": str(config.home),
            "identity": True,
            "card": True,
            "tls": {
                "cert": str(cert_path),
                "key": str(key_path),
                "fingerprint_sha256": fingerprint.hex(),
            },
            "trusted_peers": len(peers.all()),
            "locators": {
                "contexts": list(config.locator_contexts),
                "addresses": list(config.effective_addresses()),
                "warnings": locator_warnings,
            },
            "dialers": {
                "effective": [item.to_dict() for item in effective_dialers],
                "warnings": dialer_warnings,
            },
            "store": store.status(),
            "prekeys": {
                "policy": config.prekey_policy,
                **store.prekey_status(),
            },
        }
        _print_json(result)
    finally:
        store.close()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    del args
    from .mcp_server import run_mcp

    run_mcp()
    return 0


def _control_trusted_keys(args: argparse.Namespace) -> dict[str, str] | None:
    key_id = str(getattr(args, "control_key_id", "") or "").strip()
    public_key = str(getattr(args, "control_public_key", "") or "").strip()
    if not key_id and not public_key:
        return None
    if not key_id or not public_key:
        raise ValueError(
            "--control-key-id and --control-public-key must be provided together"
        )
    return _normalise_trusted_keys({key_id: public_key})


def cmd_control_sync(args: argparse.Namespace) -> int:
    _print_json(
        sync_remote_control(
            args.home,
            url=args.url,
            trusted_keys=_control_trusted_keys(args),
            apply_software=not args.no_software,
        )
    )
    return 0


def cmd_control_verify(args: argparse.Namespace) -> int:
    _print_json(
        verify_remote_control(
            args.home,
            url=args.url,
            trusted_keys=_control_trusted_keys(args),
        )
    )
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    result = asyncio.run(
        run_supervisor(
            args.home,
            url=args.url,
            trusted_keys=_control_trusted_keys(args),
            interval=args.interval,
            once=args.once,
            apply_software=not args.no_software,
        )
    )
    if result is not None:
        _print_json(result)
    return 0


def cmd_supervisor_status(args: argparse.Namespace) -> int:
    result = inspect_supervisor_health(args.home)
    _print_json(result)
    return 0 if result["ok"] else 1


_NODE_HOME_MARKERS = (
    "identity.json",
    "tls-key.pem",
    "config.json",
    "anet.sqlite3",
)


def _ahub_root(path: Path) -> Path:
    root = Path(path).expanduser().resolve()
    conflicts = [name for name in _NODE_HOME_MARKERS if (root / name).exists()]
    if conflicts:
        raise ValueError(
            "Ahub root must not be a node home; found: "
            + ", ".join(conflicts)
        )
    return root


def cmd_ahub_allow(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    root = _ahub_root(args.root)
    with AhubService(root) as service:
        changed = service.allow_node(args.node_id)
        _print_json(
            {
                "allowed": args.node_id,
                "changed": changed,
                "root": str(root),
                "status": service.status(),
            }
        )
    return 0


def cmd_ahub_disallow(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    node_id = str(args.node_id).strip()
    if str(args.confirm).strip() != node_id:
        raise ValueError("--confirm must exactly match the complete Node ID")
    root = _ahub_root(args.root)
    with AhubService(root) as service:
        changed = service.disallow_node(node_id)
        _print_json(
            {
                "disallowed": node_id,
                "changed": changed,
                "pending_mailbox_retained_until_expiry": True,
                "root": str(root),
                "status": service.status(),
            }
        )
    return 0


def cmd_ahub_nodes(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    root = _ahub_root(args.root)
    with AhubService(root) as service:
        _print_json(
            {
                "root": str(root),
                "nodes": service.ahub.allowed_nodes(
                    include_disabled=args.include_disabled
                ),
            }
        )
    return 0


def cmd_ahub_status(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    root = _ahub_root(args.root)
    with AhubService(root) as service:
        _print_json(
            {
                "ok": service.health(),
                "root": str(root),
                **service.status(),
            }
        )
    return 0


def cmd_ahub_purge(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    root = _ahub_root(args.root)
    with AhubService(root) as service:
        result = service.ahub.purge()
        _print_json(
            {
                "root": str(root),
                "purged": result,
                "status": service.status(),
            }
        )
    return 0


def cmd_ahub_checkpoint(args: argparse.Namespace) -> int:
    from .ahub import AhubService

    root = _ahub_root(args.root)
    with AhubService(root) as service:
        result = service.checkpoint()
        if any(item["busy"] for item in result.values()):
            raise RuntimeError("Ahub checkpoint is busy")
        _print_json(
            {
                "root": str(root),
                "checkpoint": result,
                "backup_ready_if_service_stopped": True,
            }
        )
    return 0


def _ahub_bind_is_loopback(host: str) -> bool:
    value = str(host).strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def cmd_ahub_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "ahub-serve requires the optional 'ahub' dependency: "
            "install anet-fabric[ahub]"
        ) from exc

    from .ahub import AhubService
    from .ahub_http import AhubASGI, AhubRateLimit

    root = _ahub_root(args.root)
    if not 1 <= args.port <= 65535:
        raise ValueError("Ahub port must be between 1 and 65535")
    if not 1 <= args.limit_concurrency <= 10_000:
        raise ValueError("Ahub concurrency limit must be between 1 and 10000")
    if not 1 <= args.keep_alive_seconds <= 60:
        raise ValueError("Ahub keep-alive must be between 1 and 60 seconds")
    if not 1 <= args.rate_limit_per_minute <= 1_000_000:
        raise ValueError(
            "Ahub rate-limit per minute must be between 1 and 1000000"
        )
    if not 1 <= args.rate_limit_burst <= 100_000:
        raise ValueError("Ahub rate-limit burst must be between 1 and 100000")
    if not _ahub_bind_is_loopback(args.host) and not args.allow_non_loopback:
        raise ValueError(
            "non-loopback Ahub binding requires --allow-non-loopback and "
            "must be protected by a TLS reverse proxy or private network"
        )
    service = AhubService(root)
    app = AhubASGI(
        service,
        rate_limit=AhubRateLimit(
            requests_per_minute=args.rate_limit_per_minute,
            burst=args.rate_limit_burst,
        ),
    )
    LOGGER.info(
        "starting_ahub root=%s bind=%s:%d access_log=false proxy_headers=false",
        root,
        args.host,
        args.port,
    )
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            lifespan="off",
            workers=1,
            limit_concurrency=args.limit_concurrency,
            timeout_keep_alive=args.keep_alive_seconds,
            ws_max_size=service.ahub.limits.max_relay_frame_bytes,
            ws_per_message_deflate=False,
            log_level="debug" if args.verbose else "info",
        )
    finally:
        service.close()
    return 0


def _social_threshold(
    current: SocialThreshold,
    score: int | None,
    confidence: int | None,
) -> SocialThreshold:
    return SocialThreshold(
        min_score=current.min_score if score is None else score,
        min_confidence=(
            current.min_confidence if confidence is None else confidence
        ),
        required_labels=current.required_labels,
    )


def cmd_discord_social_config(args: argparse.Namespace) -> int:
    node_config = NodeConfig.load(args.home)
    path = discord_social_config_path(args.home)
    existing = DiscordSocialConfig.load(args.home) if path.exists() else None
    guild_id = args.guild or (existing.guild_id if existing else "")
    channels = (
        tuple(args.channel)
        if args.channel
        else (existing.channel_ids if existing else ())
    )
    if not guild_id or not channels:
        raise ValueError(
            "Discord social config requires --guild and at least one --channel"
        )
    destination = (
        ""
        if args.clear_destination
        else (
            args.destination
            if args.destination is not None
            else (existing.destination_node_id if existing else "")
        )
    )
    if destination:
        identity = Identity.load(node_config.identity_path)
        peers = PeerBook(
            node_config.peers_path,
            own_node_id=identity.node_id,
        )
        peers.require(destination)
    current_policy = existing.policy if existing else SocialPolicy()
    policy = SocialPolicy(
        version=current_policy.version,
        surface=_social_threshold(
            current_policy.surface,
            args.surface_score,
            args.surface_confidence,
        ),
        reply=_social_threshold(
            current_policy.reply,
            args.reply_score,
            args.reply_confidence,
        ),
        amplify=_social_threshold(
            current_policy.amplify,
            args.amplify_score,
            args.amplify_confidence,
        ),
        connect_candidate=_social_threshold(
            current_policy.connect_candidate,
            args.connect_score,
            args.connect_confidence,
        ),
    )
    config = DiscordSocialConfig(
        guild_id=guild_id,
        channel_ids=channels,
        destination_node_id=destination,
        token_env=(
            args.token_env
            or (existing.token_env if existing else "ANET_DISCORD_BOT_TOKEN")
        ),
        content_mode=(
            args.content_mode
            or (existing.content_mode if existing else "mentions")
        ),
        poll_interval_seconds=(
            args.poll_seconds
            if args.poll_seconds is not None
            else (existing.poll_interval_seconds if existing else 15.0)
        ),
        signal_ttl_seconds=(
            args.signal_ttl
            if args.signal_ttl is not None
            else (existing.signal_ttl_seconds if existing else 7 * 86_400)
        ),
        policy=policy,
        enabled=(
            args.enabled
            if args.enabled is not None
            else (existing.enabled if existing else True)
        ),
    )
    config.save(args.home)
    _print_json(
        {
            "configured": True,
            "enabled": config.enabled,
            "channel_count": len(config.channel_ids),
            "destination_node_id": config.destination_node_id,
            "content_mode": config.content_mode,
            "token_env": config.token_env,
            "poll_interval_seconds": config.poll_interval_seconds,
            "signal_ttl_seconds": config.signal_ttl_seconds,
            "policy": config.policy.to_dict(),
        }
    )
    return 0


def cmd_discord_social_status(args: argparse.Namespace) -> int:
    NodeConfig.load(args.home)
    config_path = discord_social_config_path(args.home)
    if not config_path.exists():
        _print_json({"configured": False})
        return 0
    config = DiscordSocialConfig.load(args.home)
    database_path = discord_social_database_path(args.home)
    status = {
        "actors": 0,
        "events": 0,
        "routed": 0,
        "replied": 0,
        "runtime_state": "never_run",
        "last_attempt_ms": 0,
        "last_success_ms": 0,
        "last_error_ms": 0,
        "last_error_category": "",
        "consecutive_failures": 0,
        "next_retry_ms": 0,
    }
    if database_path.exists():
        store = DiscordSocialStore(
            database_path,
            discord_social_key_path(args.home),
        )
        try:
            status = {**store.status(), **store.runtime_status()}
        finally:
            store.close()
    _print_json(
        {
            "configured": True,
            "enabled": config.enabled,
            "channel_count": len(config.channel_ids),
            "destination_configured": bool(config.destination_node_id),
            "content_mode": config.content_mode,
            "token_env": config.token_env,
            "policy": config.policy.to_dict(),
            **status,
        }
    )
    return 0


def _load_discord_social_store(home: Path) -> DiscordSocialStore:
    NodeConfig.load(home)
    if not discord_social_database_path(home).exists():
        raise ValueError("Discord social ledger has not ingested any events")
    return DiscordSocialStore(
        discord_social_database_path(home),
        discord_social_key_path(home),
    )


def cmd_discord_social_actor(args: argparse.Namespace) -> int:
    config = DiscordSocialConfig.load(args.home)
    store = _load_discord_social_store(args.home)
    try:
        stats = store.actor_stats(args.actor_key)
        if stats is None:
            raise ValueError("unknown Discord social actor")
        _print_json(
            {
                **stats,
                "evaluation": config.policy.evaluate(
                    stats,
                    set(stats["labels"]),
                ),
            }
        )
    finally:
        store.close()
    return 0


def cmd_discord_social_label(args: argparse.Namespace) -> int:
    if not args.add and not args.remove:
        raise ValueError("choose at least one --add or --remove label")
    store = _load_discord_social_store(args.home)
    try:
        _print_json(
            store.update_labels(
                args.actor_key,
                add=set(args.add),
                remove=set(args.remove),
                source=args.source,
            )
        )
    finally:
        store.close()
    return 0


def cmd_discord_social_project(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.home)
    identity = Identity.load(config.identity_path)
    store = _load_discord_social_store(args.home)
    try:
        projector = DiscordRelationshipProjector(
            RelationshipBook(
                config.relationships_path,
                own_actor_id=identity.node_id,
            )
        )
        projections = [
            projector.project_local_event(event)
            for event in store.events(limit=args.limit)
        ]
    finally:
        store.close()
    _print_json(
        {
            "events_examined": len(projections),
            "interactions_recorded": sum(
                1 for item in projections if item.recorded
            ),
            "actors": sorted({item.actor_id for item in projections}),
            "note": (
                "Discord evidence created no Anet peer trust, capability, "
                "context trust, or authorization"
            ),
        }
    )
    return 0


def cmd_discord_social_reply(args: argparse.Namespace) -> int:
    selected = int(args.text is not None) + int(bool(args.stdin))
    if selected != 1:
        raise ValueError("choose exactly one of --text or --stdin")
    content = args.text if args.text is not None else sys.stdin.read()
    bridge = DiscordSocialBridge.from_home(args.home)
    try:
        _print_json(bridge.reply(args.event_key, content))
    finally:
        bridge.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anet", description="Anet encrypted store-and-forward fabric"
    )
    parser.add_argument("--version", action="version", version="Anet 0.12.1")
    parser.add_argument(
        "--home", type=Path, default=default_home(), help="node state directory"
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a node identity and local store")
    init.add_argument("--label", required=True)
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=4242)
    init.add_argument("--advertise", action="append", default=[])
    init.add_argument("--locator-context", action="append", default=[])
    init.set_defaults(func=cmd_init)

    card = sub.add_parser("card", help="export the signed public peer card")
    card.add_argument("--out", type=Path)
    card.add_argument(
        "--keys-only", action="store_true", help="omit direct network addresses"
    )
    card.set_defaults(func=cmd_card)

    peer_add = sub.add_parser("peer-add", help="pin a trusted peer card")
    peer_add.add_argument("card", type=Path)
    peer_add.set_defaults(func=cmd_peer_add)

    peer_list = sub.add_parser("peer-list", help="list pinned peers")
    peer_list.set_defaults(func=cmd_peer_list)

    peer_reachability = sub.add_parser(
        "peer-reachability",
        help="query signed short-lived Ahub reachability for a pinned peer",
    )
    peer_reachability.add_argument("peer")
    peer_reachability.add_argument(
        "--carrier",
        default="",
        help="limit the query to one configured Ahub carrier",
    )
    peer_reachability.set_defaults(func=cmd_peer_reachability)

    peer_revoke = sub.add_parser(
        "peer-revoke",
        help="fail-closed local trust revocation and peer-scoped key retirement",
    )
    peer_revoke.add_argument("peer")
    peer_revoke.add_argument(
        "--confirm", required=True, help="repeat the complete peer Node ID"
    )
    peer_revoke.add_argument("--reason", default="")
    peer_revoke.set_defaults(func=cmd_peer_revoke)

    peer_revocations = sub.add_parser(
        "peer-revocations", help="list locally revoked peer identities"
    )
    peer_revocations.set_defaults(func=cmd_peer_revocations)

    control_import = sub.add_parser(
        "control-import",
        help=(
            "verify and import a public NodeDescriptor or human-device "
            "grant/revocation into this node's control ledger"
        ),
    )
    control_import.add_argument("object", type=Path)
    control_import.set_defaults(func=cmd_control_import)

    control_device = sub.add_parser(
        "control-device",
        help="show current local authorization state for one Human/Device pair",
    )
    control_device.add_argument("human")
    control_device.add_argument("device")
    control_device.set_defaults(func=cmd_control_device)

    pair_offer = sub.add_parser(
        "pair-offer",
        help="create an expiring signed pairing offer for offline or async transfer",
    )
    pair_offer.add_argument("--out", type=Path, required=True)
    pair_offer.add_argument("--ttl", type=int, default=3600, help="validity in seconds")
    pair_offer.add_argument(
        "--keys-only", action="store_true", help="omit direct network addresses"
    )
    pair_offer.set_defaults(func=cmd_pair_offer)

    pair_accept = sub.add_parser(
        "pair-accept",
        help="explicitly trust a signed offer and return a challenge-bound response",
    )
    pair_accept.add_argument("offer", type=Path)
    pair_accept.add_argument("--out", type=Path, required=True)
    pair_accept.add_argument(
        "--keys-only", action="store_true", help="omit direct network addresses"
    )
    pair_accept.set_defaults(func=cmd_pair_accept)

    pair_complete = sub.add_parser(
        "pair-complete",
        help="verify a response against the original local offer and pin the peer",
    )
    pair_complete.add_argument("offer", type=Path)
    pair_complete.add_argument("response", type=Path)
    pair_complete.set_defaults(func=cmd_pair_complete)

    friend_qr = sub.add_parser(
        "friend-qr",
        help="create a signed, expiring QR friend invite",
    )
    friend_qr.add_argument("--out", type=Path, required=True)
    friend_qr.add_argument(
        "--ttl",
        type=int,
        default=600,
        help="validity in seconds",
    )
    friend_qr.add_argument(
        "--keys-only",
        action="store_true",
        help="omit direct network addresses",
    )
    friend_qr.set_defaults(func=cmd_friend_qr)

    friend_scan = sub.add_parser(
        "friend-scan",
        help="scan a QR friend invite or its challenge-bound acceptance",
    )
    friend_scan.add_argument(
        "source",
        help="QR image, .anetqr/.txt payload file, or anet:// friend code",
    )
    friend_scan.add_argument(
        "--out",
        type=Path,
        help="response QR path when accepting an invite",
    )
    friend_scan.add_argument(
        "--keys-only",
        action="store_true",
        help="omit direct network addresses from an acceptance",
    )
    friend_scan.set_defaults(func=cmd_friend_scan)

    relation_list = sub.add_parser(
        "relation-list",
        help="list local subject hypotheses and relationship circles",
    )
    relation_list.add_argument(
        "--model",
        action="store_true",
        help="include Actor facts, competing Subject hypotheses, and events",
    )
    relation_list.set_defaults(func=cmd_relation_list)

    relation_suggest = sub.add_parser(
        "relation-suggest",
        help="derive explainable relationship suggestions without applying them",
    )
    relation_suggest.add_argument(
        "--subject",
        help="limit suggestions to one active observer-local subj_ reference",
    )
    relation_suggest.set_defaults(func=cmd_relation_suggest)

    relation_activity = sub.add_parser(
        "relation-activity",
        help="read the observer-local relationship activity feed",
    )
    relation_activity.add_argument(
        "--after",
        help="opaque rac_ cursor returned by an earlier page",
    )
    relation_activity.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum activities to return (1-500; default: 100)",
    )
    relation_activity.add_argument(
        "--subject",
        help="limit activities to one observer-local subj_ reference",
    )
    relation_activity.add_argument(
        "--wait",
        type=float,
        default=0,
        help="wait up to 30 seconds for a new matching activity",
    )
    relation_activity.set_defaults(func=cmd_relation_activity)

    relation_disclose = sub.add_parser(
        "relation-disclose",
        help=(
            "queue an audience-bound, content-free relationship activity "
            "disclosure"
        ),
    )
    relation_disclose.add_argument("destination")
    relation_disclose.add_argument(
        "--after",
        help="opaque rac_ cursor returned by the previous disclosure",
    )
    relation_disclose.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum activities to disclose (1-100; default: 100)",
    )
    relation_disclose.add_argument(
        "--subject",
        help="disclose activity for one observer-local subj_ reference",
    )
    relation_disclose.add_argument(
        "--ttl",
        type=int,
        default=7 * 86400,
        help="encrypted Packet lifetime in seconds (default: 7 days)",
    )
    relation_disclose.set_defaults(func=cmd_relation_disclose)

    relation_disclosure_list = sub.add_parser(
        "relation-disclosure-list",
        help="list trusted relationship disclosures received by this Actor",
    )
    relation_disclosure_list.add_argument(
        "--sender",
        help="limit results to one complete sender Actor ID",
    )
    relation_disclosure_list.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum disclosures to return (1-500; default: 100)",
    )
    relation_disclosure_list.set_defaults(
        func=cmd_relation_disclosure_list
    )

    relation_reported_view = sub.add_parser(
        "relation-reported-view",
        help="derive one sender-attributed view from received disclosures",
    )
    relation_reported_view.add_argument(
        "sender",
        help="complete Actor ID of the reporting observer",
    )
    relation_reported_view.add_argument(
        "--series",
        help="select one continuity-proven rdsr_ disclosure series",
    )
    relation_reported_view.add_argument(
        "--subject",
        help="limit output to one reported subj_ hypothesis",
    )
    relation_reported_view.add_argument(
        "--include-activities",
        action="store_true",
        help="include bounded source activities behind the derived view",
    )
    relation_reported_view.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum source activities when included (1-500; default: 100)",
    )
    relation_reported_view.set_defaults(func=cmd_relation_reported_view)

    relation_dashboard = sub.add_parser(
        "relation-dashboard",
        help="export one local model and an optional sender-attributed reported view",
    )
    relation_dashboard.add_argument(
        "--reported",
        help="complete Actor ID of one reporting observer to include separately",
    )
    relation_dashboard.add_argument(
        "--series",
        help="select one continuity-proven rdsr_ series for --reported",
    )
    relation_dashboard.set_defaults(func=cmd_relation_dashboard)

    relation_schedule_add = sub.add_parser(
        "relation-disclosure-schedule-add",
        help="create a revocable observer-local disclosure schedule",
    )
    relation_schedule_add.add_argument("destination")
    relation_schedule_scope = relation_schedule_add.add_mutually_exclusive_group(
        required=True
    )
    relation_schedule_scope.add_argument(
        "--all",
        action="store_true",
        help="disclose new activity across all local Subject hypotheses",
    )
    relation_schedule_scope.add_argument(
        "--subject",
        help="limit disclosure to one observer-local subj_ reference",
    )
    relation_schedule_add.add_argument(
        "--interval",
        type=int,
        default=300,
        help="poll interval in seconds (30-86400; default: 300)",
    )
    relation_schedule_add.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum activities per disclosure (1-100; default: 100)",
    )
    relation_schedule_add.add_argument(
        "--lifetime",
        type=int,
        default=30 * 86400,
        help="schedule lifetime in seconds (default: 30 days)",
    )
    relation_schedule_add.add_argument(
        "--packet-ttl",
        type=int,
        default=7 * 86400,
        help="encrypted Packet lifetime in seconds (default: 7 days)",
    )
    relation_schedule_add.add_argument(
        "--include-history",
        action="store_true",
        help="explicitly begin at existing history instead of starting now",
    )
    relation_schedule_add.set_defaults(
        func=cmd_relation_disclosure_schedule_add
    )

    relation_schedule_list = sub.add_parser(
        "relation-disclosure-schedule-list",
        help="list observer-local disclosure schedules and delivery state",
    )
    relation_schedule_list.set_defaults(
        func=cmd_relation_disclosure_schedule_list
    )

    relation_schedule_revoke = sub.add_parser(
        "relation-disclosure-schedule-revoke",
        help="stop one disclosure schedule and discard its pending batch",
    )
    relation_schedule_revoke.add_argument("schedule")
    relation_schedule_revoke.add_argument(
        "--confirm",
        required=True,
        help="repeat the complete rdsc_ schedule ID",
    )
    relation_schedule_revoke.add_argument(
        "--reason",
        default="",
        help="optional bounded local reason code",
    )
    relation_schedule_revoke.set_defaults(
        func=cmd_relation_disclosure_schedule_revoke
    )

    relation_schedule_run = sub.add_parser(
        "relation-disclosure-schedule-run",
        help="run due schedules once, or force one named active schedule",
    )
    relation_schedule_run.add_argument(
        "--schedule",
        help="force one complete rdsc_ schedule ID",
    )
    relation_schedule_run.set_defaults(
        func=cmd_relation_disclosure_schedule_run
    )

    relation_gap_notice = sub.add_parser(
        "relation-disclosure-gap-notice",
        help="report visible missing pages without requesting disclosure",
    )
    relation_gap_notice.add_argument(
        "sender",
        help="complete Actor ID of the reporting observer",
    )
    relation_gap_notice.add_argument(
        "--series",
        required=True,
        help="gapped rdsr_ disclosure series",
    )
    relation_gap_notice.set_defaults(
        func=cmd_relation_disclosure_gap_notice
    )

    relation_gap_notice_list = sub.add_parser(
        "relation-disclosure-gap-notice-list",
        help="list authenticated advisory gap notices received",
    )
    relation_gap_notice_list.add_argument(
        "--reporter",
        help="limit results to one complete reporter Actor ID",
    )
    relation_gap_notice_list.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum notices to return (1-500; default: 100)",
    )
    relation_gap_notice_list.set_defaults(
        func=cmd_relation_disclosure_gap_notice_list
    )

    relation_gap_retransmit = sub.add_parser(
        "relation-disclosure-gap-retransmit",
        help="retransmit archived pages under their still-active schedule",
    )
    relation_gap_retransmit.add_argument(
        "notice",
        help="complete authenticated rgap_ notice ID",
    )
    relation_gap_retransmit.set_defaults(
        func=cmd_relation_disclosure_gap_retransmit
    )

    relation_decide = sub.add_parser(
        "relation-decide",
        help="accept or reject one current relationship suggestion",
    )
    relation_decide.add_argument(
        "suggestion",
        help="current rsg_ relationship suggestion ID",
    )
    relation_decide.add_argument(
        "decision",
        choices=("accepted", "rejected"),
        help="explicit observer-local decision",
    )
    relation_decide.add_argument(
        "--reason",
        required=True,
        help="bounded rationale code or content-free evidence reference",
    )
    relation_decide.set_defaults(func=cmd_relation_decide)

    relation_decision_list = sub.add_parser(
        "relation-decision-list",
        help="list immutable observer-local suggestion decisions",
    )
    relation_decision_list.add_argument(
        "--subject",
        help="limit history to one observer-local subj_ reference",
    )
    relation_decision_list.set_defaults(func=cmd_relation_decision_list)

    relation_link = sub.add_parser(
        "relation-link",
        help="link an observed Actor to a local Subject hypothesis",
    )
    relation_link.add_argument(
        "actor",
        help="observed Node or typed opaque Actor ID",
    )
    relation_link.add_argument("subject", help="observer-local subj_ reference")
    relation_link.add_argument(
        "--confidence",
        type=int,
        required=True,
        help="0-100 confidence that the Actor belongs to this Subject",
    )
    relation_link.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    relation_link.set_defaults(func=cmd_relation_link)

    relation_observe_actor = sub.add_parser(
        "relation-observe-actor",
        help="record an opaque external Actor under an explicit local assertion",
    )
    relation_observe_actor.add_argument(
        "actor",
        help="opaque act_<namespace>_<32-hex> Actor ID; never a raw account ID",
    )
    relation_observe_actor.add_argument(
        "--kind",
        required=True,
        help="source-domain label such as human.local or agent.external",
    )
    relation_observe_actor.add_argument(
        "--label",
        help="optional local display label; do not use raw account identifiers",
    )
    relation_observe_actor.add_argument(
        "--confidence",
        type=int,
        default=50,
        help="0-100 confidence in the initial local Subject hypothesis",
    )
    relation_observe_actor.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    relation_observe_actor.set_defaults(func=cmd_relation_observe_actor)

    relation_actor_revoke = sub.add_parser(
        "relation-actor-revoke",
        help="revoke one locally observed opaque external Actor",
    )
    relation_actor_revoke.add_argument(
        "actor",
        help="complete opaque act_ Actor ID",
    )
    relation_actor_revoke.add_argument(
        "--confirm",
        required=True,
        help="repeat the complete Actor ID",
    )
    relation_actor_revoke.add_argument(
        "--reason",
        required=True,
        help="bounded local reason or evidence reference; not raw private content",
    )
    relation_actor_revoke.set_defaults(func=cmd_relation_actor_revoke)

    relation_circle = sub.add_parser(
        "relation-circle",
        help="set the observer-local circle for one Subject hypothesis",
    )
    relation_circle.add_argument("subject", help="observer-local subj_ reference")
    relation_circle.add_argument("circle", choices=RELATION_CIRCLES)
    relation_circle.add_argument(
        "--confidence",
        type=int,
        required=True,
        help="0-100 confidence in this relationship estimate",
    )
    relation_circle.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    relation_circle.add_argument(
        "--label",
        action="append",
        default=[],
        help="local relationship label; may be repeated",
    )
    relation_circle.set_defaults(func=cmd_relation_circle)

    relation_trust = sub.add_parser(
        "relation-trust",
        help="set contextual trust for one Subject hypothesis",
    )
    relation_trust.add_argument("subject", help="observer-local subj_ reference")
    relation_trust.add_argument("context", help="narrow trust context")
    relation_trust.add_argument("--estimate", type=int, required=True)
    relation_trust.add_argument("--confidence", type=int, required=True)
    relation_trust.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    relation_trust.set_defaults(func=cmd_relation_trust)

    relation_end = sub.add_parser(
        "relation-end",
        help="end one observer-local relationship estimate without revoking Actors",
    )
    relation_end.add_argument("subject", help="observer-local subj_ reference")
    relation_end.add_argument(
        "--confirm",
        required=True,
        help="repeat the complete Subject reference",
    )
    relation_end.add_argument(
        "--reason",
        required=True,
        help="bounded local reason or evidence reference; not raw private content",
    )
    relation_end.set_defaults(func=cmd_relation_end)

    relation_pause = sub.add_parser(
        "relation-pause",
        help="mark one observer-local relationship dormant without revoking Actors",
    )
    relation_pause.add_argument("subject", help="observer-local subj_ reference")
    relation_pause.add_argument(
        "--confirm",
        required=True,
        help="repeat the complete Subject reference",
    )
    relation_pause.add_argument(
        "--reason",
        required=True,
        help="bounded local reason or evidence reference; not raw private content",
    )
    relation_pause.set_defaults(func=cmd_relation_pause)

    relation_propose = sub.add_parser(
        "relation-propose",
        help="sign a relationship proposal addressed to one Actor",
    )
    relation_propose.add_argument("peer", help="complete intended peer Actor Node ID")
    relation_propose.add_argument(
        "circle",
        choices=RELATION_CIRCLES[1:],
        help="mutually proposed social circle",
    )
    relation_propose.add_argument("--out", type=Path, required=True)
    relation_propose.add_argument(
        "--ttl",
        type=int,
        default=3600,
        help="acceptance window in seconds",
    )
    relation_propose.add_argument(
        "--label",
        action="append",
        default=[],
        help="public mutual relationship label; may be repeated",
    )
    relation_propose.set_defaults(func=cmd_relation_propose)

    relation_accept = sub.add_parser(
        "relation-accept",
        help="counter-sign a relationship proposal addressed to this Actor",
    )
    relation_accept.add_argument("proposal", type=Path)
    relation_accept.add_argument("--out", type=Path, required=True)
    relation_accept.set_defaults(func=cmd_relation_accept)

    relation_import = sub.add_parser(
        "relation-import",
        help="verify and project a mutually signed relationship claim",
    )
    relation_import.add_argument("claim", type=Path)
    relation_import.set_defaults(func=cmd_relation_import)

    relation_claim_list = sub.add_parser(
        "relation-claim-list",
        help="list stored mutually signed relationship claim summaries",
    )
    relation_claim_list.set_defaults(func=cmd_relation_claim_list)

    relation_claim_withdraw = sub.add_parser(
        "relation-claim-withdraw",
        help="sign withdrawal of one locally stored mutual relationship claim",
    )
    relation_claim_withdraw.add_argument(
        "claim_id",
        help="locally stored mrel_ claim ID",
    )
    relation_claim_withdraw.add_argument(
        "--out",
        type=Path,
        required=True,
        help="path for the public signed withdrawal object",
    )
    relation_claim_withdraw.set_defaults(func=cmd_relation_claim_withdraw)

    relation_claim_withdraw_import = sub.add_parser(
        "relation-claim-withdraw-import",
        help="verify a participant withdrawal for a locally stored relationship claim",
    )
    relation_claim_withdraw_import.add_argument("withdrawal", type=Path)
    relation_claim_withdraw_import.set_defaults(func=cmd_relation_claim_withdraw_import)

    subject_supersede = sub.add_parser(
        "subject-supersede",
        help="replace one local Subject hypothesis while preserving its lineage",
    )
    subject_supersede.add_argument(
        "subject",
        help="active observer-local subj_ reference",
    )
    subject_supersede.add_argument(
        "--confidence",
        type=int,
        required=True,
        help="0-100 confidence in this hypothesis revision",
    )
    subject_supersede.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    subject_supersede.add_argument(
        "--label",
        action="append",
        default=[],
        help="label added to the replacement hypothesis; may be repeated",
    )
    subject_supersede.set_defaults(func=cmd_subject_supersede)

    subject_merge = sub.add_parser(
        "subject-merge",
        help="replace multiple local Subject hypotheses with one hypothesis",
    )
    subject_merge.add_argument(
        "subjects",
        nargs="+",
        help="two or more active observer-local subj_ references",
    )
    subject_merge.add_argument(
        "--confidence",
        type=int,
        required=True,
        help="0-100 confidence in this hypothesis revision",
    )
    subject_merge.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    subject_merge.add_argument(
        "--inherit",
        default="",
        help=(
            "explicit source Subject whose relationship may be inherited; "
            "otherwise the replacement starts as known"
        ),
    )
    subject_merge.add_argument(
        "--label",
        action="append",
        default=[],
        help="label added to the replacement hypothesis; may be repeated",
    )
    subject_merge.set_defaults(func=cmd_subject_merge)

    subject_split = sub.add_parser(
        "subject-split",
        help="replace one local Subject hypothesis with an exact Actor partition",
    )
    subject_split.add_argument(
        "subject",
        help="active observer-local subj_ reference",
    )
    subject_split.add_argument(
        "--group",
        action="append",
        required=True,
        help="comma-separated Actor IDs; repeat for every partition group",
    )
    subject_split.add_argument(
        "--confidence",
        type=int,
        required=True,
        help="0-100 confidence in this hypothesis revision",
    )
    subject_split.add_argument(
        "--evidence",
        required=True,
        help="bounded local evidence reference; not raw private content",
    )
    subject_split.add_argument(
        "--inherit-group",
        type=int,
        help=(
            "1-based group allowed to inherit the source relationship; "
            "all other groups start as known"
        ),
    )
    subject_split.add_argument(
        "--label",
        action="append",
        default=[],
        help="label added to each replacement hypothesis; may be repeated",
    )
    subject_split.set_defaults(func=cmd_subject_split)

    prekey_generate = sub.add_parser(
        "prekey-generate",
        help="generate and export a signed batch of one-time X25519 prekeys",
    )
    prekey_generate.add_argument("path", type=Path)
    prekey_generate.add_argument(
        "--peer", default="", help="intended peer; inferred only with one pinned peer"
    )
    prekey_generate.add_argument("--count", type=int, default=100)
    prekey_generate.add_argument("--ttl-days", type=float, default=30.0)
    prekey_generate.set_defaults(func=cmd_prekey_generate)

    prekey_import = sub.add_parser(
        "prekey-import",
        help="verify and import a pinned peer's signed prekey bundle",
    )
    prekey_import.add_argument("path", type=Path)
    prekey_import.set_defaults(func=cmd_prekey_import)

    prekey_status = sub.add_parser(
        "prekey-status", help="show local and peer prekey inventory without key material"
    )
    prekey_status.add_argument("--peer", default="")
    prekey_status.set_defaults(func=cmd_prekey_status)

    prekey_policy = sub.add_parser(
        "prekey-policy",
        help="choose prefer, require, or disable for outbound one-time prekeys",
    )
    prekey_policy.add_argument("policy", choices=["prefer", "require", "disable"])
    prekey_policy.set_defaults(func=cmd_prekey_policy)

    prekey_config = sub.add_parser(
        "prekey-config",
        help="configure automatic peer-scoped prekey replenishment",
    )
    prekey_config.add_argument(
        "--auto", action=argparse.BooleanOptionalAction, default=None
    )
    prekey_config.add_argument("--low-watermark", type=int)
    prekey_config.add_argument("--batch-size", type=int)
    prekey_config.add_argument("--request-interval", type=float)
    prekey_config.add_argument("--ttl-days", type=float)
    prekey_config.set_defaults(func=cmd_prekey_config)

    prekey_replenish = sub.add_parser(
        "prekey-replenish",
        help="force an in-band signed prekey replenishment request",
    )
    prekey_replenish.add_argument("peer")
    prekey_replenish.set_defaults(func=cmd_prekey_replenish)

    prekey_migrate = sub.add_parser(
        "prekey-migrate",
        help="scope single-peer v1 keys or explicitly retire ambiguous shared keys",
    )
    prekey_migrate.add_argument(
        "--retire-shared",
        action="store_true",
        help="erase active unscoped v1 keys when multiple peers are pinned",
    )
    prekey_migrate.set_defaults(func=cmd_prekey_migrate)

    send = sub.add_parser("send", help="queue an end-to-end encrypted message")
    send.add_argument("destination")
    send.add_argument("--kind", default="message")
    send.add_argument("--text")
    send.add_argument("--json-body")
    send.add_argument("--file", type=Path)
    send.add_argument("--stdin", action="store_true")
    send.add_argument(
        "--stdin-format", choices=["binary", "text", "json"], default="binary"
    )
    send.add_argument("--ttl", type=int, default=86400)
    send.add_argument("--max-hops", type=int)
    send.add_argument("--causal", action="append", default=[])
    send.add_argument("--reply-to", default="")
    send.add_argument(
        "--qos",
        choices=["control", "interactive", "normal", "bulk"],
        default="normal",
    )
    send.set_defaults(func=cmd_send)

    inbox = sub.add_parser("inbox", help="read decrypted local messages")
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--unread", action="store_true")
    inbox.add_argument("--trusted-only", action="store_true")
    inbox.add_argument("--mark-read", action="store_true")
    inbox.set_defaults(func=cmd_inbox)

    consumer_open = sub.add_parser(
        "consumer-open",
        help="create an idempotent durable local Agent consumer group",
    )
    consumer_open.add_argument("group")
    consumer_open.add_argument(
        "--start", choices=["latest", "earliest"], default="latest"
    )
    consumer_open.add_argument("--kind-prefix", default="")
    consumer_open.add_argument("--sender", default="")
    consumer_open.add_argument(
        "--trusted-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    consumer_open.add_argument("--include-transient", action="store_true")
    consumer_open.set_defaults(func=cmd_consumer_open)

    consumer_claim = sub.add_parser(
        "consumer-claim",
        help="atomically lease messages from a durable consumer group",
    )
    consumer_claim.add_argument("group")
    consumer_claim.add_argument("--owner", required=True)
    consumer_claim.add_argument("--limit", type=int, default=1)
    consumer_claim.add_argument("--lease-seconds", type=float, default=300.0)
    consumer_claim.set_defaults(func=cmd_consumer_claim)

    consumer_settle = sub.add_parser(
        "consumer-settle",
        help="ACK or NACK an owned consumer claim",
    )
    consumer_settle.add_argument("group")
    consumer_settle.add_argument("claim_token")
    consumer_settle.add_argument("--owner", required=True)
    consumer_settle.add_argument("--action", choices=["ack", "nack"], required=True)
    consumer_settle.add_argument("--retry-seconds", type=float, default=0.0)
    consumer_settle.add_argument("--error", default="")
    consumer_settle.set_defaults(func=cmd_consumer_settle)

    consumer_renew = sub.add_parser(
        "consumer-renew",
        help="extend an owned consumer claim lease",
    )
    consumer_renew.add_argument("group")
    consumer_renew.add_argument("claim_token")
    consumer_renew.add_argument("--owner", required=True)
    consumer_renew.add_argument("--lease-seconds", type=float, default=300.0)
    consumer_renew.set_defaults(func=cmd_consumer_renew)

    consumer_status = sub.add_parser(
        "consumer-status",
        help="show durable consumer availability and delivery states",
    )
    consumer_status.add_argument("group")
    consumer_status.set_defaults(func=cmd_consumer_status)

    status = sub.add_parser("status", help="show node and spool status")
    status.set_defaults(func=cmd_status)

    direct_proxy = sub.add_parser(
        "direct-proxy", help="show, set, or clear the SOCKS5 direct-link proxy"
    )
    direct_proxy.add_argument("url", nargs="?")
    direct_proxy.add_argument("--clear", action="store_true")
    direct_proxy.add_argument("--allow-remote", action="store_true")
    direct_proxy.add_argument("--username-env", default="")
    direct_proxy.add_argument("--password-env", default="")
    direct_proxy.set_defaults(func=cmd_direct_proxy)

    dialer_add = sub.add_parser(
        "dialer-add",
        help="add an independently measured raw/SOCKS/stdio direct dialer",
    )
    dialer_add.add_argument("name")
    dialer_add.add_argument(
        "--type", choices=["raw", "socks5", "socks5h", "stdio"], required=True
    )
    dialer_add.add_argument("--url")
    dialer_add.add_argument("--executable")
    dialer_add.add_argument("--arg", action="append", default=[])
    dialer_add.add_argument("--env", action="append", default=[])
    dialer_add.add_argument("--startup-timeout", type=float, default=5.0)
    dialer_add.add_argument("--priority", type=int, default=100)
    dialer_add.add_argument("--allow-remote", action="store_true")
    dialer_add.add_argument("--username-env", default="")
    dialer_add.add_argument("--password-env", default="")
    dialer_add.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=True
    )
    dialer_add.set_defaults(func=cmd_dialer_add)

    dialer_list = sub.add_parser(
        "dialer-list", help="show effective independently measured direct dialers"
    )
    dialer_list.set_defaults(func=cmd_dialer_list)

    dialer_set = sub.add_parser(
        "dialer-set", help="change priority or enabled state of a direct dialer"
    )
    dialer_set.add_argument("name")
    dialer_set.add_argument("--priority", type=int)
    dialer_set.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=None
    )
    dialer_set.set_defaults(func=cmd_dialer_set)

    dialer_remove = sub.add_parser(
        "dialer-remove", help="remove an explicit direct dialer"
    )
    dialer_remove.add_argument("name")
    dialer_remove.set_defaults(func=cmd_dialer_remove)

    locator_config = sub.add_parser(
        "locator-config",
        help="configure signed scoped addresses and local host/LAN contexts",
    )
    locator_config.add_argument("--add-context", action="append", default=[])
    locator_config.add_argument("--remove-context", action="append", default=[])
    locator_config.add_argument(
        "--advertise",
        action="append",
        default=None,
        help="replace advertised locators; repeat for multiple paths",
    )
    locator_config.add_argument("--clear-advertise", action="store_true")
    locator_config.set_defaults(func=cmd_locator_config)

    sync = sub.add_parser("sync", help="perform one outbound synchronization pass")
    sync.set_defaults(func=cmd_sync)

    probe = sub.add_parser(
        "probe", help="measure end-to-end delivery and record the acknowledged path"
    )
    probe.add_argument("destination")
    probe.add_argument("--timeout", type=float, default=15.0)
    probe.add_argument("--interval", type=float, default=0.5)
    probe.add_argument("--carrier-grace", type=float, default=3.0)
    probe.add_argument("--payload-bytes", type=int, default=0)
    probe.add_argument(
        "--qos",
        choices=["control", "interactive", "normal", "bulk"],
        default="control",
    )
    probe.set_defaults(func=cmd_probe)

    dialer_probe = sub.add_parser(
        "dialer-probe",
        help="authenticate direct dialer paths without exchanging business packets",
    )
    dialer_probe.add_argument("destination")
    dialer_probe.add_argument(
        "--dialer", action="append", default=[], help="limit to a named dialer"
    )
    dialer_probe.add_argument("--require-all", action="store_true")
    dialer_probe.set_defaults(func=cmd_dialer_probe)

    benchmark = sub.add_parser(
        "benchmark",
        help="run a repeatable end-to-end probe series and write JSONL evidence",
    )
    benchmark.add_argument("destination")
    benchmark.add_argument("--count", type=int, default=10)
    benchmark.add_argument("--timeout", type=float, default=15.0)
    benchmark.add_argument("--spacing", type=float, default=1.0)
    benchmark.add_argument("--carrier-grace", type=float, default=3.0)
    benchmark.add_argument("--payload-bytes", type=int, default=0)
    benchmark.add_argument(
        "--qos",
        choices=["control", "interactive", "normal", "bulk"],
        default="control",
    )
    benchmark.add_argument("--out", type=Path)
    benchmark.add_argument("--min-success-rate", type=float, default=1.0)
    benchmark.set_defaults(func=cmd_benchmark)

    monitor = sub.add_parser(
        "monitor",
        help="run jittered low-frequency probes and append durable JSONL observations",
    )
    monitor.add_argument("destination")
    monitor.add_argument("--out", type=Path, required=True)
    monitor.add_argument("--interval", type=float, default=60.0)
    monitor.add_argument("--jitter", type=float, default=0.25)
    monitor.add_argument("--timeout", type=float, default=20.0)
    monitor.add_argument("--carrier-grace", type=float, default=3.0)
    monitor.add_argument("--payload-bytes", type=int, default=0)
    monitor.add_argument("--max-observations", type=int, default=0)
    monitor.add_argument(
        "--qos",
        choices=["control", "interactive", "normal", "bulk"],
        default="control",
    )
    monitor.set_defaults(func=cmd_monitor)

    carrier_sync = sub.add_parser(
        "carrier-sync",
        help="perform one encrypted directory-carrier synchronization pass",
    )
    carrier_sync.add_argument(
        "root", type=Path, help="shared or externally replicated drop directory"
    )
    carrier_sync.add_argument("--peer", action="append", default=[])
    carrier_sync.add_argument("--limit", type=int, default=128)
    carrier_sync.add_argument("--retry-seconds", type=float, default=300.0)
    carrier_sync.set_defaults(func=cmd_carrier_sync)

    carrier_serve = sub.add_parser(
        "carrier-serve",
        help="poll an encrypted directory carrier without opening a listener",
    )
    carrier_serve.add_argument(
        "root", type=Path, help="shared or externally replicated drop directory"
    )
    carrier_serve.add_argument("--peer", action="append", default=[])
    carrier_serve.add_argument("--limit", type=int, default=128)
    carrier_serve.add_argument("--retry-seconds", type=float, default=300.0)
    carrier_serve.add_argument("--interval", type=float, default=2.0)
    carrier_serve.add_argument("--jitter", type=float, default=0.25)
    carrier_serve.add_argument("--idle-backoff-max", type=float, default=4.0)
    carrier_serve.set_defaults(func=cmd_carrier_serve)

    carrier_add = sub.add_parser(
        "carrier-add", help="add a persistent adaptive carrier"
    )
    carrier_add.add_argument("target", help="directory path or WebDAV base URL")
    carrier_add.add_argument(
        "--type", choices=["directory", "webdav", "ahub"], default="directory"
    )
    carrier_add.add_argument("--name", required=True)
    carrier_add.add_argument("--peer", action="append", default=[])
    carrier_add.add_argument(
        "--mode", choices=["fallback", "always", "receive-only"], default="fallback"
    )
    carrier_add.add_argument("--interval", type=float)
    carrier_add.add_argument("--jitter", type=float, default=0.25)
    carrier_add.add_argument("--idle-backoff-max", type=float, default=4.0)
    carrier_add.add_argument("--retry-seconds", type=float, default=300.0)
    carrier_add.add_argument("--priority", type=int)
    carrier_add.add_argument("--timeout", type=float, default=15.0)
    carrier_add.add_argument("--claim-lease-seconds", type=float, default=30.0)
    carrier_add.add_argument(
        "--live-relay",
        action="store_true",
        help="enable peer-scoped live Relay listeners for an Ahub carrier",
    )
    carrier_add.add_argument(
        "--relay-reservation-ttl-seconds",
        type=float,
        default=900.0,
    )
    carrier_add.add_argument(
        "--relay-session-seconds",
        type=float,
        default=300.0,
    )
    carrier_add.add_argument(
        "--relay-bytes-each-direction",
        type=int,
        default=64 * 1024 * 1024,
    )
    carrier_add.add_argument(
        "--relay-listener-retry-seconds",
        type=float,
        default=2.0,
    )
    carrier_add.add_argument("--bearer-env", default="")
    carrier_add.add_argument("--username-env", default="")
    carrier_add.add_argument("--password-env", default="")
    carrier_add.add_argument("--allow-insecure-http", action="store_true")
    carrier_add.set_defaults(func=cmd_carrier_add)

    carrier_list = sub.add_parser(
        "carrier-list", help="list persistent adaptive carriers"
    )
    carrier_list.set_defaults(func=cmd_carrier_list)

    carrier_remove = sub.add_parser(
        "carrier-remove", help="remove a persistent adaptive carrier"
    )
    carrier_remove.add_argument("name")
    carrier_remove.set_defaults(func=cmd_carrier_remove)

    routing_config = sub.add_parser(
        "routing-config", help="configure adaptive routing thresholds"
    )
    routing_config.add_argument("--failure-threshold", type=int)
    routing_config.add_argument("--recovery-threshold", type=int)
    routing_config.add_argument("--carrier-failure-threshold", type=int)
    routing_config.add_argument("--carrier-recovery-threshold", type=int)
    routing_config.add_argument("--carrier-replica-count", type=int)
    routing_config.add_argument("--direct-retry-interval", type=float)
    routing_config.add_argument("--direct-race-width", type=int)
    routing_config.add_argument("--direct-race-delay", type=float)
    routing_config.add_argument("--direct-idle-probe-interval", type=float)
    routing_config.add_argument("--direct-probe-jitter", type=float)
    routing_config.add_argument("--direct-idle-backoff-max", type=float)
    routing_config.add_argument("--fallback-probe-interval", type=float)
    routing_config.add_argument("--fallback-probe-jitter", type=float)
    routing_config.add_argument("--sync-interval", type=float)
    routing_config.add_argument("--sync-jitter", type=float)
    routing_config.add_argument("--cooldown", type=float)
    routing_config.add_argument(
        "--listen", action=argparse.BooleanOptionalAction, default=None
    )
    routing_config.add_argument(
        "--direct", action=argparse.BooleanOptionalAction, default=None
    )
    routing_config.set_defaults(func=cmd_routing_config)

    discord_config = sub.add_parser(
        "discord-social-config",
        help=(
            "configure the WSL Discord social bridge, scoring thresholds, "
            "and one trusted Anet signal destination"
        ),
    )
    discord_config.add_argument("--guild")
    discord_config.add_argument("--channel", action="append", default=[])
    discord_config.add_argument("--destination")
    discord_config.add_argument("--clear-destination", action="store_true")
    discord_config.add_argument("--token-env")
    discord_config.add_argument(
        "--content-mode",
        choices=["metadata", "mentions"],
    )
    discord_config.add_argument("--poll-seconds", type=float)
    discord_config.add_argument("--signal-ttl", type=int)
    discord_config.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    discord_config.add_argument("--surface-score", type=int)
    discord_config.add_argument("--surface-confidence", type=int)
    discord_config.add_argument("--reply-score", type=int)
    discord_config.add_argument("--reply-confidence", type=int)
    discord_config.add_argument("--amplify-score", type=int)
    discord_config.add_argument("--amplify-confidence", type=int)
    discord_config.add_argument("--connect-score", type=int)
    discord_config.add_argument("--connect-confidence", type=int)
    discord_config.set_defaults(func=cmd_discord_social_config)

    discord_status = sub.add_parser(
        "discord-social-status",
        help="show redacted Discord social bridge and ledger status",
    )
    discord_status.set_defaults(func=cmd_discord_social_status)

    discord_actor = sub.add_parser(
        "discord-social-actor",
        help="show one pseudonymous Discord actor's evidence and score",
    )
    discord_actor.add_argument("actor_key")
    discord_actor.set_defaults(func=cmd_discord_social_actor)

    discord_label = sub.add_parser(
        "discord-social-label",
        help="add or remove operator-owned labels on one Discord actor",
    )
    discord_label.add_argument("actor_key")
    discord_label.add_argument("--add", action="append", default=[])
    discord_label.add_argument("--remove", action="append", default=[])
    discord_label.add_argument("--source", default="operator")
    discord_label.set_defaults(func=cmd_discord_social_label)

    discord_project = sub.add_parser(
        "discord-social-project",
        help=(
            "replay durable Discord metadata into the local Actor/Subject model"
        ),
    )
    discord_project.add_argument("--limit", type=int, default=1000)
    discord_project.set_defaults(func=cmd_discord_social_project)

    discord_reply = sub.add_parser(
        "discord-social-reply",
        help="send one threshold-gated, mention-suppressed Discord reply",
    )
    discord_reply.add_argument("event_key")
    discord_reply.add_argument("--text")
    discord_reply.add_argument("--stdin", action="store_true")
    discord_reply.set_defaults(func=cmd_discord_social_reply)

    serve = sub.add_parser(
        "serve", help="run the peer listener and continuous synchronization"
    )
    serve.set_defaults(func=cmd_serve)

    control_sync = sub.add_parser(
        "control-sync",
        help="fetch and apply one remote JSON control page",
    )
    control_sync.add_argument("--url", help="control page URL; otherwise use node settings")
    control_sync.add_argument("--control-key-id", help="locally pinned control publisher key ID")
    control_sync.add_argument(
        "--control-public-key",
        help="base64url Ed25519 public key for the control publisher",
    )
    control_sync.add_argument(
        "--no-software",
        action="store_true",
        help="apply config and peers but do not install a package update",
    )
    control_sync.set_defaults(func=cmd_control_sync)

    control_verify = sub.add_parser(
        "control-verify",
        help="verify one remote JSON control page without applying it",
    )
    control_verify.add_argument(
        "--url", help="control page URL; otherwise use node settings"
    )
    control_verify.add_argument(
        "--control-key-id", help="locally pinned control publisher key ID"
    )
    control_verify.add_argument(
        "--control-public-key",
        help="base64url Ed25519 public key for the control publisher",
    )
    control_verify.set_defaults(func=cmd_control_verify)

    supervisor = sub.add_parser(
        "supervisor",
        help="run the remote control client and supervise an Anet server child",
    )
    supervisor.add_argument("--url", help="control page URL; otherwise use node settings")
    supervisor.add_argument("--control-key-id", help="locally pinned control publisher key ID")
    supervisor.add_argument(
        "--control-public-key",
        help="base64url Ed25519 public key for the control publisher",
    )
    supervisor.add_argument("--interval", type=float)
    supervisor.add_argument(
        "--no-software",
        action="store_true",
        help="supervise the node but do not install package updates",
    )
    supervisor.add_argument(
        "--once",
        action="store_true",
        help="sync once and exit without starting the server child",
    )
    supervisor.set_defaults(func=cmd_supervisor)

    supervisor_status = sub.add_parser(
        "supervisor-status",
        help="inspect durable supervisor, control-sync, and server-child health",
    )
    supervisor_status.set_defaults(func=cmd_supervisor_status)

    wake_bridge = sub.add_parser(
        "wake-bridge",
        help="wake one local runtime session when a durable consumer has messages",
    )
    wake_bridge.add_argument("--group", required=True)
    wake_bridge.add_argument("--endpoint", required=True)
    wake_bridge.add_argument("--token-env", default="ANET_WAKE_TOKEN")
    wake_bridge.add_argument("--start", choices=["latest", "earliest"], default="latest")
    wake_bridge.add_argument("--poll-seconds", type=float, default=0.25)
    wake_bridge.add_argument("--rearm-seconds", type=float, default=30.0)
    wake_bridge.add_argument("--startup-delay-seconds", type=float, default=5.0)
    wake_bridge.set_defaults(func=cmd_wake_bridge)

    ahub_allow = sub.add_parser(
        "ahub-allow",
        help="locally allow one complete Node ID on a separate Ahub root",
    )
    ahub_allow.add_argument("--root", type=Path, required=True)
    ahub_allow.add_argument("node_id")
    ahub_allow.set_defaults(func=cmd_ahub_allow)

    ahub_disallow = sub.add_parser(
        "ahub-disallow",
        help="disable Ahub access without deleting retained ciphertext",
    )
    ahub_disallow.add_argument("--root", type=Path, required=True)
    ahub_disallow.add_argument("node_id")
    ahub_disallow.add_argument(
        "--confirm", required=True, help="repeat the complete Node ID"
    )
    ahub_disallow.set_defaults(func=cmd_ahub_disallow)

    ahub_nodes = sub.add_parser(
        "ahub-nodes", help="list locally provisioned Ahub Node IDs"
    )
    ahub_nodes.add_argument("--root", type=Path, required=True)
    ahub_nodes.add_argument("--include-disabled", action="store_true")
    ahub_nodes.set_defaults(func=cmd_ahub_nodes)

    ahub_status = sub.add_parser(
        "ahub-status",
        help="show bounded local Ahub health and queue metrics",
    )
    ahub_status.add_argument("--root", type=Path, required=True)
    ahub_status.set_defaults(func=cmd_ahub_status)

    ahub_purge = sub.add_parser(
        "ahub-purge",
        help="delete only expired packets/nonces and release expired claims",
    )
    ahub_purge.add_argument("--root", type=Path, required=True)
    ahub_purge.set_defaults(func=cmd_ahub_purge)

    ahub_checkpoint = sub.add_parser(
        "ahub-checkpoint",
        help="checkpoint both Ahub databases before an offline backup",
    )
    ahub_checkpoint.add_argument("--root", type=Path, required=True)
    ahub_checkpoint.set_defaults(func=cmd_ahub_checkpoint)

    ahub_serve = sub.add_parser(
        "ahub-serve",
        help="serve the no-private-key Ahub ASGI API",
    )
    ahub_serve.add_argument("--root", type=Path, required=True)
    ahub_serve.add_argument("--host", default="127.0.0.1")
    ahub_serve.add_argument("--port", type=int, default=8422)
    ahub_serve.add_argument("--allow-non-loopback", action="store_true")
    ahub_serve.add_argument("--limit-concurrency", type=int, default=100)
    ahub_serve.add_argument("--keep-alive-seconds", type=int, default=5)
    ahub_serve.add_argument(
        "--rate-limit-per-minute",
        type=int,
        default=600,
        help="maximum HTTP/WebSocket handshakes per peer per minute",
    )
    ahub_serve.add_argument(
        "--rate-limit-burst",
        type=int,
        default=120,
        help="initial burst allowance per peer",
    )
    ahub_serve.set_defaults(func=cmd_ahub_serve)

    bundle_export = sub.add_parser(
        "bundle-export", help="export sealed packets for offline carriage"
    )
    bundle_export.add_argument("path", type=Path)
    bundle_export.add_argument("--destination", default="")
    bundle_export.set_defaults(func=cmd_bundle_export)

    bundle_import = sub.add_parser(
        "bundle-import", help="import sealed packets from an offline bundle"
    )
    bundle_import.add_argument("path", type=Path)
    bundle_import.set_defaults(func=cmd_bundle_import)

    doctor = sub.add_parser(
        "doctor", help="verify identity, TLS material, trust store, and spool"
    )
    doctor.set_defaults(func=cmd_doctor)

    mcp = sub.add_parser("mcp", help="run the Anet MCP adapter on stdio")
    mcp.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except Exception as exc:
        if args.verbose:
            raise
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
