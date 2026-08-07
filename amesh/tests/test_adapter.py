from __future__ import annotations

import pytest

from amesh.adapters.discord import DiscordAdapter
from amesh.policy import PermissionStore, amesh_database_path
from amesh.adapters.discord_backend import (
    DISCORD_SIGNAL_KIND,
    DiscordStore,
    discord_database_path,
    discord_key_path,
)

ACTOR = "a" * 64
EVENT = "b" * 32


def _empty_ledger(home) -> None:
    store = DiscordStore(
        discord_database_path(home),
        discord_key_path(home),
    )
    store.close()


def test_descriptor_unconfigured(tmp_path) -> None:
    adapter = DiscordAdapter(tmp_path)
    try:
        descriptor = adapter.descriptor()
        assert descriptor["name"] == "discord"
        assert descriptor["configured"] is False
        assert descriptor["enabled"] is False
    finally:
        adapter.close()


def test_gated_queue_denies_surface(tmp_path) -> None:
    adapter = DiscordAdapter(tmp_path)
    try:
        store = PermissionStore(amesh_database_path(tmp_path))
        adapter._permissions = store
        store.add_rule("discord", "*", "surface", "deny", reason="quiet channel")
        calls = []

        def fake_queue(destination_id, kind, body):
            calls.append((destination_id, kind, body))
            return "d" * 32

        gate = adapter._gated_queue_signal(fake_queue)
        marker = gate(
            "dest-node",
            DISCORD_SIGNAL_KIND,
            {"actor_key": ACTOR, "source_event_id": EVENT},
        )
        assert calls == []
        assert len(marker) == 32
        assert marker == marker.lower()
        decisions = store.decisions(adapter="discord")
        assert len(decisions) == 1
        assert decisions[0]["action"] == "surface"
        assert decisions[0]["effect"] == "deny"
        assert decisions[0]["event_key"] == EVENT
    finally:
        adapter.close()


def test_gated_queue_passes_when_allowed(tmp_path) -> None:
    adapter = DiscordAdapter(tmp_path)
    try:
        store = PermissionStore(amesh_database_path(tmp_path))
        adapter._permissions = store
        calls = []

        def fake_queue(destination_id, kind, body):
            calls.append((destination_id, kind, body))
            return "e" * 32

        gate = adapter._gated_queue_signal(fake_queue)
        packet_id = gate("dest-node", DISCORD_SIGNAL_KIND, {"actor_key": ACTOR})
        assert calls == [(("dest-node"), DISCORD_SIGNAL_KIND, {"actor_key": ACTOR})]
        assert packet_id == "e" * 32
    finally:
        adapter.close()


def test_exact_allow_overrides_wildcard_deny(tmp_path) -> None:
    adapter = DiscordAdapter(tmp_path)
    try:
        store = PermissionStore(amesh_database_path(tmp_path))
        adapter._permissions = store
        store.add_rule("discord", "*", "surface", "deny")
        store.add_rule("discord", ACTOR, "surface", "allow")
        calls = []

        def fake_queue(destination_id, kind, body):
            calls.append(True)
            return "f" * 32

        gate = adapter._gated_queue_signal(fake_queue)
        gate("dest-node", DISCORD_SIGNAL_KIND, {"actor_key": ACTOR})
        assert calls == [True]
        assert store.decisions(adapter="discord") == []
    finally:
        adapter.close()


def test_reply_denied_by_rule(tmp_path) -> None:
    _empty_ledger(tmp_path)
    adapter = DiscordAdapter(tmp_path)
    try:
        store = PermissionStore(amesh_database_path(tmp_path))
        adapter._permissions = store

        class FakeStore:
            def event(self, event_key):
                return {"actor_key": ACTOR}

            def close(self):
                pass

        class FakeBridge:
            def close(self):
                pass

            def reply(self, event_key, content):
                return {"sent": True, "event_key": event_key}

        adapter._store_handle = FakeStore()
        adapter._bridge = FakeBridge()
        store.add_rule("discord", ACTOR, "reply", "deny", reason="do not reply")
        with pytest.raises(PermissionError):
            adapter.reply(EVENT, "hello")
        assert len(store.decisions(adapter="discord")) == 1
    finally:
        adapter.close()


def test_reply_allowed_passes(tmp_path) -> None:
    _empty_ledger(tmp_path)
    adapter = DiscordAdapter(tmp_path)
    try:
        store = PermissionStore(amesh_database_path(tmp_path))
        adapter._permissions = store

        class FakeStore:
            def event(self, event_key):
                return {"actor_key": ACTOR}

            def close(self):
                pass

        class FakeBridge:
            def close(self):
                pass

            def reply(self, event_key, content):
                return {"sent": True, "event_key": event_key}

        adapter._store_handle = FakeStore()
        adapter._bridge = FakeBridge()
        assert adapter.reply(EVENT, "hello") == {
            "sent": True,
            "event_key": EVENT,
        }
    finally:
        adapter.close()


def test_unsupported_signal_kind_rejected(tmp_path) -> None:
    adapter = DiscordAdapter(tmp_path)
    try:
        gate = adapter._gated_queue_signal(None)
        with pytest.raises(ValueError):
            gate("dest-node", "social.other.signal", {"actor_key": ACTOR})
    finally:
        adapter.close()
