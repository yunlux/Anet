from __future__ import annotations

import time

import pytest

from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from anet.relationship_disclosure_recovery import (
    RelationshipDisclosureArchiveBook,
    RelationshipDisclosureGapNotice,
    RelationshipDisclosureGapNoticeBook,
)
from anet.relationship_disclosure_schedules import (
    RelationshipDisclosureScheduleBook,
)
from anet.relationship_disclosures import RelationshipDisclosureBook
from anet.reported_relationship_views import ReportedRelationshipViewProjector
from anet.relations import InteractionEvidence, RelationshipBook


NOW = int(time.time() * 1000) - 20_000


def _pair(tmp_path):
    a_config = initialize_node(tmp_path / "a", label="a", listen_port=0)
    b_config = initialize_node(tmp_path / "b", label="b", listen_port=0)
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    PeerBook(
        a_config.peers_path,
        own_node_id=a_identity.node_id,
    ).add(b_identity.card())
    PeerBook(
        b_config.peers_path,
        own_node_id=b_identity.node_id,
    ).add(a_identity.card())
    return a_config, b_config, a_identity, b_identity


def _prepare_two_pages(tmp_path):
    a_config, b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    relations = RelationshipBook(
        a_config.relationships_path,
        own_actor_id=a_identity.node_id,
    )
    subject = relations.observe_actor(
        observed.card(),
        evidence_ref="packet:first",
        now=NOW + 10,
    )
    schedules = RelationshipDisclosureScheduleBook(
        a_config.relationship_disclosure_schedules_path,
        own_actor_id=a_identity.node_id,
    )
    schedule = schedules.create(
        b_identity.node_id,
        cursor="",
        interval_seconds=30,
        baseline="history-start",
        now=NOW + 20,
    )
    sender = AnetNode(a_config)
    first = sender.run_relationship_disclosure_schedules_once(
        schedule_id=schedule.schedule_id,
        force=True,
        now=NOW + 30,
    )[0]
    relations.record_interaction(
        InteractionEvidence.create(
            actor_id=observed.node_id,
            subject_ref=subject.subject_ref,
            direction="outgoing",
            facets=("message",),
            context="social.discord",
            outcome="submitted",
            evidence_ref="discord:second",
            occurred_ms=NOW + 40,
        )
    )
    second = sender.run_relationship_disclosure_schedules_once(
        schedule_id=schedule.schedule_id,
        force=True,
        now=NOW + 50,
    )[0]
    return (
        a_config,
        b_config,
        a_identity,
        b_identity,
        schedule,
        sender,
        first,
        second,
    )


def test_gap_notice_and_active_schedule_recover_exact_archived_page(
    tmp_path,
) -> None:
    (
        a_config,
        b_config,
        a_identity,
        b_identity,
        schedule,
        sender,
        first,
        second,
    ) = _prepare_two_pages(tmp_path)
    receiver = AnetNode(b_config)
    try:
        # Only sequence 1 arrives, making sequence 0 visibly absent.
        second_raw = sender.store.get_packet(second["packet_id"])
        assert second_raw is not None
        receiver.accept_carrier_packet(
            second_raw,
            depth=1,
            peer_id=sender.node_id,
        )
        before = ReportedRelationshipViewProjector.project(
            RelationshipDisclosureBook(
                b_config.relationship_disclosures_path,
                own_actor_id=b_identity.node_id,
            ),
            sender_actor_id=a_identity.node_id,
            series_id=schedule.series_id,
        )
        analysis = before["provenance"]["series"][0]
        assert before["completeness"] == "gap-detected"
        assert analysis["missing_sequences"] == [0]

        queued_notice = receiver.queue_relationship_disclosure_gap_notice(
            a_identity.node_id,
            schedule.series_id,
            now=NOW + 60,
        )
        assert queued_notice["requested_action"] == "none"
        notice_raw = receiver.store.get_packet(queued_notice["packet_id"])
        assert notice_raw is not None
        sender.accept_carrier_packet(
            notice_raw,
            depth=1,
            peer_id=receiver.node_id,
        )
        received_notice = RelationshipDisclosureGapNoticeBook(
            a_config.relationship_disclosure_gap_notices_path,
            own_actor_id=a_identity.node_id,
        ).require(queued_notice["notice_id"])
        assert received_notice.notice.missing_sequences == (0,)

        recovered = sender.retransmit_relationship_disclosure_gap(
            queued_notice["notice_id"],
            now=NOW + 70,
        )
        assert recovered["scope_unchanged"] is True
        assert recovered["series_advanced"] is False
        assert recovered["unavailable_sequences"] == []
        assert recovered["retransmitted"][0]["sequence"] == 0
        archived = RelationshipDisclosureArchiveBook(
            a_config.relationship_disclosure_archive_path,
            own_actor_id=a_identity.node_id,
        ).find(schedule.series_id, 0)
        assert archived is not None
        assert (
            recovered["retransmitted"][0]["disclosure_id"]
            == archived.disclosure.disclosure_id
        )

        recovered_raw = sender.store.get_packet(
            recovered["retransmitted"][0]["packet_id"]
        )
        assert recovered_raw is not None
        receiver.accept_carrier_packet(
            recovered_raw,
            depth=1,
            peer_id=sender.node_id,
        )
        after = ReportedRelationshipViewProjector.project(
            RelationshipDisclosureBook(
                b_config.relationship_disclosures_path,
                own_actor_id=b_identity.node_id,
            ),
            sender_actor_id=a_identity.node_id,
            series_id=schedule.series_id,
        )
        assert after["completeness"] == "proven-continuous-segment"
        assert after["provenance"]["series"][0]["missing_sequences"] == []
    finally:
        sender.close()
        receiver.close()
    assert first["disclosure_id"] == archived.disclosure.disclosure_id


def test_revoked_schedule_refuses_gap_retransmission(tmp_path) -> None:
    (
        a_config,
        b_config,
        a_identity,
        _b_identity,
        schedule,
        sender,
        _first,
        second,
    ) = _prepare_two_pages(tmp_path)
    receiver = AnetNode(b_config)
    try:
        raw = sender.store.get_packet(second["packet_id"])
        assert raw is not None
        receiver.accept_carrier_packet(raw, depth=1, peer_id=sender.node_id)
        queued = receiver.queue_relationship_disclosure_gap_notice(
            a_identity.node_id,
            schedule.series_id,
            now=NOW + 60,
        )
        notice_raw = receiver.store.get_packet(queued["packet_id"])
        assert notice_raw is not None
        sender.accept_carrier_packet(
            notice_raw,
            depth=1,
            peer_id=receiver.node_id,
        )
        RelationshipDisclosureScheduleBook(
            a_config.relationship_disclosure_schedules_path,
            own_actor_id=a_identity.node_id,
        ).revoke(
            schedule.schedule_id,
            reason="observer-stopped",
            now=NOW + 65,
        )
        with pytest.raises(PermissionError, match="active schedule"):
            sender.retransmit_relationship_disclosure_gap(
                queued["notice_id"],
                now=NOW + 70,
            )
    finally:
        sender.close()
        receiver.close()


def test_gap_notice_is_digest_bound_and_cannot_request_action() -> None:
    reporter = Identity.generate("reporter")
    observer = Identity.generate("observer")
    notice = RelationshipDisclosureGapNotice.create(
        reporter_actor_id=reporter.node_id,
        observer_actor_id=observer.node_id,
        series_id="rdsr_" + ("a" * 32),
        missing_sequences=(0, 2),
        detected_through_sequence=2,
        now=NOW,
    )
    value = notice.to_dict()
    assert value["requested_action"] == "none"
    assert value["scope_change"] is False
    tampered = {**value, "missing_sequences": [0, 1, 2]}
    with pytest.raises(ValueError, match="digest"):
        RelationshipDisclosureGapNotice.from_dict(tampered)
    expanded = {**value, "requested_action": "send-history"}
    with pytest.raises(ValueError, match="boundary"):
        RelationshipDisclosureGapNotice.from_dict(expanded)
