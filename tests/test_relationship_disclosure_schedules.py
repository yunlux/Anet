from __future__ import annotations

import json
import time

import pytest

from anet.cli import main
from anet.config import initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook
from anet.relation_activity import RelationshipActivityFeed
from anet.relationship_disclosure_schedules import (
    RelationshipDisclosureScheduleBook,
)
from anet.relationship_disclosures import (
    RELATIONSHIP_DISCLOSURE_KIND,
    RelationshipDisclosureBook,
)
from anet.relations import InteractionEvidence, RelationshipBook


NOW = int(time.time() * 1000) - 10_000


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


def _observed_relation(config, observer, observed):
    relations = RelationshipBook(
        config.relationships_path,
        own_actor_id=observer.node_id,
    )
    subject = relations.observe_actor(
        observed.card(),
        evidence_ref="packet:actor-proof",
        now=NOW + 10,
    )
    relations.record_interaction(
        InteractionEvidence.create(
            actor_id=observed.node_id,
            subject_ref=subject.subject_ref,
            direction="incoming",
            facets=("message",),
            context="social.discord",
            outcome="received",
            evidence_ref="discord:message-reference",
            occurred_ms=NOW + 20,
        )
    )
    return relations, subject.subject_ref


def test_schedule_defaults_to_now_then_discloses_only_new_activity(
    tmp_path,
) -> None:
    a_config, b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    relations, subject_ref = _observed_relation(
        a_config,
        a_identity,
        observed,
    )
    tail = RelationshipActivityFeed.read(
        relations.snapshot(),
        limit=1,
        tail=True,
    ).next_cursor
    schedules = RelationshipDisclosureScheduleBook(
        a_config.relationship_disclosure_schedules_path,
        own_actor_id=a_identity.node_id,
    )
    schedule = schedules.create(
        b_identity.node_id,
        cursor=tail,
        subject_ref=subject_ref,
        interval_seconds=30,
        now=NOW + 30,
    )
    sender = AnetNode(a_config)
    receiver = AnetNode(b_config)
    try:
        first = sender.run_relationship_disclosure_schedules_once(
            schedule_id=schedule.schedule_id,
            force=True,
            now=NOW + 40,
        )
        assert first[0]["activities"] == 0
        relations.record_interaction(
            InteractionEvidence.create(
                actor_id=observed.node_id,
                subject_ref=subject_ref,
                direction="outgoing",
                facets=("skill",),
                context="social.discord",
                outcome="completed",
                evidence_ref="discord:skill-reference",
                occurred_ms=NOW + 50,
            )
        )
        second = sender.run_relationship_disclosure_schedules_once(
            schedule_id=schedule.schedule_id,
            force=True,
            now=NOW + 60,
        )
        assert second[0]["queued"] is True
        assert second[0]["activities"] == 1
        raw = sender.store.get_packet(second[0]["packet_id"])
        assert raw is not None
        receiver.accept_carrier_packet(
            raw,
            depth=1,
            peer_id=sender.node_id,
        )
    finally:
        sender.close()
        receiver.close()
    received = RelationshipDisclosureBook(
        b_config.relationship_disclosures_path,
        own_actor_id=b_identity.node_id,
    ).all()
    assert len(received) == 1
    assert len(received[0].disclosure.activities) == 1
    assert received[0].disclosure.activities[0].activity_type == (
        "interaction.observed"
    )


def test_failed_queue_retries_same_persisted_disclosure_id(
    tmp_path,
    monkeypatch,
) -> None:
    a_config, _b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    relations, _subject_ref = _observed_relation(
        a_config,
        a_identity,
        observed,
    )
    schedules = RelationshipDisclosureScheduleBook(
        a_config.relationship_disclosure_schedules_path,
        own_actor_id=a_identity.node_id,
    )
    schedule = schedules.create(
        b_identity.node_id,
        cursor="",
        interval_seconds=30,
        now=NOW + 30,
    )
    sender = AnetNode(a_config)
    try:
        def fail_queue(*_args, **_kwargs):
            raise RuntimeError("simulated queue crash")

        monkeypatch.setattr(sender, "queue", fail_queue)
        failed = sender.run_relationship_disclosure_schedules_once(
            schedule_id=schedule.schedule_id,
            force=True,
            now=NOW + 40,
        )
        assert failed[0]["error"] == "RuntimeError"
        persisted = RelationshipDisclosureScheduleBook(
            a_config.relationship_disclosure_schedules_path,
            own_actor_id=a_identity.node_id,
        ).require(schedule.schedule_id)
        assert persisted.pending is not None
        first_id = persisted.pending.disclosure.disclosure_id
        monkeypatch.undo()
        retried = sender.run_relationship_disclosure_schedules_once(
            schedule_id=schedule.schedule_id,
            force=True,
            now=NOW + 50,
        )
        assert retried[0]["disclosure_id"] == first_id
        assert retried[0]["queued"] is True
    finally:
        sender.close()
    assert relations.snapshot()["events"]


def test_revocation_discards_pending_and_prevents_forced_send(
    tmp_path,
) -> None:
    a_config, _b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    relations, _subject_ref = _observed_relation(
        a_config,
        a_identity,
        observed,
    )
    page = RelationshipActivityFeed.read(relations.snapshot())
    schedules = RelationshipDisclosureScheduleBook(
        a_config.relationship_disclosure_schedules_path,
        own_actor_id=a_identity.node_id,
    )
    schedule = schedules.create(
        b_identity.node_id,
        cursor="",
        interval_seconds=30,
        now=NOW + 30,
    )
    from anet.relationship_disclosures import RelationshipDisclosure

    schedules.prepare(
        schedule.schedule_id,
        RelationshipDisclosure.create(
            page,
            audience_actor_id=b_identity.node_id,
            now=NOW + 35,
        ),
        start_cursor="",
        now=NOW + 35,
    )
    revoked = schedules.revoke(
        schedule.schedule_id,
        reason="observer-stopped",
        now=NOW + 40,
    )
    assert revoked.pending is None
    node = AnetNode(a_config)
    try:
        result = node.run_relationship_disclosure_schedules_once(
            schedule_id=schedule.schedule_id,
            force=True,
            now=NOW + 50,
        )
    finally:
        node.close()
    assert result == [
        {
            "schedule_id": schedule.schedule_id,
            "state": "revoked",
            "queued": False,
        }
    ]


def test_receiver_deduplicates_same_disclosure_in_two_packets(tmp_path) -> None:
    a_config, b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    relations, _subject_ref = _observed_relation(
        a_config,
        a_identity,
        observed,
    )
    from anet.relationship_disclosures import RelationshipDisclosure

    disclosure = RelationshipDisclosure.create(
        RelationshipActivityFeed.read(relations.snapshot()),
        audience_actor_id=b_identity.node_id,
        now=NOW + 30,
    )
    sender = AnetNode(a_config)
    receiver = AnetNode(b_config)
    try:
        packet_ids = [
            sender.queue(
                b_identity.node_id,
                kind=RELATIONSHIP_DISCLOSURE_KIND,
                body=disclosure.to_dict(),
            )
            for _ in range(2)
        ]
        assert packet_ids[0] != packet_ids[1]
        for packet_id in packet_ids:
            raw = sender.store.get_packet(packet_id)
            assert raw is not None
            receiver.accept_carrier_packet(
                raw,
                depth=1,
                peer_id=sender.node_id,
            )
    finally:
        sender.close()
        receiver.close()
    assert len(
        RelationshipDisclosureBook(
            b_config.relationship_disclosures_path,
            own_actor_id=b_identity.node_id,
        ).all()
    ) == 1


def test_schedule_cli_lifecycle(tmp_path, capsys) -> None:
    a_config, _b_config, a_identity, b_identity = _pair(tmp_path)
    observed = Identity.generate("observed")
    _observed_relation(a_config, a_identity, observed)
    assert main(
        [
            "--home",
            str(a_config.home),
            "relation-disclosure-schedule-add",
            b_identity.node_id,
            "--all",
            "--include-history",
            "--interval",
            "30",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["history_mode"] == "explicit-replay"
    assert created["audience_pull"] is False
    schedule_id = created["schedule_id"]

    assert main(
        [
            "--home",
            str(a_config.home),
            "relation-disclosure-schedule-run",
            "--schedule",
            schedule_id,
        ]
    ) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["results"][0]["queued"] is True

    assert main(
        [
            "--home",
            str(a_config.home),
            "relation-disclosure-schedule-revoke",
            schedule_id,
            "--confirm",
            schedule_id,
            "--reason",
            "demo-complete",
        ]
    ) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["state"] == "revoked"
    with pytest.raises(SystemExit):
        main(
            [
                "--home",
                str(a_config.home),
                "relation-disclosure-schedule-add",
                b_identity.node_id,
            ]
        )
