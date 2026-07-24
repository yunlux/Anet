from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

from anet.control_plane import (
    GENESIS_DIGEST,
    ControlPlaneStore,
    ControlPlaneRevisionTracker,
    HumanDeviceGrant,
    HumanDeviceRevocation,
    HumanPrincipalIdentity,
    NodeDescriptor,
    ReachabilityRecord,
    derive_human_id,
    issue_human_device_grant,
    issue_human_device_revocation,
    issue_node_descriptor,
    issue_reachability_record,
)
from anet.encoding import canonical_pack
from anet.identity import Identity, PeerCard


NOW = int(time.time() * 1000)
HOUR_MS = 60 * 60 * 1000


def descriptor(
    identity: Identity,
    *,
    capabilities: tuple[str, ...] = ("agent.task",),
    sequence: int = 1,
    previous_digest: bytes = GENESIS_DIGEST,
    issued_ms: int = NOW,
) -> NodeDescriptor:
    return issue_node_descriptor(
        identity,
        capabilities=capabilities,
        sequence=sequence,
        previous_digest=previous_digest,
        issued_ms=issued_ms,
        ttl_ms=HOUR_MS,
    )


def reachability(
    identity: Identity,
    current: NodeDescriptor,
    *,
    sequence: int = 1,
    previous_digest: bytes = GENESIS_DIGEST,
    session_id: str = "1" * 32,
    candidates: tuple[str, ...] = ("tls://127.0.0.1:4242",),
    issued_ms: int = NOW,
) -> ReachabilityRecord:
    return issue_reachability_record(
        identity,
        current,
        protocol_versions=("anet/1",),
        candidates=candidates,
        relay_reservation="relay_123",
        capability_digest=hashlib.sha256(b"capabilities").digest(),
        sequence=sequence,
        previous_digest=previous_digest,
        session_id=session_id,
        issued_ms=issued_ms,
        ttl_ms=5 * 60 * 1000,
    )


def test_v2_descriptor_preserves_v1_node_identity_and_wire_boundary() -> None:
    identity = Identity.generate("mobile")
    card = identity.card(
        addresses=("tls://127.0.0.1:4242",),
        capabilities=("agent-message-v0",),
    )
    current = descriptor(identity, capabilities=())

    assert current.node_id == card.node_id == identity.node_id
    assert current.sign_public == card.sign_public
    assert current.box_public == card.box_public
    assert "addresses" not in current.to_dict()
    assert NodeDescriptor.from_dict(current.to_dict()) == current
    assert PeerCard.from_dict(card.to_dict()) == card
    card.verify()


def test_descriptor_chain_is_idempotent_and_rejects_fork_gap_and_rollback() -> None:
    identity = Identity.generate("node")
    first = descriptor(identity)
    second = descriptor(
        identity,
        capabilities=("agent.task", "link-health-v1"),
        sequence=2,
        previous_digest=first.digest,
        issued_ms=NOW + 1,
    )
    fork = descriptor(
        identity,
        capabilities=("agent.task", "other"),
        sequence=2,
        previous_digest=first.digest,
        issued_ms=NOW + 2,
    )
    gap = descriptor(
        identity,
        sequence=4,
        previous_digest=second.digest,
        issued_ms=NOW + 3,
    )

    tracker = ControlPlaneRevisionTracker()
    assert tracker.accept_descriptor(first, now=NOW)
    assert not tracker.accept_descriptor(first, now=NOW)
    assert tracker.accept_descriptor(second, now=NOW + 1)
    with pytest.raises(ValueError, match="fork"):
        tracker.accept_descriptor(fork, now=NOW + 2)
    with pytest.raises(ValueError, match="gap"):
        tracker.accept_descriptor(gap, now=NOW + 3)
    with pytest.raises(ValueError, match="stale"):
        tracker.accept_descriptor(first, now=NOW + 3)


def test_reachability_rolls_session_without_allowing_old_session_replay() -> None:
    identity = Identity.generate("phone")
    current = descriptor(identity)
    first = reachability(identity, current)
    second = reachability(
        identity,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="2" * 32,
        candidates=("tls://192.0.2.10:4242?scope=wan&priority=20",),
        issued_ms=NOW + 1,
    )

    tracker = ControlPlaneRevisionTracker()
    tracker.accept_descriptor(current, now=NOW)
    assert tracker.accept_reachability(first, current, now=NOW)
    assert tracker.accept_reachability(second, current, now=NOW + 1)
    with pytest.raises(ValueError, match="stale"):
        tracker.accept_reachability(first, current, now=NOW + 2)


def test_reachability_rejects_fork_stale_descriptor_and_wrong_signature() -> None:
    identity = Identity.generate("phone")
    current = descriptor(identity)
    first = reachability(identity, current)
    fork_a = reachability(
        identity,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="2" * 32,
        issued_ms=NOW + 1,
    )
    fork_b = reachability(
        identity,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="3" * 32,
        issued_ms=NOW + 2,
    )
    tracker = ControlPlaneRevisionTracker()
    tracker.accept_descriptor(current, now=NOW)
    tracker.accept_reachability(first, current, now=NOW)
    tracker.accept_reachability(fork_a, current, now=NOW + 1)
    with pytest.raises(ValueError, match="fork"):
        tracker.accept_reachability(fork_b, current, now=NOW + 2)

    next_descriptor = descriptor(
        identity,
        sequence=2,
        previous_digest=current.digest,
        issued_ms=NOW + 3,
    )
    tracker.accept_descriptor(next_descriptor, now=NOW + 3)
    old_descriptor_record = reachability(
        identity,
        current,
        sequence=3,
        previous_digest=fork_a.digest,
        session_id="4" * 32,
        issued_ms=NOW + 4,
    )
    with pytest.raises(ValueError, match="current node descriptor"):
        tracker.accept_reachability(
            old_descriptor_record, current, now=NOW + 4
        )

    forged = replace(first, signature=Identity.generate("attacker").sign(
        canonical_pack(first.signing_fields())
    ))
    with pytest.raises(InvalidSignature):
        forged.verify(current, now=NOW)


def test_control_objects_reject_expiry_unknown_fields_and_boolean_revision() -> None:
    identity = Identity.generate("node")
    current = descriptor(identity, issued_ms=NOW - 20 * 60 * 1000)
    expired = reachability(
        identity, current, issued_ms=NOW - 10 * 60 * 1000
    )
    with pytest.raises(ValueError, match="expired"):
        expired.verify(current, now=NOW)

    unknown = current.to_dict()
    unknown["directory_override"] = "forged"
    with pytest.raises(ValueError, match="unknown fields"):
        NodeDescriptor.from_dict(unknown)

    boolean_revision = current.to_dict()
    boolean_revision["sequence"] = True
    with pytest.raises(ValueError, match="must be an integer"):
        NodeDescriptor.from_dict(boolean_revision)


def test_human_device_grant_uses_a_distinct_self_certifying_identity() -> None:
    device = Identity.generate("primary-phone")
    current = descriptor(
        device,
        capabilities=(
            "human.approval_signer",
            "human.primary_interface",
        ),
    )
    human = HumanPrincipalIdentity.generate()
    grant = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        issued_ms=NOW,
        ttl_ms=HOUR_MS,
    )

    assert grant.human_id == derive_human_id(human.sign_public)
    assert grant.human_id != device.node_id
    assert grant.human_sign_public != device.sign_public
    assert HumanDeviceGrant.from_dict(grant.to_dict(), current) == grant

    device_as_human = HumanPrincipalIdentity(device.sign_private)
    with pytest.raises(ValueError, match="must be separate"):
        issue_human_device_grant(
            device_as_human,
            current,
            capabilities=("human.approval_signer",),
            issued_ms=NOW,
            ttl_ms=HOUR_MS,
        )


def test_human_device_revocation_is_terminal_and_does_not_change_human_id() -> None:
    device = Identity.generate("lost-phone")
    current = descriptor(
        device, capabilities=("human.approval_signer",)
    )
    human = HumanPrincipalIdentity.generate()
    grant = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        issued_ms=NOW,
        ttl_ms=HOUR_MS,
    )
    revocation = issue_human_device_revocation(
        human,
        current,
        sequence=2,
        previous_digest=grant.digest,
        revoked_ms=NOW + 1,
        reason_code="device-lost",
    )
    replacement_grant = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        sequence=3,
        previous_digest=revocation.digest,
        issued_ms=NOW + 2,
        ttl_ms=HOUR_MS,
    )

    tracker = ControlPlaneRevisionTracker()
    tracker.accept_descriptor(current, now=NOW)
    assert tracker.accept_human_grant(grant, current, now=NOW)
    assert tracker.accept_human_revocation(
        revocation, current, now=NOW + 1
    )
    assert not tracker.accept_human_revocation(
        revocation, current, now=NOW + 1
    )
    with pytest.raises(ValueError, match="permanently revoked"):
        tracker.accept_human_grant(
            replacement_grant, current, now=NOW + 2
        )
    assert revocation.human_id == grant.human_id == human.human_id
    assert HumanDeviceRevocation.from_dict(
        revocation.to_dict(), current
    ) == revocation

    fresh_tracker = ControlPlaneRevisionTracker()
    fresh_tracker.accept_descriptor(current, now=NOW)
    with pytest.raises(ValueError, match="without an accepted grant"):
        fresh_tracker.accept_human_revocation(
            revocation, current, now=NOW + 1
        )


def test_control_plane_store_survives_restart_and_returns_current_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    device = Identity.generate("phone")
    current = descriptor(
        device,
        capabilities=("human.approval_signer",),
    )
    record = reachability(device, current)
    human = HumanPrincipalIdentity.generate()
    grant = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        issued_ms=NOW,
        ttl_ms=HOUR_MS,
    )

    with ControlPlaneStore(database) as store:
        assert store.accept_descriptor(current, now=NOW)
        assert store.accept_reachability(record, current, now=NOW)
        assert store.accept_human_grant(grant, current, now=NOW)

    with ControlPlaneStore(database) as reopened:
        assert reopened.current_descriptor(device.node_id, now=NOW) == current
        assert reopened.current_reachability(device.node_id, now=NOW) == record
        assert reopened.human_device_grant(
            human.human_id, device.node_id, now=NOW
        ) == grant
        assert not reopened.accept_descriptor(current, now=NOW)
        assert not reopened.accept_reachability(record, current, now=NOW)
        assert not reopened.accept_human_grant(grant, current, now=NOW)


def test_control_plane_store_rejects_old_session_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    device = Identity.generate("phone")
    current = descriptor(device)
    first = reachability(device, current)
    second = reachability(
        device,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="2" * 32,
        issued_ms=NOW + 1,
    )

    with ControlPlaneStore(database) as store:
        store.accept_descriptor(current, now=NOW)
        store.accept_reachability(first, current, now=NOW)
    with ControlPlaneStore(database) as reopened:
        reopened.accept_reachability(second, current, now=NOW + 1)
    with ControlPlaneStore(database) as reopened_again:
        with pytest.raises(ValueError, match="stale"):
            reopened_again.accept_reachability(first, current, now=NOW + 2)
        assert reopened_again.current_reachability(
            device.node_id, now=NOW + 2
        ) == second


def test_control_plane_store_concurrent_fork_has_one_durable_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    device = Identity.generate("phone")
    current = descriptor(device)
    first = reachability(device, current)
    fork_a = reachability(
        device,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="a" * 32,
        issued_ms=NOW + 1,
    )
    fork_b = reachability(
        device,
        current,
        sequence=2,
        previous_digest=first.digest,
        session_id="b" * 32,
        issued_ms=NOW + 1,
    )
    with ControlPlaneStore(database) as store:
        store.accept_descriptor(current, now=NOW)
        store.accept_reachability(first, current, now=NOW)

    def accept(candidate: ReachabilityRecord) -> str:
        try:
            with ControlPlaneStore(database) as concurrent:
                concurrent.accept_reachability(
                    candidate, current, now=NOW + 1
                )
            return "accepted"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, (fork_a, fork_b)))

    assert outcomes.count("accepted") == 1
    assert sum("fork" in item for item in outcomes) == 1
    with ControlPlaneStore(database) as final:
        assert final.current_reachability(
            device.node_id, now=NOW + 2
        ) in {fork_a, fork_b}


def test_control_plane_store_revocation_is_terminal_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    device = Identity.generate("phone")
    current = descriptor(
        device, capabilities=("human.approval_signer",)
    )
    human = HumanPrincipalIdentity.generate()
    grant = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        issued_ms=NOW,
        ttl_ms=HOUR_MS,
    )
    revocation = issue_human_device_revocation(
        human,
        current,
        sequence=2,
        previous_digest=grant.digest,
        revoked_ms=NOW + 1,
    )
    forbidden = issue_human_device_grant(
        human,
        current,
        capabilities=("human.approval_signer",),
        sequence=3,
        previous_digest=revocation.digest,
        issued_ms=NOW + 2,
        ttl_ms=HOUR_MS,
    )

    with ControlPlaneStore(database) as store:
        store.accept_descriptor(current, now=NOW)
        store.accept_human_grant(grant, current, now=NOW)
        store.accept_human_revocation(
            revocation, current, now=NOW + 1
        )
    with ControlPlaneStore(database) as reopened:
        assert reopened.is_human_device_revoked(
            human.human_id, device.node_id
        )
        assert reopened.human_device_grant(
            human.human_id, device.node_id, now=NOW + 2
        ) is None
        with pytest.raises(ValueError, match="permanently revoked"):
            reopened.accept_human_grant(
                forbidden, current, now=NOW + 2
            )


def test_control_plane_store_rolls_back_invalid_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    device = Identity.generate("node")
    first = descriptor(device)
    gap = descriptor(
        device,
        sequence=3,
        previous_digest=first.digest,
        issued_ms=NOW + 1,
    )
    with ControlPlaneStore(database) as store:
        store.accept_descriptor(first, now=NOW)
        with pytest.raises(ValueError, match="gap"):
            store.accept_descriptor(gap, now=NOW + 1)
        assert store.current_descriptor(device.node_id, now=NOW + 1) == first
