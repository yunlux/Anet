from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
from typing import Any
import urllib.error

import pytest

from anet.config import initialize_node
from anet.discord_social import (
    DiscordRateLimited,
    DiscordRESTClient,
    DiscordSocialBridge,
    DiscordSocialConfig,
    DiscordSocialStore,
    discord_social_database_path,
    discord_social_key_path,
)
from anet.social import DISCORD_SIGNAL_KIND


GUILD_ID = "175928847299117063"
CHANNEL_ID = "175928847299117064"
BOT_ID = "175928847299117065"
ACTOR_ID = "175928847299117066"
MESSAGE_ID = "1174109848058347520"
REPLY_ID = "1174109848058347521"
DESTINATION = "an1aaaaaaaaaaaaaaaaa"


class FakeDiscordClient:
    timeout = 1.0

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.sent: list[dict[str, str]] = []

    def current_user(self) -> dict[str, Any]:
        return {"id": BOT_ID, "bot": True}

    def channel(self, channel_id: str) -> dict[str, Any]:
        return {"id": channel_id, "guild_id": GUILD_ID}

    def channel_messages(
        self,
        channel_id: str,
        *,
        after: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert channel_id == CHANNEL_ID
        return [
            item
            for item in self.messages
            if not after or int(item["id"]) > int(after)
        ][:limit]

    def send_reply(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        nonce: str,
    ) -> dict[str, Any]:
        self.sent.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "content": content,
                "nonce": nonce,
            }
        )
        return {"id": REPLY_ID}


class FakeResponse:
    def __init__(self, value: Any) -> None:
        self._raw = json.dumps(value).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


def _message(
    *,
    message_id: str = MESSAGE_ID,
    content: str = f"<@{BOT_ID}> hello",
    mentioned: bool = True,
    author_id: str = ACTOR_ID,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": CHANNEL_ID,
        "author": {"id": author_id, "bot": False},
        "content": content,
        "edited_timestamp": None,
        "mention_everyone": False,
        "mentions": [{"id": BOT_ID}] if mentioned else [],
        "attachments": [],
        "reactions": [{"count": 2}],
        "pinned": False,
        "referenced_message": None,
    }


@pytest.fixture
def store(tmp_path: Path) -> DiscordSocialStore:
    value = DiscordSocialStore(
        tmp_path / "social.sqlite3",
        tmp_path / "social.key",
    )
    yield value
    value.close()


def test_config_round_trip_and_threshold_validation(tmp_path: Path) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        destination_node_id=DESTINATION,
    )
    config.save(tmp_path)
    assert DiscordSocialConfig.load(tmp_path) == config
    rendered = (tmp_path / "discord-social.json").read_text()
    assert "Bot " not in rendered
    assert "secret-token-value" not in rendered

    with pytest.raises(ValueError, match="monotonic"):
        replace(
            config,
            policy=replace(
                config.policy,
                reply=replace(config.policy.reply, min_score=40),
            ),
        )


def test_rest_client_suppresses_mentions_and_uses_idempotent_nonce() -> None:
    requests = []

    def opener(request, _timeout):
        requests.append(request)
        if request.full_url.endswith("/users/@me"):
            return FakeResponse({"id": BOT_ID, "bot": True})
        return FakeResponse({"id": REPLY_ID})

    client = DiscordRESTClient("secret-token-value", opener=opener)
    assert client.current_user()["id"] == BOT_ID
    result = client.send_reply(
        CHANNEL_ID,
        MESSAGE_ID,
        "@everyone hello",
        nonce="ab" * 12,
    )
    assert result["id"] == REPLY_ID
    assert requests[0].get_header("Authorization") == (
        "Bot secret-token-value"
    )
    payload = json.loads(requests[1].data)
    assert payload["allowed_mentions"] == {
        "parse": [],
        "replied_user": False,
    }
    assert payload["enforce_nonce"] is True
    assert payload["message_reference"]["fail_if_not_exists"] is True


def test_rest_client_obeys_retry_after_without_leaking_token() -> None:
    def opener(request, _timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "rate limited",
            {"Retry-After": "4.5"},
            io.BytesIO(
                b'{"message":"rate limited","retry_after":3.25}'
            ),
        )

    client = DiscordRESTClient("secret-token-value", opener=opener)
    with pytest.raises(DiscordRateLimited) as captured:
        client.current_user()
    assert captured.value.retry_after == 3.25
    assert "secret-token-value" not in str(captured.value)


def test_ingest_uses_pseudonyms_content_levels_and_idempotency(
    store: DiscordSocialStore,
) -> None:
    event = store.ingest_message(
        _message(),
        channel_id=CHANNEL_ID,
        bot_user_id=BOT_ID,
        content_mode="mentions",
        policy=DiscordSocialConfig(
            guild_id=GUILD_ID,
            channel_ids=(CHANNEL_ID,),
        ).policy,
    )
    assert event is not None
    assert event["actor_key"] != ACTOR_ID
    assert len(event["actor_key"]) == 64
    assert event["content_level"] == "mention"
    assert event["content"] == f"<@{BOT_ID}> hello"
    assert event["evaluation"]["action"] == "surface"
    duplicate = store.ingest_message(
        _message(),
        channel_id=CHANNEL_ID,
        bot_user_id=BOT_ID,
        content_mode="mentions",
        policy=DiscordSocialConfig(
            guild_id=GUILD_ID,
            channel_ids=(CHANNEL_ID,),
        ).policy,
    )
    assert duplicate is not None
    assert duplicate["new"] is False
    assert store.actor_stats(event["actor_key"])["mention_count"] == 1

    other = store.ingest_message(
        _message(
            message_id=str(int(MESSAGE_ID) + 10),
            mentioned=False,
            content="private channel chatter",
        ),
        channel_id=CHANNEL_ID,
        bot_user_id=BOT_ID,
        content_mode="mentions",
        policy=DiscordSocialConfig(
            guild_id=GUILD_ID,
            channel_ids=(CHANNEL_ID,),
        ).policy,
    )
    assert other is not None
    assert other["content_level"] == "metadata"
    assert other["content"] == ""
    assert "private channel chatter" not in repr(store.event(other["event_key"]))


def test_poll_routes_only_public_safe_signal_and_advances_cursor(
    store: DiscordSocialStore,
) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        destination_node_id=DESTINATION,
    )
    client = FakeDiscordClient([_message()])
    bridge = DiscordSocialBridge(config, store, client)
    queued: list[tuple[str, str, dict[str, Any]]] = []

    def queue(destination: str, kind: str, body: dict[str, Any]) -> str:
        queued.append((destination, kind, body))
        return "ab" * 16

    result = bridge.poll_once(queue)
    assert result == {
        "enabled": True,
        "seen": 1,
        "ingested": 1,
        "routed": 1,
        "decisions": {"surface": 1},
    }
    assert store.cursor(CHANNEL_ID) == MESSAGE_ID
    destination, kind, signal = queued[0]
    assert destination == DESTINATION
    assert kind == DISCORD_SIGNAL_KIND
    assert signal["content"] == f"<@{BOT_ID}> hello"
    assert signal["actor_key"] != ACTOR_ID
    assert "guild_id" not in repr(signal)
    assert "channel_id" not in repr(signal)

    assert bridge.poll_once(queue)["seen"] == 0
    assert len(queued) == 1


def test_poll_does_not_advance_cursor_when_anet_queue_fails(
    store: DiscordSocialStore,
) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        destination_node_id=DESTINATION,
    )
    bridge = DiscordSocialBridge(
        config,
        store,
        FakeDiscordClient([_message()]),
    )
    attempts = 0

    def flaky_queue(
        _destination: str,
        _kind: str,
        _body: dict[str, Any],
    ) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated Anet queue failure")
        return "cd" * 16

    with pytest.raises(RuntimeError, match="simulated"):
        bridge.poll_once(flaky_queue)
    assert store.cursor(CHANNEL_ID) == ""
    assert store.status()["events"] == 1
    assert store.status()["routed"] == 0

    result = bridge.poll_once(flaky_queue)
    assert result["ingested"] == 0
    assert result["routed"] == 1
    assert attempts == 2
    assert store.cursor(CHANNEL_ID) == MESSAGE_ID


def test_poll_rejects_channel_outside_configured_guild(
    store: DiscordSocialStore,
) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
    )
    client = FakeDiscordClient([_message()])
    client.channel = lambda channel_id: {  # type: ignore[method-assign]
        "id": channel_id,
        "guild_id": str(int(GUILD_ID) + 1),
    }
    bridge = DiscordSocialBridge(config, store, client)
    with pytest.raises(PermissionError, match="outside"):
        bridge.poll_once()
    assert store.status()["events"] == 0


def test_relationship_projection_failure_keeps_durable_event_and_cursor(
    store: DiscordSocialStore,
) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
    )
    bridge = DiscordSocialBridge(
        config,
        store,
        FakeDiscordClient([_message()]),
    )

    def broken_projection(_event: dict[str, Any]) -> None:
        raise RuntimeError("simulated relationship repair window")

    result = bridge.poll_once(project_event=broken_projection)
    assert result["ingested"] == 1
    assert store.status()["events"] == 1
    assert store.cursor(CHANNEL_ID) == MESSAGE_ID


def test_operator_vouch_enables_one_idempotent_reply(
    store: DiscordSocialStore,
) -> None:
    config = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
    )
    client = FakeDiscordClient([_message()])
    bridge = DiscordSocialBridge(config, store, client)
    result = bridge.poll_once()
    assert result["ingested"] == 1
    event = store.event(next(iter(_event_keys(store))))
    assert event is not None

    with pytest.raises(PermissionError, match="threshold"):
        bridge.reply(event["event_key"], "hello back")
    store.update_labels(
        event["actor_key"],
        add={"relationship:vouched", "interest:agents"},
    )
    status = bridge.actor_status(event["actor_key"])
    event_evaluation = config.policy.evaluate(
        status,
        set(status["labels"]),
        set(event["event_labels"]),
    )
    assert "reply" in event_evaluation["allowed_actions"]

    sent = bridge.reply(event["event_key"], "hello back")
    assert sent["sent"] is True
    assert client.sent == [
        {
            "channel_id": CHANNEL_ID,
            "message_id": MESSAGE_ID,
            "content": "hello back",
            "nonce": event["event_key"][:25],
        }
    ]
    duplicate = bridge.reply(event["event_key"], "hello back")
    assert duplicate["duplicate"] is True
    assert len(client.sent) == 1
    with pytest.raises(ValueError, match="different outbound reply"):
        bridge.reply(event["event_key"], "changed reply")


def _event_keys(store: DiscordSocialStore) -> list[str]:
    rows = store._conn.execute(  # noqa: SLF001 - ledger fixture inspection
        "SELECT event_key FROM discord_social_events ORDER BY created_ms"
    ).fetchall()
    return [str(row["event_key"]) for row in rows]


def test_social_paths_stay_inside_existing_node_home(tmp_path: Path) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    social = DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        enabled=False,
    )
    social.save(config.home)
    assert discord_social_database_path(config.home).parent == config.home
    assert discord_social_key_path(config.home).parent == config.home
