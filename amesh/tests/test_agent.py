from __future__ import annotations

import pytest

from amesh.agent import AgentStore, agent_database_path


def test_agent_token_is_one_time_and_grant_is_explicit(tmp_path) -> None:
    store = AgentStore(agent_database_path(tmp_path))
    try:
        created = store.register("reviewer", "Review agent", scopes=["reply"])
        assert created["token"]
        assert "token" not in store.list()[0].to_dict()
        record = store.authenticate(created["token"])
        assert record.agent_id == "reviewer"
        assert store.authorize("reviewer", "discord", "reply") is False
        grant = store.grant(
            "reviewer", "discord", "reply", "allow", reason="review queue"
        )
        assert grant["effect"] == "allow"
        assert store.authorize("reviewer", "discord", "reply") is True
        assert store.authorize("reviewer", "discord", "admin") is False
    finally:
        store.close()


def test_revoked_agent_cannot_authenticate_or_authorize(tmp_path) -> None:
    store = AgentStore(agent_database_path(tmp_path))
    try:
        created = store.register("bot", "Bot", scopes=["reply"])
        store.grant("bot", "discord", "reply", "allow")
        assert store.revoke("bot") is True
        with pytest.raises(PermissionError):
            store.authenticate(created["token"])
        assert store.authorize("bot", "discord", "reply") is False
    finally:
        store.close()
