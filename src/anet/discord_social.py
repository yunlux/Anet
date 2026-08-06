from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .encoding import canonical_pack
from .packet import now_ms
from .social import (
    DISCORD_SIGNAL_KIND,
    SocialPolicy,
    build_discord_signal,
    normalize_social_label,
)


LOGGER = logging.getLogger("anet.discord-social")
DISCORD_API_BASE = "https://discord.com/api/v10"
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_NODE_ID_RE = re.compile(r"^an1[a-z2-7]{17,125}$")
_DISCORD_RUNTIME_CATEGORIES = frozenset(
    {
        "rate_limited",
        "permission",
        "transport",
        "api",
        "validation",
        "unknown",
    }
)
_DISCORD_MAX_RETRY_SECONDS = 3600.0


def discord_social_config_path(home: Path) -> Path:
    return Path(home) / "discord-social.json"


def discord_social_database_path(home: Path) -> Path:
    return Path(home) / "discord-social.sqlite3"


def discord_social_key_path(home: Path) -> Path:
    return Path(home) / "discord-social.key"


@dataclass(frozen=True)
class DiscordSocialConfig:
    guild_id: str
    channel_ids: tuple[str, ...]
    destination_node_id: str = ""
    token_env: str = "ANET_DISCORD_BOT_TOKEN"
    content_mode: str = "mentions"
    poll_interval_seconds: float = 15.0
    signal_ttl_seconds: int = 7 * 86_400
    policy: SocialPolicy = SocialPolicy()
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _snowflake(self.guild_id, "Discord guild ID"),
        )
        channels = tuple(
            sorted(
                {
                    _snowflake(value, "Discord channel ID")
                    for value in self.channel_ids
                },
                key=int,
            )
        )
        if not 1 <= len(channels) <= 32:
            raise ValueError("Discord social bridge requires 1 to 32 channels")
        object.__setattr__(self, "channel_ids", channels)
        destination = str(self.destination_node_id).strip().lower()
        if destination and not _NODE_ID_RE.fullmatch(destination):
            raise ValueError("invalid Discord social destination Node ID")
        object.__setattr__(self, "destination_node_id", destination)
        token_env = str(self.token_env).strip()
        if not _ENV_RE.fullmatch(token_env):
            raise ValueError("invalid Discord bot token environment name")
        object.__setattr__(self, "token_env", token_env)
        if self.content_mode not in {"metadata", "mentions"}:
            raise ValueError("Discord content mode must be metadata or mentions")
        if not 5.0 <= float(self.poll_interval_seconds) <= 3600.0:
            raise ValueError("Discord polling interval is outside limits")
        if not 60 <= int(self.signal_ttl_seconds) <= 7 * 86_400:
            raise ValueError("Discord social signal TTL is outside limits")
        if self.version != 1:
            raise ValueError("unsupported Discord social config version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "guild_id": self.guild_id,
            "channel_ids": list(self.channel_ids),
            "destination_node_id": self.destination_node_id,
            "token_env": self.token_env,
            "content_mode": self.content_mode,
            "poll_interval_seconds": self.poll_interval_seconds,
            "signal_ttl_seconds": self.signal_ttl_seconds,
            "policy": self.policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DiscordSocialConfig:
        if not isinstance(value, Mapping):
            raise ValueError("Discord social config must be an object")
        expected = {
            "version",
            "enabled",
            "guild_id",
            "channel_ids",
            "destination_node_id",
            "token_env",
            "content_mode",
            "poll_interval_seconds",
            "signal_ttl_seconds",
            "policy",
        }
        if set(value) != expected:
            raise ValueError("Discord social config has unexpected fields")
        channels = value["channel_ids"]
        if not isinstance(channels, list):
            raise ValueError("Discord channel_ids must be a list")
        enabled = value["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("Discord social enabled must be boolean")
        interval = value["poll_interval_seconds"]
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
        ):
            raise ValueError("Discord polling interval must be numeric")
        ttl = _exact_int(value["signal_ttl_seconds"], "signal_ttl_seconds")
        return cls(
            version=_exact_int(value["version"], "version"),
            enabled=enabled,
            guild_id=str(value["guild_id"]),
            channel_ids=tuple(str(item) for item in channels),
            destination_node_id=str(value["destination_node_id"]),
            token_env=str(value["token_env"]),
            content_mode=str(value["content_mode"]),
            poll_interval_seconds=float(interval),
            signal_ttl_seconds=ttl,
            policy=SocialPolicy.from_dict(value["policy"]),
        )

    @classmethod
    def load(cls, home: Path) -> DiscordSocialConfig:
        path = discord_social_config_path(home)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, home: Path) -> Path:
        path = discord_social_config_path(home)
        _atomic_private_json(path, self.to_dict())
        return path


class DiscordRateLimited(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.1, min(float(retry_after), 3600.0))
        super().__init__("Discord API rate limited the bridge")


class DiscordAPIError(RuntimeError):
    pass


class DiscordPermissionError(DiscordAPIError):
    pass


class DiscordTransportError(DiscordAPIError):
    pass


class DiscordRESTClient:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        timeout: float = 20.0,
    ) -> None:
        token = str(token).strip()
        if not token or len(token) > 512:
            raise ValueError("Discord bot token is missing or invalid")
        self._token = token
        self._opener = opener or self._open
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @staticmethod
    def _open(request: urllib.request.Request, timeout: float) -> Any:
        return urllib.request.urlopen(request, timeout=timeout)

    def current_user(self) -> dict[str, Any]:
        value = self._request("GET", "/users/@me")
        if not isinstance(value, dict):
            raise DiscordAPIError("Discord current-user response is invalid")
        _snowflake(value.get("id"), "Discord bot user ID")
        if not bool(value.get("bot", False)):
            raise DiscordAPIError("Discord token does not belong to a bot")
        return value

    def channel_messages(
        self,
        channel_id: str,
        *,
        after: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        if not 1 <= int(limit) <= 100:
            raise ValueError("Discord message page limit is outside limits")
        query: dict[str, str] = {"limit": str(int(limit))}
        if after:
            query["after"] = _snowflake(after, "Discord message cursor")
        value = self._request(
            "GET",
            f"/channels/{channel_id}/messages?{urllib.parse.urlencode(query)}",
        )
        if not isinstance(value, list) or len(value) > limit:
            raise DiscordAPIError("Discord messages response is invalid")
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise DiscordAPIError("Discord message item is invalid")
            result.append(item)
        return result

    def channel(self, channel_id: str) -> dict[str, Any]:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        value = self._request("GET", f"/channels/{channel_id}")
        if not isinstance(value, dict):
            raise DiscordAPIError("Discord channel response is invalid")
        if _snowflake(
            value.get("id"),
            "Discord channel response ID",
        ) != channel_id:
            raise DiscordAPIError("Discord channel response ID changed")
        _snowflake(value.get("guild_id"), "Discord channel guild ID")
        return value

    def send_reply(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        nonce: str,
    ) -> dict[str, Any]:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        message_id = _snowflake(message_id, "Discord message ID")
        content = str(content)
        if not 1 <= len(content) <= 2000:
            raise ValueError("Discord reply must contain 1 to 2000 characters")
        if not 1 <= len(nonce) <= 25:
            raise ValueError("Discord reply nonce is invalid")
        value = self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            {
                "content": content,
                "nonce": nonce,
                "enforce_nonce": True,
                "allowed_mentions": {
                    "parse": [],
                    "replied_user": False,
                },
                "message_reference": {
                    "type": 0,
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "fail_if_not_exists": True,
                },
            },
        )
        if not isinstance(value, dict):
            raise DiscordAPIError("Discord reply response is invalid")
        _snowflake(value.get("id"), "Discord reply message ID")
        return value

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("Discord API path must be absolute")
        body = None
        headers = {
            "Authorization": f"Bot {self._token}",
            "Accept": "application/json",
            "User-Agent": "Anet-Discord-Social/1",
        }
        if payload is not None:
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            DISCORD_API_BASE + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise DiscordAPIError("Discord API response is too large")
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            detail: Any = {}
            try:
                detail = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "")
                if isinstance(detail, dict):
                    retry_after = detail.get("retry_after", retry_after)
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 1.0
                raise DiscordRateLimited(delay) from None
            if exc.code in {401, 403}:
                raise DiscordPermissionError(
                    f"Discord API permission check failed with HTTP {exc.code}"
                ) from None
            raise DiscordAPIError(
                f"Discord API request failed with HTTP {exc.code}"
            ) from None
        except (OSError, TimeoutError) as exc:
            raise DiscordTransportError(
                f"Discord API transport failed: {type(exc).__name__}"
            ) from None


class DiscordSocialStore:
    def __init__(self, path: Path, key_path: Path) -> None:
        self.path = Path(path)
        self.key_path = Path(key_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._harden_permissions()

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            value = self.key_path.read_bytes()
            if len(value) != 32:
                raise ValueError("Discord social pseudonym key is invalid")
            self._harden_permissions()
            return value
        value = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(self.key_path, flags, 0o600)
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._harden_permissions()
        return value

    def _harden_permissions(self) -> None:
        if os.name == "nt":
            return
        try:
            os.chmod(self.path.parent, 0o700)
        except (FileNotFoundError, PermissionError):
            pass
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            self.key_path,
        ):
            try:
                os.chmod(candidate, 0o600)
            except (FileNotFoundError, PermissionError):
                pass

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_social_actors (
                actor_key TEXT PRIMARY KEY,
                first_seen_ms INTEGER NOT NULL,
                last_seen_ms INTEGER NOT NULL,
                account_created_ms INTEGER NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                reaction_count INTEGER NOT NULL DEFAULT 0,
                pinned_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discord_social_labels (
                actor_key TEXT NOT NULL,
                label TEXT NOT NULL,
                source TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                PRIMARY KEY(actor_key, label)
            );
            CREATE TABLE IF NOT EXISTS discord_social_events (
                event_key TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                revision TEXT NOT NULL,
                content_level TEXT NOT NULL,
                content TEXT NOT NULL,
                labels_json TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                routed_packet_id TEXT NOT NULL DEFAULT '',
                reply_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_social_message_revision
                ON discord_social_events(channel_id, message_id, revision);
            CREATE INDEX IF NOT EXISTS idx_discord_social_actor_events
                ON discord_social_events(actor_key, created_ms);
            CREATE TABLE IF NOT EXISTS discord_social_cursors (
                channel_id TEXT PRIMARY KEY,
                last_message_id TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discord_social_outbound (
                event_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                discord_message_id TEXT NOT NULL DEFAULT '',
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discord_social_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_attempt_ms INTEGER NOT NULL DEFAULT 0,
                last_success_ms INTEGER NOT NULL DEFAULT 0,
                last_error_ms INTEGER NOT NULL DEFAULT 0,
                last_error_category TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_retry_ms INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO discord_social_runtime(id)
            VALUES (1);
            """
        )

    def close(self) -> None:
        self._conn.close()

    def record_poll_attempt(self) -> None:
        self._conn.execute(
            """
            UPDATE discord_social_runtime
            SET last_attempt_ms = ?, next_retry_ms = 0
            WHERE id = 1
            """,
            (now_ms(),),
        )

    def record_poll_success(self) -> None:
        current = now_ms()
        self._conn.execute(
            """
            UPDATE discord_social_runtime
            SET last_attempt_ms = ?, last_success_ms = ?,
                consecutive_failures = 0, next_retry_ms = 0
            WHERE id = 1
            """,
            (current, current),
        )

    def record_poll_failure(
        self,
        category: str,
        *,
        retry_after_seconds: float = 0.0,
        base_interval_seconds: float = 15.0,
    ) -> dict[str, Any]:
        category = _runtime_category(category)
        current = now_ms()
        self._conn.execute(
            """
            UPDATE discord_social_runtime
            SET last_attempt_ms = ?, last_error_ms = ?,
                last_error_category = ?,
                consecutive_failures = consecutive_failures + 1
            WHERE id = 1
            """,
            (current, current, category),
        )
        status = self.runtime_status()
        delay = _discord_retry_delay(
            base_interval_seconds,
            int(status["consecutive_failures"]),
            retry_after_seconds,
        )
        self._conn.execute(
            """
            UPDATE discord_social_runtime SET next_retry_ms = ?
            WHERE id = 1
            """,
            (current + int(delay * 1000),),
        )
        return self.runtime_status()

    def runtime_status(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM discord_social_runtime WHERE id = 1"
        ).fetchone()
        if row is None:  # pragma: no cover - created by _init_schema
            return {
                "runtime_state": "never_run",
                "last_attempt_ms": 0,
                "last_success_ms": 0,
                "last_error_ms": 0,
                "last_error_category": "",
                "consecutive_failures": 0,
                "next_retry_ms": 0,
            }
        failures = int(row["consecutive_failures"])
        attempt = int(row["last_attempt_ms"])
        state = (
            "never_run"
            if attempt == 0
            else "degraded"
            if failures
            else "healthy"
        )
        return {
            "runtime_state": state,
            "last_attempt_ms": attempt,
            "last_success_ms": int(row["last_success_ms"]),
            "last_error_ms": int(row["last_error_ms"]),
            "last_error_category": str(row["last_error_category"]),
            "consecutive_failures": failures,
            "next_retry_ms": int(row["next_retry_ms"]),
        }

    def pseudonym(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._key,
            f"{namespace}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def cursor(self, channel_id: str) -> str:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        row = self._conn.execute(
            """
            SELECT last_message_id FROM discord_social_cursors
            WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchone()
        return str(row["last_message_id"]) if row else ""

    def update_cursor(self, channel_id: str, message_id: str) -> None:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        message_id = _snowflake(message_id, "Discord message ID")
        self._conn.execute(
            """
            INSERT INTO discord_social_cursors(
                channel_id, last_message_id, updated_ms
            ) VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_message_id = CASE
                    WHEN CAST(excluded.last_message_id AS INTEGER) >
                         CAST(last_message_id AS INTEGER)
                    THEN excluded.last_message_id ELSE last_message_id END,
                updated_ms = excluded.updated_ms
            """,
            (channel_id, message_id, now_ms()),
        )

    def labels(self, actor_key: str) -> set[str]:
        actor_key = _actor_key(actor_key)
        rows = self._conn.execute(
            """
            SELECT label FROM discord_social_labels
            WHERE actor_key = ? ORDER BY label
            """,
            (actor_key,),
        ).fetchall()
        return {str(row["label"]) for row in rows}

    def update_labels(
        self,
        actor_key: str,
        *,
        add: set[str] | frozenset[str] = frozenset(),
        remove: set[str] | frozenset[str] = frozenset(),
        source: str = "operator",
    ) -> dict[str, Any]:
        actor_key = _actor_key(actor_key)
        source = str(source).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", source):
            raise ValueError("invalid social label source")
        additions = {
            normalize_social_label(label, manual=True) for label in add
        }
        removals = {
            normalize_social_label(label, manual=True) for label in remove
        }
        if additions & removals:
            raise ValueError("cannot add and remove the same social label")
        if self.actor_stats(actor_key) is None:
            raise ValueError("unknown Discord social actor")
        current = now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for label in sorted(removals):
                self._conn.execute(
                    """
                    DELETE FROM discord_social_labels
                    WHERE actor_key = ? AND label = ?
                    """,
                    (actor_key, label),
                )
            for label in sorted(additions):
                self._conn.execute(
                    """
                    INSERT INTO discord_social_labels(
                        actor_key, label, source, created_ms
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(actor_key, label) DO UPDATE SET
                        source = excluded.source,
                        created_ms = excluded.created_ms
                    """,
                    (actor_key, label, source, current),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return {
            "actor_key": actor_key,
            "labels": sorted(self.labels(actor_key)),
        }

    def actor_stats(self, actor_key: str) -> dict[str, Any] | None:
        actor_key = _actor_key(actor_key)
        row = self._conn.execute(
            """
            SELECT * FROM discord_social_actors WHERE actor_key = ?
            """,
            (actor_key,),
        ).fetchone()
        if row is None:
            return None
        current = now_ms()
        account_created = int(row["account_created_ms"])
        return {
            "actor_key": actor_key,
            "first_seen_ms": int(row["first_seen_ms"]),
            "last_seen_ms": int(row["last_seen_ms"]),
            "account_created_ms": account_created,
            "account_age_days": max(
                0,
                (current - account_created) // 86_400_000,
            ),
            "mention_count": int(row["mention_count"]),
            "reply_count": int(row["reply_count"]),
            "reaction_count": int(row["reaction_count"]),
            "pinned_count": int(row["pinned_count"]),
            "labels": sorted(self.labels(actor_key)),
        }

    def ingest_message(
        self,
        message: Mapping[str, Any],
        *,
        channel_id: str,
        bot_user_id: str,
        content_mode: str,
        policy: SocialPolicy,
    ) -> dict[str, Any] | None:
        channel_id = _snowflake(channel_id, "Discord channel ID")
        bot_user_id = _snowflake(bot_user_id, "Discord bot user ID")
        if not isinstance(message, Mapping):
            raise ValueError("Discord message must be an object")
        message_id = _snowflake(message.get("id"), "Discord message ID")
        if _snowflake(
            message.get("channel_id"),
            "Discord message channel ID",
        ) != channel_id:
            raise ValueError("Discord message channel does not match poll scope")
        author = message.get("author")
        if not isinstance(author, Mapping):
            raise ValueError("Discord message author is missing")
        author_id = _snowflake(author.get("id"), "Discord author ID")
        if author_id == bot_user_id:
            return None
        actor_key = self.pseudonym("actor", author_id)
        created_ms = snowflake_created_ms(message_id)
        revision = str(message.get("edited_timestamp") or "create")
        if not 1 <= len(revision) <= 64:
            raise ValueError("Discord message revision is invalid")
        event_key = self.pseudonym(
            "event",
            f"{channel_id}\0{message_id}\0{revision}",
        )[:32]

        mentions = message.get("mentions", [])
        if not isinstance(mentions, list) or len(mentions) > 100:
            raise ValueError("Discord message mentions are invalid")
        mentioned_bot = any(
            isinstance(item, Mapping)
            and str(item.get("id", "")) == bot_user_id
            for item in mentions
        )
        referenced = message.get("referenced_message")
        reply_to_bot = bool(
            isinstance(referenced, Mapping)
            and isinstance(referenced.get("author"), Mapping)
            and str(referenced["author"].get("id", "")) == bot_user_id
        )
        content_level = (
            "mention"
            if content_mode == "mentions" and mentioned_bot
            else "metadata"
        )
        content = ""
        if content_level == "mention":
            content = str(message.get("content", ""))
            if len(content) > 2000:
                raise ValueError("Discord message content is too long")

        event_labels = {"platform:discord"}
        if mentioned_bot:
            event_labels.add("interaction:mention")
        if reply_to_bot:
            event_labels.add("interaction:reply")
        if bool(author.get("bot", False)):
            event_labels.add("actor:bot")
        if message.get("webhook_id") is not None:
            event_labels.add("actor:webhook")
        if bool(message.get("mention_everyone", False)):
            event_labels.add("event:mass-mention")
        if bool(message.get("pinned", False)):
            event_labels.add("event:pinned")
        attachments = message.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 100:
            raise ValueError("Discord message attachments are invalid")
        if attachments:
            event_labels.add("content:attachment")
        reactions = message.get("reactions", [])
        if not isinstance(reactions, list) or len(reactions) > 100:
            raise ValueError("Discord message reactions are invalid")
        reaction_count = 0
        for reaction in reactions:
            if not isinstance(reaction, Mapping):
                raise ValueError("Discord reaction is invalid")
            count = _exact_int(reaction.get("count", 0), "reaction count")
            if not 0 <= count <= 1_000_000:
                raise ValueError("Discord reaction count is outside limits")
            reaction_count += count
        reaction_count = min(reaction_count, 1_000_000)

        digest_body = {
            "message_id": message_id,
            "channel_id": channel_id,
            "actor_key": actor_key,
            "revision": revision,
            "content_level": content_level,
            "content": content,
            "labels": sorted(event_labels),
            "reaction_count": reaction_count,
        }
        body_hash = hashlib.sha256(canonical_pack(digest_body)).hexdigest()
        existing = self._conn.execute(
            """
            SELECT body_hash FROM discord_social_events
            WHERE event_key = ?
            """,
            (event_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["body_hash"]) != body_hash:
                raise ValueError("Discord social event key conflicts")
            stored = self.event(event_key)
            if stored is None:  # pragma: no cover - selected above
                raise RuntimeError("Discord social event disappeared")
            stored["actor_labels"] = sorted(self.labels(actor_key))
            stored["new"] = False
            return stored

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO discord_social_actors(
                    actor_key, first_seen_ms, last_seen_ms,
                    account_created_ms, mention_count, reply_count,
                    reaction_count, pinned_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_key) DO UPDATE SET
                    last_seen_ms = MAX(last_seen_ms, excluded.last_seen_ms),
                    mention_count = MIN(
                        10000,
                        mention_count + excluded.mention_count
                    ),
                    reply_count = MIN(
                        10000,
                        reply_count + excluded.reply_count
                    ),
                    reaction_count = MIN(
                        1000000,
                        reaction_count + excluded.reaction_count
                    ),
                    pinned_count = MIN(
                        10000,
                        pinned_count + excluded.pinned_count
                    )
                """,
                (
                    actor_key,
                    created_ms,
                    created_ms,
                    snowflake_created_ms(author_id),
                    1 if mentioned_bot else 0,
                    1 if reply_to_bot else 0,
                    reaction_count,
                    1 if bool(message.get("pinned", False)) else 0,
                ),
            )
            stats = self.actor_stats(actor_key)
            if stats is None:  # pragma: no cover - inserted in this transaction
                raise RuntimeError("Discord social actor insert failed")
            manual_labels = set(stats["labels"])
            evaluation = policy.evaluate(
                stats,
                manual_labels,
                event_labels,
            )
            self._conn.execute(
                """
                INSERT INTO discord_social_events(
                    event_key, message_id, channel_id, actor_key,
                    created_ms, revision, content_level, content,
                    labels_json, body_hash, evaluation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    message_id,
                    channel_id,
                    actor_key,
                    created_ms,
                    revision,
                    content_level,
                    content,
                    _compact_json(sorted(event_labels)),
                    body_hash,
                    _compact_json(evaluation),
                ),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return {
            "event_key": event_key,
            "message_id": message_id,
            "channel_id": channel_id,
            "actor_key": actor_key,
            "created_ms": created_ms,
            "revision": revision,
            "content_level": content_level,
            "content": content,
            "event_labels": sorted(event_labels),
            "actor_labels": sorted(manual_labels),
            "evaluation": evaluation,
            "routed_packet_id": "",
            "reply_message_id": "",
            "new": True,
        }

    def event(self, event_key: str) -> dict[str, Any] | None:
        event_key = _event_key(event_key)
        row = self._conn.execute(
            """
            SELECT * FROM discord_social_events WHERE event_key = ?
            """,
            (event_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "event_key": event_key,
            "message_id": str(row["message_id"]),
            "channel_id": str(row["channel_id"]),
            "actor_key": str(row["actor_key"]),
            "created_ms": int(row["created_ms"]),
            "revision": str(row["revision"]),
            "content_level": str(row["content_level"]),
            "content": str(row["content"]),
            "event_labels": json.loads(str(row["labels_json"])),
            "evaluation": json.loads(str(row["evaluation_json"])),
            "routed_packet_id": str(row["routed_packet_id"]),
            "reply_message_id": str(row["reply_message_id"]),
        }

    def events(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        limit = _exact_int(limit, "Discord social event limit")
        if not 1 <= limit <= 10_000:
            raise ValueError("Discord social event limit must be 1 to 10000")
        rows = self._conn.execute(
            """
            SELECT event_key FROM discord_social_events
            ORDER BY created_ms, event_key LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            event = self.event(str(row["event_key"]))
            if event is not None:
                result.append(event)
        return result

    def mark_routed(self, event_key: str, packet_id: str) -> None:
        event_key = _event_key(event_key)
        packet_id = str(packet_id).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", packet_id):
            raise ValueError("invalid routed packet ID")
        changed = self._conn.execute(
            """
            UPDATE discord_social_events SET routed_packet_id = ?
            WHERE event_key = ? AND routed_packet_id = ''
            """,
            (packet_id, event_key),
        ).rowcount
        if changed != 1:
            existing = self.event(event_key)
            if existing is None or existing["routed_packet_id"] != packet_id:
                raise ValueError("Discord social route is already settled")

    def reserve_reply(self, event_key: str, content: str) -> bool:
        event_key = _event_key(event_key)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        current = now_ms()
        try:
            self._conn.execute(
                """
                INSERT INTO discord_social_outbound(
                    event_key, content_hash, state, updated_ms
                ) VALUES (?, ?, 'pending', ?)
                """,
                (event_key, content_hash, current),
            )
            return True
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                """
                SELECT content_hash FROM discord_social_outbound
                WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
            if row is None or str(row["content_hash"]) != content_hash:
                raise ValueError(
                    "Discord event already has a different outbound reply"
                ) from None
            return False

    def settle_reply(self, event_key: str, discord_message_id: str) -> None:
        event_key = _event_key(event_key)
        discord_message_id = _snowflake(
            discord_message_id,
            "Discord reply message ID",
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            changed = self._conn.execute(
                """
                UPDATE discord_social_outbound
                SET state = 'sent', discord_message_id = ?, updated_ms = ?
                WHERE event_key = ? AND state IN ('pending', 'sent')
                  AND (discord_message_id = '' OR discord_message_id = ?)
                """,
                (
                    discord_message_id,
                    now_ms(),
                    event_key,
                    discord_message_id,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Discord outbound reply is not reserved")
            self._conn.execute(
                """
                UPDATE discord_social_events SET reply_message_id = ?
                WHERE event_key = ?
                """,
                (discord_message_id, event_key),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def status(self) -> dict[str, Any]:
        actor_count = int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM discord_social_actors"
            ).fetchone()["n"]
        )
        event_count = int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM discord_social_events"
            ).fetchone()["n"]
        )
        surfaced = int(
            self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM discord_social_events
                WHERE routed_packet_id != ''
                """
            ).fetchone()["n"]
        )
        replied = int(
            self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM discord_social_events
                WHERE reply_message_id != ''
                """
            ).fetchone()["n"]
        )
        return {
            "actors": actor_count,
            "events": event_count,
            "routed": surfaced,
            "replied": replied,
        }


class DiscordSocialBridge:
    def __init__(
        self,
        config: DiscordSocialConfig,
        store: DiscordSocialStore,
        client: DiscordRESTClient,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self._bot_user_id = ""
        self._polling = threading.Event()
        self._validated_channels: set[str] = set()

    @classmethod
    def from_home(cls, home: Path) -> DiscordSocialBridge:
        config = DiscordSocialConfig.load(home)
        token = os.environ.get(config.token_env, "")
        if not token:
            raise ValueError(
                f"Discord bot token environment {config.token_env} is not set"
            )
        return cls(
            config,
            DiscordSocialStore(
                discord_social_database_path(home),
                discord_social_key_path(home),
            ),
            DiscordRESTClient(token),
        )

    def close(self) -> None:
        deadline = time.monotonic() + self.client.timeout + 1.0
        while self._polling.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.store.close()

    def poll_once(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None = None,
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        self._polling.set()
        try:
            self.store.record_poll_attempt()
            result = self._poll_once(queue_signal, project_event)
            self.store.record_poll_success()
            return result
        except Exception as exc:
            self.store.record_poll_failure(
                _runtime_category_for_exception(exc),
                retry_after_seconds=(
                    exc.retry_after
                    if isinstance(exc, DiscordRateLimited)
                    else 0.0
                ),
                base_interval_seconds=self.config.poll_interval_seconds,
            )
            raise
        finally:
            self._polling.clear()

    def _poll_once(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None = None,
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"enabled": False, "seen": 0, "ingested": 0, "routed": 0}
        if not self._bot_user_id:
            self._bot_user_id = _snowflake(
                self.client.current_user()["id"],
                "Discord bot user ID",
            )
        seen = 0
        ingested = 0
        routed = 0
        decisions: dict[str, int] = {}
        for channel_id in self.config.channel_ids:
            if channel_id not in self._validated_channels:
                channel = self.client.channel(channel_id)
                if str(channel["guild_id"]) != self.config.guild_id:
                    raise PermissionError(
                        "Discord channel is outside the configured guild"
                    )
                self._validated_channels.add(channel_id)
            cursor = self.store.cursor(channel_id)
            messages = self.client.channel_messages(
                channel_id,
                after=cursor,
                limit=100,
            )
            ordered = sorted(
                messages,
                key=lambda item: int(
                    _snowflake(item.get("id"), "Discord message ID")
                ),
            )
            for message in ordered:
                seen += 1
                event = self.store.ingest_message(
                    message,
                    channel_id=channel_id,
                    bot_user_id=self._bot_user_id,
                    content_mode=self.config.content_mode,
                    policy=self.config.policy,
                )
                message_id = _snowflake(
                    message.get("id"),
                    "Discord message ID",
                )
                if event is None:
                    self.store.update_cursor(channel_id, message_id)
                    continue
                if event.get("new", False):
                    ingested += 1
                    if project_event is not None:
                        try:
                            project_event(event)
                        except Exception:
                            LOGGER.warning(
                                "Discord event persisted but relationship "
                                "projection failed",
                                exc_info=True,
                            )
                action = str(event["evaluation"]["action"])
                decisions[action] = decisions.get(action, 0) + 1
                if (
                    queue_signal is None
                    or not self.config.destination_node_id
                    or "surface"
                    not in event["evaluation"]["allowed_actions"]
                    or bool(event["routed_packet_id"])
                ):
                    self.store.update_cursor(channel_id, message_id)
                    continue
                signal_created_ms = now_ms()
                signal = build_discord_signal(
                    source_event_id=event["event_key"],
                    actor_key=event["actor_key"],
                    created_ms=signal_created_ms,
                    expires_ms=signal_created_ms
                    + self.config.signal_ttl_seconds * 1000,
                    content_level=event["content_level"],
                    content=event["content"],
                    labels=(
                        set(event["event_labels"])
                        | set(event["actor_labels"])
                    ),
                    evaluation=event["evaluation"],
                    guild_key=self.store.pseudonym(
                        "guild",
                        self.config.guild_id,
                    ),
                    channel_key=self.store.pseudonym(
                        "channel",
                        channel_id,
                    ),
                    message_revision=event["revision"],
                )
                packet_id = queue_signal(
                    self.config.destination_node_id,
                    DISCORD_SIGNAL_KIND,
                    signal,
                )
                self.store.mark_routed(event["event_key"], packet_id)
                routed += 1
                self.store.update_cursor(channel_id, message_id)
        return {
            "enabled": True,
            "seen": seen,
            "ingested": ingested,
            "routed": routed,
            "decisions": decisions,
        }

    def actor_status(self, actor_key: str) -> dict[str, Any]:
        stats = self.store.actor_stats(actor_key)
        if stats is None:
            raise ValueError("unknown Discord social actor")
        evaluation = self.config.policy.evaluate(
            stats,
            set(stats["labels"]),
        )
        return {
            **stats,
            "evaluation": evaluation,
        }

    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        if not self.config.enabled:
            raise PermissionError("Discord social bridge is disabled")
        content = str(content)
        if not 1 <= len(content) <= 2000:
            raise ValueError("Discord reply must contain 1 to 2000 characters")
        event = self.store.event(event_key)
        if event is None:
            raise ValueError("unknown Discord social event")
        stats = self.store.actor_stats(event["actor_key"])
        if stats is None:
            raise ValueError("Discord social actor ledger is incomplete")
        evaluation = self.config.policy.evaluate(
            stats,
            set(stats["labels"]),
            set(event["event_labels"]),
        )
        if "reply" not in evaluation["allowed_actions"]:
            raise PermissionError(
                "Discord social reply threshold is not satisfied"
            )
        reserved = self.store.reserve_reply(event_key, content)
        if not reserved and event["reply_message_id"]:
            return {
                "sent": False,
                "duplicate": True,
                "event_key": event_key,
                "discord_message_id": event["reply_message_id"],
            }
        response = self.client.send_reply(
            event["channel_id"],
            event["message_id"],
            content,
            nonce=event_key[:25],
        )
        reply_message_id = _snowflake(
            response["id"],
            "Discord reply message ID",
        )
        self.store.settle_reply(event_key, reply_message_id)
        return {
            "sent": True,
            "duplicate": False,
            "event_key": event_key,
            "discord_message_id": reply_message_id,
        }

    async def run(
        self,
        stop: asyncio.Event,
        queue_signal: Callable[[str, str, dict[str, Any]], str],
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        while not stop.is_set():
            delay = self.config.poll_interval_seconds
            try:
                result = await asyncio.to_thread(
                    self.poll_once,
                    queue_signal,
                    project_event,
                )
                if result["ingested"] or result["routed"]:
                    LOGGER.info(
                        "Discord social poll ingested=%d routed=%d",
                        result["ingested"],
                        result["routed"],
                    )
            except DiscordRateLimited as exc:
                runtime = self.store.runtime_status()
                delay = _discord_retry_delay(
                    self.config.poll_interval_seconds,
                    int(runtime["consecutive_failures"]),
                    exc.retry_after,
                )
                LOGGER.warning(
                    "Discord social bridge rate limited; retrying later"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime = self.store.runtime_status()
                delay = _discord_retry_delay(
                    self.config.poll_interval_seconds,
                    int(runtime["consecutive_failures"]),
                )
                LOGGER.warning(
                    "Discord social poll failed category=%s retry_seconds=%.1f",
                    _runtime_category_for_exception(exc),
                    delay,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def snowflake_created_ms(value: str) -> int:
    snowflake = int(_snowflake(value, "Discord snowflake"))
    return (snowflake >> 22) + 1_420_070_400_000


def _snowflake(value: Any, label: str) -> str:
    text = str(value).strip()
    if (
        not _SNOWFLAKE_RE.fullmatch(text)
        or int(text) <= 0
        or int(text) >= 2**64
    ):
        raise ValueError(f"{label} is invalid")
    return text


def _actor_key(value: str) -> str:
    text = str(value).strip().lower()
    if not _HEX64_RE.fullmatch(text):
        raise ValueError("invalid Discord social actor key")
    return text


def _event_key(value: str) -> str:
    text = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        raise ValueError("invalid Discord social event key")
    return text


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _runtime_category(value: str) -> str:
    category = str(value).strip().lower()
    if category not in _DISCORD_RUNTIME_CATEGORIES:
        raise ValueError("invalid Discord runtime error category")
    return category


def _runtime_category_for_exception(exc: BaseException) -> str:
    if isinstance(exc, DiscordRateLimited):
        return "rate_limited"
    if isinstance(exc, (DiscordPermissionError, PermissionError)):
        return "permission"
    if isinstance(exc, DiscordTransportError):
        return "transport"
    if isinstance(exc, DiscordAPIError):
        return "api"
    if isinstance(exc, ValueError):
        return "validation"
    return "unknown"


def _discord_retry_delay(
    base_interval_seconds: float,
    consecutive_failures: int,
    retry_after_seconds: float = 0.0,
) -> float:
    base = max(
        0.05,
        min(float(base_interval_seconds), _DISCORD_MAX_RETRY_SECONDS),
    )
    failures = max(1, int(consecutive_failures))
    exponent = min(failures - 1, 8)
    retry_after = max(
        0.0,
        min(float(retry_after_seconds), _DISCORD_MAX_RETRY_SECONDS),
    )
    return min(
        _DISCORD_MAX_RETRY_SECONDS,
        max(base, base * float(2**exponent), retry_after),
    )


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    if os.name != "nt":
        os.chmod(path, 0o600)
