from __future__ import annotations

import asyncio
import json

import pytest

from anet.cli import main
from anet.config import initialize_node
from anet.discord_social import (
    DiscordSocialConfig,
    DiscordSocialStore,
    discord_social_database_path,
    discord_social_key_path,
)
from anet.identity import Identity
from anet.node import AnetNode
from anet.relations import RelationshipBook


GUILD_ID = "175928847299117063"
CHANNEL_ID = "175928847299117064"
BOT_ID = "175928847299117065"
ACTOR_ID = "175928847299117066"
MESSAGE_ID = "1174109848058347520"


def test_discord_social_cli_config_and_redacted_status(
    tmp_path,
    capsys,
) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    assert main(
        [
            "--home",
            str(config.home),
            "discord-social-status",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"configured": False}

    assert main(
        [
            "--home",
            str(config.home),
            "discord-social-config",
            "--guild",
            GUILD_ID,
            "--channel",
            CHANNEL_ID,
            "--no-enabled",
            "--reply-score",
            "65",
            "--reply-confidence",
            "30",
        ]
    ) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["enabled"] is False
    assert configured["channel_count"] == 1
    assert configured["policy"]["reply"]["min_score"] == 65
    assert "guild_id" not in configured
    assert "channel_ids" not in configured

    assert main(
        [
            "--home",
            str(config.home),
            "discord-social-status",
        ]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configured"] is True
    assert status["enabled"] is False
    assert status["events"] == 0
    assert "guild_id" not in status


def test_discord_social_destination_must_already_be_trusted(
    tmp_path,
    capsys,
) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    result = main(
        [
            "--home",
            str(config.home),
            "discord-social-config",
            "--guild",
            GUILD_ID,
            "--channel",
            CHANNEL_ID,
            "--destination",
            "an1aaaaaaaaaaaaaaaaa",
        ]
    )
    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert "unknown peer" in error["error"]


def test_disabled_bridge_does_not_require_token_or_start_second_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        enabled=False,
    ).save(config.home)
    monkeypatch.delenv("ANET_DISCORD_BOT_TOKEN", raising=False)
    node = AnetNode(config)

    async def exercise() -> None:
        await node.start()
        assert node._discord_bridge is None  # noqa: SLF001
        await node.stop()

    try:
        asyncio.run(exercise())
    finally:
        node.close()


def test_enabled_bridge_fails_closed_without_token(
    tmp_path,
    monkeypatch,
) -> None:
    config = initialize_node(
        tmp_path / "node",
        label="node",
        listen_port=0,
    )
    DiscordSocialConfig(
        guild_id=GUILD_ID,
        channel_ids=(CHANNEL_ID,),
        enabled=True,
    ).save(config.home)
    monkeypatch.delenv("ANET_DISCORD_BOT_TOKEN", raising=False)
    node = AnetNode(config)

    async def exercise() -> None:
        with pytest.raises(ValueError, match="is not set"):
            await node.start()

    try:
        asyncio.run(exercise())
    finally:
        node.close()


def test_discord_social_project_replays_without_creating_trust(
    tmp_path,
    capsys,
) -> None:
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
    store = DiscordSocialStore(
        discord_social_database_path(config.home),
        discord_social_key_path(config.home),
    )
    try:
        store.ingest_message(
            {
                "id": MESSAGE_ID,
                "channel_id": CHANNEL_ID,
                "author": {"id": ACTOR_ID, "bot": False},
                "content": f"<@{BOT_ID}> hello",
                "edited_timestamp": None,
                "mention_everyone": False,
                "mentions": [{"id": BOT_ID}],
                "attachments": [],
                "reactions": [],
                "pinned": False,
                "referenced_message": None,
            },
            channel_id=CHANNEL_ID,
            bot_user_id=BOT_ID,
            content_mode="mentions",
            policy=social.policy,
        )
    finally:
        store.close()

    assert main(
        [
            "--home",
            str(config.home),
            "discord-social-project",
        ]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["events_examined"] == 1
    assert first["interactions_recorded"] == 1
    assert len(first["actors"]) == 1

    assert main(
        [
            "--home",
            str(config.home),
            "discord-social-project",
        ]
    ) == 0
    duplicate = json.loads(capsys.readouterr().out)
    assert duplicate["interactions_recorded"] == 0

    model = RelationshipBook(
        config.relationships_path,
        own_actor_id=Identity.load(config.identity_path).node_id,
    ).snapshot()
    assert model["actors"][0]["actor_kind"] == "account.discord"
    assert model["relationships"][0]["context_trust"] == []


def test_discord_social_key_is_written_as_binary_on_windows(
    tmp_path,
    monkeypatch,
) -> None:
    key = b"\n" + (b"k" * 31)
    monkeypatch.setattr("anet.discord_social.secrets.token_bytes", lambda _: key)
    database_path = tmp_path / "discord-social.sqlite3"
    key_path = tmp_path / "discord-social.key"

    store = DiscordSocialStore(database_path, key_path)
    store.close()

    assert key_path.read_bytes() == key
    reopened = DiscordSocialStore(database_path, key_path)
    reopened.close()
