from __future__ import annotations

import json

import pytest

from amesh.signal import DirectorySignalSink, build_signal, validate_signal
from amesh.policy import score_social_actor


def _evaluation() -> dict:
    stats = {
        "mention_count": 1,
        "reply_count": 0,
        "reaction_count": 0,
        "pinned_count": 0,
        "account_age_days": 0,
    }
    return {
        "action": "surface",
        "allowed_actions": ["observe", "surface"],
        "reasons": ["bounded mentions +2"],
        "policy_version": 1,
        "reputation": score_social_actor(stats, frozenset()),
    }


def test_build_and_validate_signal() -> None:
    signal = build_signal(
        platform="loopback",
        adapter="loopback-spool-v1",
        source_event_id="a" * 32,
        actor_key="b" * 64,
        created_ms=1_000,
        expires_ms=1_000_000,
        content_level="mention",
        content="hi @amesh",
        labels={"platform:loopback", "interaction:mention"},
        evaluation=_evaluation(),
        provenance={"channel": "c" * 64, "revision": "d" * 32},
    )
    assert signal["protocol"] == "amesh.social.loopback"
    assert len(signal["signal_id"]) == 32
    assert validate_signal(signal) == signal
    assert signal["provenance"]["platform"] == "loopback"
    assert signal["provenance"]["adapter"] == "loopback-spool-v1"


def test_metadata_signal_cannot_carry_content() -> None:
    with pytest.raises(ValueError):
        build_signal(
            platform="loopback",
            adapter="loopback-spool-v1",
            source_event_id="a" * 32,
            actor_key="b" * 64,
            created_ms=1_000,
            expires_ms=1_000_000,
            content_level="metadata",
            content="should not exist",
            labels=set(),
            evaluation=_evaluation(),
            provenance={},
        )


def test_validate_rejects_malformed() -> None:
    signal = build_signal(
        platform="loopback",
        adapter="loopback-spool-v1",
        source_event_id="a" * 32,
        actor_key="b" * 64,
        created_ms=1_000,
        expires_ms=1_000_000,
        content_level="mention",
        content="hi",
        labels=set(),
        evaluation=_evaluation(),
        provenance={},
    )
    with pytest.raises(ValueError):
        validate_signal({**signal, "protocol": "amesh.other"})
    with pytest.raises(ValueError):
        validate_signal({**signal, "actor_key": "not-hex"})
    with pytest.raises(ValueError):
        validate_signal({**signal, "expires_ms": 10})
    with pytest.raises(ValueError):
        validate_signal({**signal, "extra": True})


def test_directory_sink_round_trip(tmp_path) -> None:
    sink = DirectorySignalSink(tmp_path)
    signal = build_signal(
        platform="loopback",
        adapter="loopback-spool-v1",
        source_event_id="a" * 32,
        actor_key="b" * 64,
        created_ms=1_000,
        expires_ms=1_000_000,
        content_level="mention",
        content="hi",
        labels=set(),
        evaluation=_evaluation(),
        provenance={},
    )
    signal_id = sink.emit(signal)
    assert signal_id == signal["signal_id"]
    path = tmp_path / f"loopback-{signal_id}.json"
    assert path.exists()
    listed = sink.list(platform="loopback")
    assert len(listed) == 1
    assert listed[0]["signal_id"] == signal_id
    assert sink.list(platform="discord") == []
    assert sink.count() == 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["protocol"] == "amesh.social.loopback"
