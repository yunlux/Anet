from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..policy import SocialPolicy, normalize_social_label
from ..signal import build_signal
from ..time import now_ms

DISCORD_SIGNAL_KIND = "amesh.discord.signal"
DISCORD_API_BASE = "https://discord.com/api/v10"
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_CATEGORIES = frozenset({"rate_limited", "permission", "transport", "api", "validation", "unknown"})


def discord_config_path(home: Path) -> Path:
    return Path(home) / "discord.json"


def discord_database_path(home: Path) -> Path:
    return Path(home) / "discord.sqlite3"


def discord_key_path(home: Path) -> Path:
    return Path(home) / "discord.key"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (json.dumps(dict(value), sort_keys=True, indent=2) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _snowflake(value: Any, label: str) -> str:
    text = str(value).strip()
    if not _SNOWFLAKE_RE.fullmatch(text):
        raise ValueError(f"invalid Discord {label}")
    return text


@dataclass(frozen=True)
class DiscordConfig:
    guild_id: str
    channel_ids: tuple[str, ...]
    destination_id: str = ""
    token_env: str = "AMESH_DISCORD_BOT_TOKEN"
    content_mode: str = "mentions"
    poll_interval_seconds: float = 15.0
    signal_ttl_seconds: int = 7 * 86_400
    policy: SocialPolicy = SocialPolicy()
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _snowflake(self.guild_id, "guild ID"))
        channels = tuple(sorted({_snowflake(item, "channel ID") for item in self.channel_ids}, key=int))
        if not 1 <= len(channels) <= 32:
            raise ValueError("Discord adapter requires 1 to 32 channels")
        object.__setattr__(self, "channel_ids", channels)
        destination = str(self.destination_id).strip()
        if len(destination) > 128 or (destination and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}", destination)):
            raise ValueError("invalid Discord destination ID")
        object.__setattr__(self, "destination_id", destination)
        if not _ENV_RE.fullmatch(str(self.token_env).strip()):
            raise ValueError("invalid Discord token environment name")
        if self.content_mode not in {"metadata", "mentions"}:
            raise ValueError("Discord content mode must be metadata or mentions")
        if not 5.0 <= float(self.poll_interval_seconds) <= 3600.0:
            raise ValueError("Discord polling interval is outside limits")
        if not 60 <= int(self.signal_ttl_seconds) <= 7 * 86_400:
            raise ValueError("Discord signal TTL is outside limits")
        if int(self.version) != 1:
            raise ValueError("unsupported Discord config version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "guild_id": self.guild_id,
            "channel_ids": list(self.channel_ids),
            "destination_id": self.destination_id,
            "token_env": self.token_env,
            "content_mode": self.content_mode,
            "poll_interval_seconds": self.poll_interval_seconds,
            "signal_ttl_seconds": self.signal_ttl_seconds,
            "policy": self.policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DiscordConfig:
        expected = {"version", "enabled", "guild_id", "channel_ids", "destination_id", "token_env", "content_mode", "poll_interval_seconds", "signal_ttl_seconds", "policy"}
        if not isinstance(value, Mapping) or set(value) != expected or not isinstance(value["channel_ids"], list):
            raise ValueError("Discord config has unexpected fields")
        return cls(
            version=int(value["version"]), enabled=bool(value["enabled"]), guild_id=str(value["guild_id"]),
            channel_ids=tuple(str(item) for item in value["channel_ids"]), destination_id=str(value["destination_id"]),
            token_env=str(value["token_env"]), content_mode=str(value["content_mode"]),
            poll_interval_seconds=float(value["poll_interval_seconds"]), signal_ttl_seconds=int(value["signal_ttl_seconds"]),
            policy=SocialPolicy.from_dict(value["policy"]),
        )

    @classmethod
    def load(cls, home: Path) -> DiscordConfig:
        return cls.from_dict(json.loads(discord_config_path(home).read_text(encoding="utf-8")))

    def save(self, home: Path) -> Path:
        path = discord_config_path(home)
        _atomic_json(path, self.to_dict())
        return path


class DiscordRateLimited(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.1, min(float(retry_after), 3600.0))
        super().__init__("Discord API rate limited the adapter")


class DiscordAPIError(RuntimeError):
    pass


class DiscordPermissionError(DiscordAPIError):
    pass


class DiscordTransportError(DiscordAPIError):
    pass


class DiscordRESTClient:
    def __init__(self, token: str, *, opener: Callable[[urllib.request.Request, float], Any] | None = None, timeout: float = 20.0) -> None:
        if not token or len(str(token).strip()) > 512:
            raise ValueError("Discord bot token is missing or invalid")
        self._token = str(token).strip()
        self._opener = opener or self._open
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @staticmethod
    def _open(request: urllib.request.Request, timeout: float) -> Any:
        return urllib.request.urlopen(request, timeout=timeout)

    def current_user(self) -> dict[str, Any]:
        value = self._request("GET", "/users/@me")
        if not isinstance(value, dict):
            raise DiscordAPIError("Discord current-user response is invalid")
        _snowflake(value.get("id"), "bot user ID")
        return value

    def channel_messages(self, channel_id: str, *, after: str = "", limit: int = 100) -> list[dict[str, Any]]:
        channel_id = _snowflake(channel_id, "channel ID")
        limit = max(1, min(int(limit), 100))
        query = {"limit": str(limit)}
        if after:
            query["after"] = _snowflake(after, "message ID")
        value = self._request("GET", f"/channels/{channel_id}/messages", query=query)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise DiscordAPIError("Discord messages response is invalid")
        return list(reversed(value))

    def send_reply(self, channel_id: str, content: str, *, message_id: str) -> dict[str, Any]:
        channel_id = _snowflake(channel_id, "channel ID")
        message_id = _snowflake(message_id, "message ID")
        if not 1 <= len(content) <= 2000:
            raise ValueError("Discord reply must contain 1 to 2000 characters")
        value = self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            body={"content": content, "message_reference": {"message_id": message_id}},
        )
        if not isinstance(value, dict):
            raise DiscordAPIError("Discord reply response is invalid")
        return {"message_id": _snowflake(value.get("id"), "reply ID")}

    def _request(self, method: str, path: str, *, query: Mapping[str, str] | None = None, body: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid Discord API path")
        url = DISCORD_API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=encoded,
            method=method,
            headers={"Authorization": f"Bot {self._token}", "User-Agent": "Amesh/0.1", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, self.timeout) as response:
                payload = response.read(2_000_000)
                return json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    value = json.loads(exc.read().decode("utf-8"))
                    raise DiscordRateLimited(float(value.get("retry_after", 1))) from exc
                except (ValueError, TypeError, json.JSONDecodeError):
                    raise DiscordRateLimited(1) from exc
            if exc.code in {401, 403}:
                raise DiscordPermissionError("Discord rejected the configured bot permission") from exc
            raise DiscordAPIError(f"Discord API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiscordTransportError("Discord transport failed") from exc


class DiscordStore:
    def __init__(self, path: Path, key_path: Path) -> None:
        self.path = Path(path)
        self.key_path = Path(key_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_key()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_actors(
                actor_key TEXT PRIMARY KEY, first_seen_ms INTEGER NOT NULL,
                last_seen_ms INTEGER NOT NULL, mention_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0, reaction_count INTEGER NOT NULL DEFAULT 0,
                pinned_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discord_labels(
                actor_key TEXT NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL,
                created_ms INTEGER NOT NULL, PRIMARY KEY(actor_key, label)
            );
            CREATE TABLE IF NOT EXISTS discord_events(
                event_key TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, author TEXT NOT NULL,
                actor_key TEXT NOT NULL, created_ms INTEGER NOT NULL,
                content_level TEXT NOT NULL, text TEXT NOT NULL,
                labels_json TEXT NOT NULL, body_hash TEXT NOT NULL,
                evaluation_json TEXT NOT NULL, reply_message_id TEXT NOT NULL DEFAULT '',
                routed_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS discord_cursors(channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discord_outbound(event_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, state TEXT NOT NULL, updated_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS discord_runtime(id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, error_category TEXT NOT NULL, failures INTEGER NOT NULL, next_retry_ms INTEGER NOT NULL);
            INSERT OR IGNORE INTO discord_runtime VALUES (1, 'never_run', '', 0, 0);
            """
        )

    def _load_key(self) -> bytes:
        if self.key_path.exists():
            value = self.key_path.read_bytes()
            if len(value) != 32:
                raise ValueError("Discord pseudonym key is invalid")
            return value
        value = secrets.token_bytes(32)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return value

    def close(self) -> None:
        self._conn.close()

    def pseudonym(self, namespace: str, value: str) -> str:
        return hmac.new(self._key, f"{namespace}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()

    def cursor(self, channel_id: str) -> str:
        row = self._conn.execute("SELECT message_id FROM discord_cursors WHERE channel_id=?", (str(channel_id),)).fetchone()
        return str(row["message_id"]) if row else ""

    def update_cursor(self, channel_id: str, message_id: str) -> None:
        self._conn.execute("INSERT INTO discord_cursors VALUES (?, ?) ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id", (str(channel_id), str(message_id)))

    def labels(self, actor_key: str) -> set[str]:
        rows = self._conn.execute("SELECT label FROM discord_labels WHERE actor_key=? ORDER BY label", (actor_key,)).fetchall()
        return {str(row["label"]) for row in rows}

    def update_labels(self, actor_key: str, *, add=(), remove=(), source="operator") -> dict[str, Any]:
        if not _HEX64_RE.fullmatch(str(actor_key)):
            raise ValueError("invalid Discord actor key")
        additions = {normalize_social_label(item, manual=True) for item in add}
        removals = {normalize_social_label(item, manual=True) for item in remove}
        if additions & removals or self.actor_stats(actor_key) is None:
            raise ValueError("invalid or unknown Discord actor label update")
        current = now_ms()
        for label in removals:
            self._conn.execute("DELETE FROM discord_labels WHERE actor_key=? AND label=?", (actor_key, label))
        for label in additions:
            self._conn.execute("INSERT INTO discord_labels VALUES (?, ?, ?, ?) ON CONFLICT(actor_key, label) DO UPDATE SET source=excluded.source, created_ms=excluded.created_ms", (actor_key, label, str(source)[:64], current))
        return {"actor_key": actor_key, "labels": sorted(self.labels(actor_key))}

    def actor_stats(self, actor_key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM discord_actors WHERE actor_key=?", (actor_key,)).fetchone()
        if row is None:
            return None
        return {"actor_key": actor_key, "first_seen_ms": int(row["first_seen_ms"]), "last_seen_ms": int(row["last_seen_ms"]), "account_age_days": 0, "mention_count": int(row["mention_count"]), "reply_count": int(row["reply_count"]), "reaction_count": int(row["reaction_count"]), "pinned_count": int(row["pinned_count"]), "labels": sorted(self.labels(actor_key))}

    def ingest_message(self, message: Mapping[str, Any], *, guild_id: str, channel_id: str, policy: SocialPolicy) -> dict[str, Any]:
        message_id = _snowflake(message.get("id"), "message ID")
        author_value = message.get("author")
        if not isinstance(author_value, Mapping):
            raise ValueError("Discord message author is invalid")
        author_id = _snowflake(author_value.get("id"), "author ID")
        author_name = str(author_value.get("username", "user"))[:128]
        content = str(message.get("content", ""))[:2000]
        mentioned = bool(message.get("mention_everyone")) or "@amesh" in content or "<@" in content
        referenced = isinstance(message.get("referenced_message"), Mapping)
        bot = bool(author_value.get("bot", False))
        actor_key = self.pseudonym("actor", author_id)
        event_key = self.pseudonym("event", f"discord:{message_id}")[:32]
        labels = {"platform:discord"}
        if mentioned:
            labels.add("interaction:mention")
        if referenced:
            labels.add("interaction:reply")
        if bot:
            labels.add("actor:bot")
        content_level = "mention" if mentioned else "metadata"
        body_hash = hashlib.sha256(json.dumps({"message_id": message_id, "channel_id": channel_id, "content": content, "actor_key": actor_key}, sort_keys=True).encode("utf-8")).hexdigest()
        existing = self._conn.execute("SELECT body_hash FROM discord_events WHERE event_key=?", (event_key,)).fetchone()
        if existing is not None:
            if str(existing["body_hash"]) != body_hash:
                raise ValueError("Discord event key conflicts")
            event = self.event(event_key)
            if event is None:
                raise RuntimeError("Discord event disappeared")
            event["new"] = False
            return event
        current = now_ms()
        self._conn.execute("INSERT INTO discord_actors(actor_key, first_seen_ms, last_seen_ms, mention_count) VALUES (?, ?, ?, ?) ON CONFLICT(actor_key) DO UPDATE SET last_seen_ms=MAX(last_seen_ms, excluded.last_seen_ms), mention_count=MIN(10000, mention_count + excluded.mention_count), reply_count=MIN(10000, reply_count + excluded.reply_count)", (actor_key, current, current, int(mentioned)))
        stats = self.actor_stats(actor_key)
        if stats is None:
            raise RuntimeError("Discord actor insert failed")
        evaluation = policy.evaluate(stats, set(stats["labels"]), labels)
        self._conn.execute("INSERT INTO discord_events(event_key, message_id, guild_id, channel_id, author, actor_key, created_ms, content_level, text, labels_json, body_hash, evaluation_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_key, message_id, str(guild_id), str(channel_id), author_name, actor_key, current, content_level, content if content_level == "mention" else "", json.dumps(sorted(labels)), body_hash, json.dumps(evaluation, sort_keys=True)))
        return self.event(event_key) | {"new": True}  # type: ignore[operator]

    def event(self, event_key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM discord_events WHERE event_key=?", (str(event_key),)).fetchone()
        if row is None:
            return None
        return {"event_key": str(row["event_key"]), "message_id": str(row["message_id"]), "guild_id": str(row["guild_id"]), "channel_id": str(row["channel_id"]), "author": str(row["author"]), "actor_key": str(row["actor_key"]), "created_ms": int(row["created_ms"]), "content_level": str(row["content_level"]), "text": str(row["text"]), "event_labels": json.loads(row["labels_json"]), "evaluation": json.loads(row["evaluation_json"]), "reply_message_id": str(row["reply_message_id"]), "new": False}

    def events(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT event_key FROM discord_events ORDER BY created_ms, event_key LIMIT ?", (max(1, min(int(limit), 10_000)),)).fetchall()
        return [self.event(str(row["event_key"])) for row in rows]  # type: ignore[list-item]

    def mark_routed(self, event_key: str, packet_id: str) -> None:
        self._conn.execute("UPDATE discord_events SET routed_id=? WHERE event_key=?", (str(packet_id)[:128], str(event_key)))

    def reserve_reply(self, event_key: str, content: str) -> bool:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            self._conn.execute("INSERT INTO discord_outbound VALUES (?, ?, 'pending', ?)", (event_key, digest, now_ms()))
            return True
        except sqlite3.IntegrityError:
            row = self._conn.execute("SELECT content_hash FROM discord_outbound WHERE event_key=?", (event_key,)).fetchone()
            if row is None or str(row["content_hash"]) != digest:
                raise ValueError("Discord event already has a different reply")
            return False

    def settle_reply(self, event_key: str, reply_message_id: str) -> None:
        self._conn.execute("UPDATE discord_outbound SET state='sent', updated_ms=? WHERE event_key=?", (now_ms(), event_key))
        self._conn.execute("UPDATE discord_events SET reply_message_id=? WHERE event_key=?", (reply_message_id, event_key))

    def status(self) -> dict[str, int]:
        return {"actors": int(self._conn.execute("SELECT COUNT(*) FROM discord_actors").fetchone()[0]), "events": int(self._conn.execute("SELECT COUNT(*) FROM discord_events").fetchone()[0]), "routed": int(self._conn.execute("SELECT COUNT(*) FROM discord_events WHERE routed_id != ''").fetchone()[0]), "replied": int(self._conn.execute("SELECT COUNT(*) FROM discord_events WHERE reply_message_id != ''").fetchone()[0])}

    def runtime_status(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM discord_runtime WHERE id=1").fetchone()
        return {"runtime_state": str(row["state"]), "last_error_category": str(row["error_category"]), "consecutive_failures": int(row["failures"]), "next_retry_ms": int(row["next_retry_ms"])}

    def runtime(self, state: str, *, category: str = "", failures: int = 0, retry_ms: int = 0) -> None:
        if category and category not in _RUNTIME_CATEGORIES:
            category = "unknown"
        self._conn.execute("UPDATE discord_runtime SET state=?, error_category=?, failures=?, next_retry_ms=? WHERE id=1", (state, category, int(failures), int(retry_ms)))


class DiscordBridge:
    def __init__(self, config: DiscordConfig, store: DiscordStore, client: DiscordRESTClient) -> None:
        self.config = config
        self.store = store
        self.client = client

    @classmethod
    def from_home(cls, home: Path) -> DiscordBridge:
        config = DiscordConfig.load(home)
        token = os.environ.get(config.token_env, "")
        return cls(config, DiscordStore(discord_database_path(home), discord_key_path(home)), DiscordRESTClient(token))

    def close(self) -> None:
        self.store.close()

    def poll_once(self, queue_signal=None, project_event=None) -> dict[str, Any]:
        if not self.config.enabled:
            return {"enabled": False, "ingested": 0}
        ingested = routed = seen = 0
        decisions: dict[str, int] = {}
        try:
            for channel_id in self.config.channel_ids:
                messages = self.client.channel_messages(channel_id, after=self.store.cursor(channel_id))
                for message in messages:
                    seen += 1
                    event = self.store.ingest_message(message, guild_id=self.config.guild_id, channel_id=channel_id, policy=self.config.policy)
                    self.store.update_cursor(channel_id, str(message.get("id", "")))
                    if not event["new"]:
                        continue
                    ingested += 1
                    if project_event is not None:
                        project_event(event)
                    if self.config.destination_id and queue_signal and "surface" in event["evaluation"]["allowed_actions"]:
                        signal = build_signal(
                            platform="discord", adapter="discord-rest-v1", source_event_id=event["event_key"], actor_key=event["actor_key"], created_ms=event["created_ms"], expires_ms=event["created_ms"] + self.config.signal_ttl_seconds * 1000, content_level=event["content_level"], content=event["text"], labels=set(event["event_labels"]), evaluation=event["evaluation"], provenance={"guild_key": self.store.pseudonym("guild", self.config.guild_id), "channel_key": self.store.pseudonym("channel", channel_id), "message_revision": event["message_id"]},
                        )
                        packet_id = queue_signal(self.config.destination_id, DISCORD_SIGNAL_KIND, signal)
                        self.store.mark_routed(event["event_key"], packet_id)
                        routed += 1
                    action = str(event["evaluation"]["action"])
                    decisions[action] = decisions.get(action, 0) + 1
            self.store.runtime("healthy")
        except DiscordRateLimited as exc:
            self.store.runtime("degraded", category="rate_limited", failures=1, retry_ms=now_ms() + int(exc.retry_after * 1000))
            raise
        except Exception:
            self.store.runtime("degraded", category="unknown", failures=1)
            raise
        return {"enabled": True, "seen": seen, "ingested": ingested, "routed": routed, "decisions": decisions}

    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        event = self.store.event(event_key)
        if event is None:
            raise ValueError("unknown Discord social event")
        if not 1 <= len(str(content)) <= 2000:
            raise ValueError("Discord reply must contain 1 to 2000 characters")
        reserved = self.store.reserve_reply(event_key, str(content))
        if not reserved and event["reply_message_id"]:
            return {"sent": False, "duplicate": True, "event_key": event_key, "message_id": event["reply_message_id"]}
        result = self.client.send_reply(event["channel_id"], str(content), message_id=event["message_id"])
        self.store.settle_reply(event_key, result["message_id"])
        return {"sent": True, "duplicate": False, "event_key": event_key, "message_id": result["message_id"]}

    async def run(self, stop: Any, queue_signal, project_event=None) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.poll_once, queue_signal, project_event)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
