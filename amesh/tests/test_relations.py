from __future__ import annotations

from pathlib import Path

import pytest

from amesh.relations import (
    ActorObservation,
    ActorProof,
    InteractionEvidence,
    RelationshipBook,
    RelationshipHub,
)


def make_observation(
    actor_id: str,
    *,
    kind: str = "account.discord",
    label: str = "alice",
    issuer: str = "operator",
    proof_type: str = "platform-observed",
    scope: str = "platform-observed",
    evidence: str = "ev-1",
    observed_ms: int = 1_750_000_000_000,
) -> ActorObservation:
    return ActorObservation(
        actor_id=actor_id,
        actor_kind=kind,
        actor_label=label,
        proof=ActorProof(
            proof_type=proof_type,
            scope=scope,
            issuer_actor_id=issuer,
            evidence_ref=evidence,
            observed_ms=observed_ms,
        ),
    )


def make_interaction(
    subject_ref: str,
    *,
    evidence_ref: str = "ev-1",
    facets: tuple[str, ...] = ("message", "trust"),
    occurred_ms: int = 1_750_000_000_000,
) -> InteractionEvidence:
    return InteractionEvidence.create(
        actor_id="actor-1",
        subject_ref=subject_ref,
        direction="in",
        facets=facets,
        context="discord",
        outcome="seen",
        evidence_ref=evidence_ref,
        occurred_ms=occurred_ms,
    )


def test_observe_typed_actor_creates_subject(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        subject = book.observe_typed_actor(
            make_observation("alice"), now=1_750_000_000_000
        )
        assert subject.subject_ref.startswith("subj_")
        assert subject.actor_id == "alice"
        estimate = book.relationship(subject.subject_ref)
        assert estimate is not None
        assert estimate.circle == "public"
        assert estimate.state == "active"
        assert estimate.confidence == 50
        assert estimate.labels == ()
    finally:
        book.close()


def test_observe_typed_actor_is_idempotent_for_same_actor(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        first = book.observe_typed_actor(
            make_observation("alice"), now=1
        )
        second = book.observe_typed_actor(
            make_observation("alice"), now=2
        )
        assert first.subject_ref == second.subject_ref
        assert len(book.all()) == 1
    finally:
        book.close()


def test_observe_typed_actor_rejects_invalid_confidence(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        with pytest.raises(ValueError, match="confidence"):
            book.observe_typed_actor(
                make_observation("alice"), subject_confidence=101
            )
    finally:
        book.close()


def test_observe_typed_actor_rejects_empty_actor(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        with pytest.raises(ValueError, match="actor ID"):
            book.observe_typed_actor(make_observation("  "))
    finally:
        book.close()


def test_record_interaction_increments_count_and_dedups(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        subject = book.observe_typed_actor(make_observation("alice"), now=1)
        assert book.record_interaction(
            make_interaction(subject.subject_ref, evidence_ref="ev-1")
        )
        assert book.record_interaction(
            make_interaction(subject.subject_ref, evidence_ref="ev-1")
        ) is False
        estimate = book.relationship(subject.subject_ref)
        assert estimate is not None
        assert estimate.interaction_count == 1
    finally:
        book.close()


def test_set_circle_updates_state_and_labels(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        subject = book.observe_typed_actor(make_observation("alice"), now=1)
        updated = book.set_circle(
            subject.subject_ref,
            "friend",
            confidence=80,
            evidence_ref="ev-2",
            labels=("relationship:vouched",),
            now=1_750_000_000_100,
        )
        assert updated.circle == "friend"
        assert updated.confidence == 80
        assert "relationship:vouched" in updated.labels
    finally:
        book.close()


def test_set_circle_rejects_invalid_circle(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        subject = book.observe_typed_actor(make_observation("alice"), now=1)
        with pytest.raises(ValueError, match="circle"):
            book.set_circle(
                subject.subject_ref, "not valid!", confidence=50, evidence_ref="e"
            )
    finally:
        book.close()


def test_set_circle_rejects_unknown_subject(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        with pytest.raises(ValueError, match="unknown"):
            book.set_circle(
                "subj_missing", "friend", confidence=50, evidence_ref="e"
            )
    finally:
        book.close()


def test_primary_subject_returns_mapping(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        book.observe_typed_actor(make_observation("alice"), now=1)
        subject = book.primary_subject("alice")
        assert subject is not None
        assert subject.actor_id == "alice"
        assert book.primary_subject("bob") is None
    finally:
        book.close()


def test_relationship_hub_end_to_end(tmp_path: Path) -> None:
    hub = RelationshipHub(tmp_path)
    try:
        record = hub.observe_actor(
            "alice", kind="account.discord", label="alice", confidence=60
        )
        assert record["actor_id"] == "alice"
        assert record["circle"] == "public"
        records = hub.list_records()
        assert len(records) == 1
        updated = hub.set_circle(
            record["subject_ref"],
            "known",
            confidence=70,
            evidence_ref="ev-9",
            labels=("interest:agents",),
        )
        assert updated["circle"] == "known"
        assert "interest:agents" in updated["labels"]
    finally:
        hub.book.close()


def test_relationship_hub_uses_amesh_identity(tmp_path: Path) -> None:
    hub_a = RelationshipHub(tmp_path / "a")
    hub_b = RelationshipHub(tmp_path / "b")
    try:
        assert hub_a.identity.identity_id != hub_b.identity.identity_id
    finally:
        hub_a.book.close()
        hub_b.book.close()


def test_interaction_facets_are_sorted(tmp_path: Path) -> None:
    book = RelationshipBook(tmp_path / "rel.db", own_actor_id="own-1")
    try:
        subject = book.observe_typed_actor(make_observation("alice"), now=1)
        evidence = make_interaction(
            subject.subject_ref,
            evidence_ref="ev-7",
            facets=("trust", "message"),
        )
        assert evidence.facets == ("message", "trust")
    finally:
        book.close()
