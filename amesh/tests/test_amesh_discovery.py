from __future__ import annotations

import time

import pytest

from anet.discovery import (
    DiscoveryStore,
    build_discovery_signal,
    discovery_database_path,
    validate_discovery_signal,
)


SENDER = "an1" + "a" * 20


def _signal(*, intent: str = "need", topic: str = "research") -> dict:
    now = int(time.time() * 1000)
    return build_discovery_signal(
        published_ms=now,
        expires_ms=now + 86_400_000,
        intent=intent,
        summary="Looking for an agent that can review a public protocol design.",
        topics=[topic],
        capabilities=["code.review"],
        languages=["en"],
        provenance={
            "source": "operator",
            "adapter": "amesh-cli",
            "revision": "test",
        },
    )


def test_discovery_signal_is_digest_bound_and_public_safe_shape() -> None:
    signal = _signal()
    assert validate_discovery_signal(signal) == signal
    changed = {**signal, "summary": "changed"}
    with pytest.raises(ValueError, match="digest"):
        validate_discovery_signal(changed)
    changed = {**signal, "topics": ["other"]}
    with pytest.raises(ValueError, match="digest"):
        validate_discovery_signal(changed)


def test_local_profile_subscription_feed_cursor_and_feedback(tmp_path) -> None:
    store = DiscoveryStore(discovery_database_path(tmp_path))
    try:
        profile = store.set_profile(
            "default",
            topics=["research"],
            capabilities=["code.review"],
            languages=["en"],
        )
        assert profile["profile_id"] == "default"
        subscription = store.add_subscription(
            "research-needs",
            profile_id="default",
            intents=["need"],
            topics=["research"],
            min_score=40,
        )
        assert subscription["subscription_id"] == "research-needs"

        signal = _signal()
        first = store.ingest(signal, sender_node_id=SENDER)
        assert first == {
            "signal_id": signal["signal_id"],
            "duplicate": False,
            "matches": 1,
        }
        assert store.ingest(signal, sender_node_id=SENDER)["duplicate"] is True

        page = store.feed("research-needs", limit=10)
        assert len(page["items"]) == 1
        item = page["items"][0]
        assert item["score"] >= 40
        assert "topics:research" in item["reasons"]
        assert page["next_cursor"] == item["cursor"]

        feedback = store.add_feedback(
            "research-needs",
            signal["signal_id"],
            "useful",
            note="good fit",
        )
        assert feedback["duplicate"] is False
        assert store.feed("research-needs")["items"][0]["feedback"] == {
            "verdict": "useful",
            "note": "good fit",
        }
        assert store.add_feedback(
            "research-needs",
            signal["signal_id"],
            "useful",
            note="good fit",
        )["duplicate"] is True
        with pytest.raises(ValueError, match="immutable"):
            store.add_feedback(
                "research-needs",
                signal["signal_id"],
                "spam",
            )
    finally:
        store.close()


def test_tenant_signal_is_not_matched_to_another_profile(tmp_path) -> None:
    store = DiscoveryStore(discovery_database_path(tmp_path))
    try:
        store.set_profile("default", tenant="team-a")
        store.add_subscription("all", profile_id="default", min_score=0)
        now = int(time.time() * 1000)
        signal = build_discovery_signal(
            published_ms=now,
            expires_ms=now + 60_000,
            intent="offer",
            summary="A private team offer.",
            visibility="tenant",
            tenant="team-b",
            provenance={"source": "test", "adapter": "fixture"},
        )
        assert store.ingest(signal, sender_node_id=SENDER)["matches"] == 0
        assert store.feed("all")["items"] == []
    finally:
        store.close()
