from __future__ import annotations

import json

import pytest

from amesh.adapters.loopback import (
    LoopbackAdapter,
    LoopbackConfig,
    LoopbackLedger,
    loopback_database_path,
    loopback_key_path,
    loopback_outbox_dir,
)
from anet.config import initialize_node
from anet.social import SocialPolicy, SocialThreshold

LOW_POLICY = SocialPolicy(
    surface=SocialThreshold(0, 0),
    reply=SocialThreshold(0, 0),
    amplify=SocialThreshold(0, 0),
    connect_candidate=SocialThreshold(0, 0, ("relationship:vouched",)),
)


def _configured_adapter(home, *, policy: SocialPolicy = LOW_POLICY) -> LoopbackAdapter:
    LoopbackConfig(channels=("lobby",), policy=policy).save(home)
    return LoopbackAdapter(home)


def _event_key_for(ledger: LoopbackLedger, author: str, text: str) -> str:
    for event in ledger.events():
        if event["author"] == author and event["text"] == text:
            return event["event_key"]
    raise AssertionError("event not found")


def test_config_round_trip(tmp_path) -> None:
    config = LoopbackConfig(channels=("lobby", "general"))
    config.save(tmp_path)
    loaded = LoopbackConfig.load(tmp_path)
    assert loaded.channels == ("general", "lobby")
    assert loaded.enabled is True
    assert loaded.policy.version == 1


def test_config_validation(tmp_path) -> None:
    with pytest.raises(ValueError):
        LoopbackConfig(channels=("Bad Channel",))
    with pytest.raises(ValueError):
        LoopbackConfig(channels=())
    with pytest.raises(ValueError):
        LoopbackConfig(poll_interval_seconds=0.1)


def test_setup_writes_config(tmp_path) -> None:
    adapter = LoopbackAdapter(tmp_path)
    try:
        descriptor = adapter.setup()
        assert descriptor["configured"] is True
        assert descriptor["channels"] == ["lobby"]
    finally:
        adapter.close()


def test_inject_and_poll(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        result = adapter.inject("alice", "@amesh hello")
        assert result["spooled"] is True
        poll = adapter.poll_once()
        assert poll["seen"] == 1
        assert poll["ingested"] == 1
        assert poll["decisions"]
        assert adapter.status()["events"] == 1
        assert adapter._spool_count() == 0
    finally:
        adapter.close()


def test_poll_skips_rejected_spool(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh ok")
        import pathlib

        (
            pathlib.Path(tmp_path)
            / "loopback-spool"
            / "m-1234567890abcdef1234567890abcdef.json"
        ).write_text(
            json.dumps({"author": "bob", "channel": "not-allowed", "text": "hi"}),
            encoding="utf-8",
        )
        poll = adapter.poll_once()
        assert poll["ingested"] == 1
        assert adapter._spool_count() == 1
    finally:
        adapter.close()


def test_actor_and_labels(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh hello")
        adapter.poll_once()
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            actor_key = ledger.pseudonym("actor", "alice")
            stats = ledger.actor_stats(actor_key)
            assert stats is not None
            assert stats["mention_count"] == 1
        finally:
            ledger.close()
        view = adapter.actor(actor_key)
        assert view["evaluation"]["action"] in {"surface", "reply", "amplify"}
        labels = adapter.set_labels(
            actor_key,
            add={"relationship:known"},
            remove=set(),
        )
        assert "relationship:known" in labels["labels"]
    finally:
        adapter.close()


def test_reply_requires_mention_and_permission(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            adapter.inject("alice", "no mention here")
            adapter.poll_once()
            event_key = _event_key_for(ledger, "alice", "no mention here")
            with pytest.raises(PermissionError):
                adapter.reply(event_key, "sure")
        finally:
            ledger.close()
    finally:
        adapter.close()


def test_reply_success_writes_outbox(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            adapter.inject("alice", "@amesh hello")
            adapter.poll_once()
            event_key = _event_key_for(ledger, "alice", "@amesh hello")
            result = adapter.reply(event_key, "hi alice")
            assert result["sent"] is True
            outbox = loopback_outbox_dir(tmp_path) / f"{event_key}.json"
            assert outbox.exists()
            duplicate = adapter.reply(event_key, "hi alice")
            assert duplicate["duplicate"] is True
        finally:
            ledger.close()
    finally:
        adapter.close()


def test_reply_blocked_by_permission_rule(tmp_path) -> None:
    adapter = _configured_adapter(tmp_path)
    try:
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            adapter.inject("alice", "@amesh hello")
            adapter.poll_once()
            event_key = _event_key_for(ledger, "alice", "@amesh hello")
            actor_key = ledger.pseudonym("actor", "alice")
            adapter._permissions.add_rule("loopback", actor_key, "reply", "deny")
            with pytest.raises(PermissionError):
                adapter.reply(event_key, "hi")
        finally:
            ledger.close()
    finally:
        adapter.close()


def test_project_into_relations(tmp_path) -> None:
    node = initialize_node(tmp_path, label="loopback-test")
    adapter = _configured_adapter(node.home)
    try:
        adapter.inject("alice", "@amesh hello")
        adapter.poll_once()
        result = adapter.project()
        assert result["events_examined"] == 1
        assert result["interactions_recorded"] == 1
        assert result["actors"]
    finally:
        adapter.close()


def test_relation_mapping(tmp_path) -> None:
    node = initialize_node(tmp_path, label="loopback-test")
    adapter = _configured_adapter(node.home)
    try:
        ledger = LoopbackLedger(
            loopback_database_path(tmp_path),
            loopback_key_path(tmp_path),
        )
        try:
            adapter.inject("alice", "@amesh hello")
            adapter.poll_once()
            actor_key = ledger.pseudonym("actor", "alice")
        finally:
            ledger.close()
        view = adapter.relation(actor_key)
        assert view["observed"] is False
        assert view["platform"] == "loopback"
        adapter.project()
        view = adapter.relation(actor_key)
        assert view["observed"] is True
        assert view["subject_ref"].startswith("subj_")
    finally:
        adapter.close()
