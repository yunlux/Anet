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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..identity import platform_actor_id
from ..time import now_ms
from ..relations import (
    ActorObservation,
    ActorProof,
    InteractionEvidence,
    RelationshipBook,
)
from ..policy import (
    SocialPolicy,
    normalize_social_label,
)

from ..adapter import PlatformAdapter
from ..model import validate_actor_key, validate_event_key
from ..relations import RelationshipHub
from ..signal import build_signal

LOGGER = logging.getLogger("amesh.loopback")
LOOPBACK_SIGNAL_KIND = "social.loopback.signal"
_ACTOR_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_FILE_RE = re.compile(r"^m-[0-9a-f]{32}\.json$")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")


def loopback_config_path(home: Path) -> Path:
    return Path(home) / "loopback-social.json"


def loopback_database_path(home: Path) -> Path:
    return Path(home) / "loopback-social.sqlite3"


def loopback_key_path(home: Path) -> Path:
    return Path(home) / "loopback-social.key"


def loopback_spool_dir(home: Path) -> Path:
    return Path(home) / "loopback-spool"


def loopback_outbox_dir(home: Path) -> Path:
    return Path(home) / "loopback-outbox"


@dataclass(frozen=True)
class LoopbackConfig:
    channels: tuple[str, ...] = ("lobby",)
    poll_interval_seconds: float = 5.0
    destination_id: str = ""
    signal_ttl_seconds: int = 7 * 86_400
    policy: SocialPolicy = SocialPolicy()
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        channels = tuple(
            sorted(
                {
                    str(value).strip().lower()
                    for value in self.channels
                    if str(value).strip().lower()
                }
            )
        )
        for channel in channels:
            if not _CHANNEL_RE.fullmatch(channel):
                raise ValueError(f"invalid loopback channel: {channel}")
        if not 1 <= len(channels) <= 32:
            raise ValueError("loopback requires 1 to 32 channels")
        if not 1.0 <= float(self.poll_interval_seconds) <= 3600.0:
            raise ValueError("loopback polling interval is outside limits")
        destination = str(self.destination_id).strip()
        if destination and not _TARGET_ID_RE.fullmatch(destination):
            raise ValueError("invalid loopback destination ID")
        if not 60 <= int(self.signal_ttl_seconds) <= 7 * 86_400:
            raise ValueError("loopback signal TTL is outside limits")
        if self.version != 1:
            raise ValueError("unsupported loopback config version")
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "destination_id", destination)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "channels": list(self.channels),
            "poll_interval_seconds": self.poll_interval_seconds,
            "destination_id": self.destination_id,
            "signal_ttl_seconds": self.signal_ttl_seconds,
            "policy": self.policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LoopbackConfig:
        if not isinstance(value, Mapping):
            raise ValueError("loopback config must be an object")
        expected = {
            "version",
            "enabled",
            "channels",
            "poll_interval_seconds",
            "destination_id",
            "signal_ttl_seconds",
            "policy",
        }
        if set(value) != expected:
            raise ValueError("loopback config has unexpected fields")
        channels = value["channels"]
        if not isinstance(channels, list) or not channels:
            raise ValueError("loopback channels must be a non-empty list")
        interval = value["poll_interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError("loopback polling interval must be numeric")
        enabled = value["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("loopback enabled must be boolean")
        return cls(
            version=int(value["version"]),
            enabled=enabled,
            channels=tuple(str(item) for item in channels),
            poll_interval_seconds=float(interval),
            destination_id=str(value["destination_id"]),
            signal_ttl_seconds=int(value["signal_ttl_seconds"]),
            policy=SocialPolicy.from_dict(value["policy"]),
        )

    @classmethod
    def load(cls, home: Path) -> LoopbackConfig:
        return cls.from_dict(
            json.loads(loopback_config_path(home).read_text(encoding="utf-8"))
        )

    def save(self, home: Path) -> Path:
        path = loopback_config_path(home)
        _atomic_json(path, self.to_dict())
        return path


class LoopbackLedger:
    """Private SQLite ledger for the loopback adapter.

    Stores immutable events, bounded actor counters, operator labels, and the
    outbound reply reservation. Raw spool messages never survive ingestion.
    """

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
                raise ValueError("loopback pseudonym key is invalid")
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
            CREATE TABLE IF NOT EXISTS loopback_actors (
                actor_key TEXT PRIMARY KEY,
                first_seen_ms INTEGER NOT NULL,
                last_seen_ms INTEGER NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                reaction_count INTEGER NOT NULL DEFAULT 0,
                pinned_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS loopback_labels (
                actor_key TEXT NOT NULL,
                label TEXT NOT NULL,
                source TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                PRIMARY KEY(actor_key, label)
            );
            CREATE TABLE IF NOT EXISTS loopback_events (
                event_key TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                channel TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                content_level TEXT NOT NULL,
                text TEXT NOT NULL,
                labels_json TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                reply_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_loopback_events_actor
                ON loopback_events(actor_key, created_ms);
            CREATE TABLE IF NOT EXISTS loopback_outbound (
                event_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def pseudonym(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._key,
            f"{namespace}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def labels(self, actor_key: str) -> set[str]:
        actor_key = _actor_key(actor_key)
        rows = self._conn.execute(
            """
            SELECT label FROM loopback_labels
            WHERE actor_key = ? ORDER BY label
            """,
            (actor_key,),
        ).fetchall()
        return {str(row["label"]) for row in rows}

    def actor_stats(self, actor_key: str) -> dict[str, Any] | None:
        actor_key = _actor_key(actor_key)
        row = self._conn.execute(
            "SELECT * FROM loopback_actors WHERE actor_key = ?",
            (actor_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "actor_key": actor_key,
            "first_seen_ms": int(row["first_seen_ms"]),
            "last_seen_ms": int(row["last_seen_ms"]),
            "account_age_days": 0,
            "mention_count": int(row["mention_count"]),
            "reply_count": int(row["reply_count"]),
            "reaction_count": int(row["reaction_count"]),
            "pinned_count": int(row["pinned_count"]),
            "labels": sorted(self.labels(actor_key)),
        }

    def update_labels(
        self,
        actor_key: str,
        *,
        add: set[str] | frozenset[str] = frozenset(),
        remove: set[str] | frozenset[str] = frozenset(),
        source: str = "operator",
    ) -> dict[str, Any]:
        actor_key = _actor_key(actor_key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", source):
            raise ValueError("invalid social label source")
        additions = {normalize_social_label(label, manual=True) for label in add}
        removals = {normalize_social_label(label, manual=True) for label in remove}
        if additions & removals:
            raise ValueError("cannot add and remove the same social label")
        if self.actor_stats(actor_key) is None:
            raise ValueError("unknown loopback actor")
        current = now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for label in sorted(removals):
                self._conn.execute(
                    "DELETE FROM loopback_labels WHERE actor_key = ? AND label = ?",
                    (actor_key, label),
                )
            for label in sorted(additions):
                self._conn.execute(
                    """
                    INSERT INTO loopback_labels(
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
        return {"actor_key": actor_key, "labels": sorted(self.labels(actor_key))}

    def ingest(
        self,
        raw: Mapping[str, Any],
        *,
        channel: str,
        policy: SocialPolicy,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("loopback message must be an object")
        author = str(raw.get("author", "")).strip()
        if not author or len(author) > 128:
            raise ValueError("loopback author is invalid")
        text = str(raw.get("text", ""))
        if len(text) > 2000:
            raise ValueError("loopback text is too long")
        bot = bool(raw.get("bot", False))
        actor_key = self.pseudonym("actor", author)
        event_key = self.pseudonym("event", f"{channel}\0{author}\0{text}")[:32]

        mentioned = "@amesh" in text
        replied = text.startswith("re:")
        event_labels = {"platform:loopback"}
        if mentioned:
            event_labels.add("interaction:mention")
        if replied:
            event_labels.add("interaction:reply")
        if bot:
            event_labels.add("actor:bot")
        content_level = "mention" if mentioned else "metadata"

        digest_body = {
            "channel": channel,
            "author": author,
            "text": text,
            "actor_key": actor_key,
            "labels": sorted(event_labels),
        }
        body_hash = hashlib.sha256(
            json.dumps(
                digest_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        current = now_ms()
        existing = self._conn.execute(
            "SELECT body_hash FROM loopback_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["body_hash"]) != body_hash:
                raise ValueError("loopback event key conflicts")
            stored = self.event(event_key)
            if stored is None:  # pragma: no cover - selected above
                raise RuntimeError("loopback event disappeared")
            stored["new"] = False
            return stored

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO loopback_actors(
                    actor_key, first_seen_ms, last_seen_ms,
                    mention_count, reply_count, reaction_count, pinned_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_key) DO UPDATE SET
                    last_seen_ms = MAX(last_seen_ms, excluded.last_seen_ms),
                    mention_count = MIN(10000, mention_count + excluded.mention_count),
                    reply_count = MIN(10000, reply_count + excluded.reply_count)
                """,
                (
                    actor_key,
                    current,
                    current,
                    1 if mentioned else 0,
                    1 if replied else 0,
                    0,
                    0,
                ),
            )
            stats = self.actor_stats(actor_key)
            if stats is None:  # pragma: no cover - inserted above
                raise RuntimeError("loopback actor insert failed")
            manual_labels = set(stats["labels"])
            evaluation = policy.evaluate(stats, manual_labels, event_labels)
            self._conn.execute(
                """
                INSERT INTO loopback_events(
                    event_key, author, channel, actor_key, created_ms,
                    content_level, text, labels_json, body_hash, evaluation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    author,
                    channel,
                    actor_key,
                    current,
                    content_level,
                    text,
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
            "author": author,
            "channel": channel,
            "actor_key": actor_key,
            "created_ms": current,
            "content_level": content_level,
            "text": text,
            "event_labels": sorted(event_labels),
            "actor_labels": sorted(manual_labels),
            "evaluation": evaluation,
            "reply_message_id": "",
            "new": True,
        }

    def event(self, event_key: str) -> dict[str, Any] | None:
        event_key = _event_key(event_key)
        row = self._conn.execute(
            "SELECT * FROM loopback_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "event_key": event_key,
            "author": str(row["author"]),
            "channel": str(row["channel"]),
            "actor_key": str(row["actor_key"]),
            "created_ms": int(row["created_ms"]),
            "content_level": str(row["content_level"]),
            "text": str(row["text"]),
            "event_labels": json.loads(str(row["labels_json"])),
            "evaluation": json.loads(str(row["evaluation_json"])),
            "reply_message_id": str(row["reply_message_id"]),
        }

    def events(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("loopback event limit must be 1 to 10000")
        rows = self._conn.execute(
            """
            SELECT event_key FROM loopback_events
            ORDER BY created_ms, event_key LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        result = []
        for row in rows:
            event = self.event(str(row["event_key"]))
            if event is not None:
                result.append(event)
        return result

    def reserve_reply(self, event_key: str, content: str) -> bool:
        event_key = _event_key(event_key)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            self._conn.execute(
                """
                INSERT INTO loopback_outbound(
                    event_key, content_hash, state, updated_ms
                ) VALUES (?, ?, 'pending', ?)
                """,
                (event_key, content_hash, now_ms()),
            )
            return True
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                """
                SELECT content_hash FROM loopback_outbound
                WHERE event_key = ?
                """,
                (event_key,),
            ).fetchone()
            if row is None or str(row["content_hash"]) != content_hash:
                raise ValueError(
                    "loopback event already has a different outbound reply"
                ) from None
            return False

    def settle_reply(self, event_key: str) -> None:
        event_key = _event_key(event_key)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            changed = self._conn.execute(
                """
                UPDATE loopback_outbound
                SET state = 'sent', updated_ms = ?
                WHERE event_key = ? AND state IN ('pending', 'sent')
                """,
                (now_ms(), event_key),
            ).rowcount
            if changed != 1:
                raise ValueError("loopback outbound reply is not reserved")
            self._conn.execute(
                "UPDATE loopback_events SET reply_message_id = ? WHERE event_key = ?",
                (event_key, event_key),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def status(self) -> dict[str, Any]:
        actor_count = int(
            self._conn.execute("SELECT COUNT(*) AS n FROM loopback_actors").fetchone()[
                "n"
            ]
        )
        event_count = int(
            self._conn.execute("SELECT COUNT(*) AS n FROM loopback_events").fetchone()[
                "n"
            ]
        )
        replied = int(
            self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM loopback_events
                WHERE reply_message_id != ''
                """
            ).fetchone()["n"]
        )
        return {"actors": actor_count, "events": event_count, "replied": replied}


class LoopbackAdapter(PlatformAdapter):
    """Second Amesh adapter: a local, network-free social platform.

    Messages are dropped as JSON files into ``loopback-spool/`` and replies are
    written into ``loopback-outbox/``. It exercises the same ingestion,
    evidence, threshold, permission, and relationship pipeline as a real
    platform and is fully testable offline.
    """

    name = "loopback"

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self._ledger: LoopbackLedger | None = None
        self._hub: RelationshipHub | None = None

    @property
    def configured(self) -> bool:
        return loopback_config_path(self.home).exists()

    def _config(self) -> LoopbackConfig:
        if not self.configured:
            raise ValueError("loopback adapter is not configured in this home")
        return LoopbackConfig.load(self.home)

    def _open_ledger(self) -> LoopbackLedger:
        if self._ledger is None:
            self._ledger = LoopbackLedger(
                loopback_database_path(self.home),
                loopback_key_path(self.home),
            )
        return self._ledger

    def setup(self) -> dict[str, Any]:
        config = LoopbackConfig()
        config.save(self.home)
        return self.descriptor()

    def descriptor(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "name": self.name,
                "configured": False,
                "enabled": False,
                "summary": "no loopback-social.json configured",
            }
        config = self._config()
        return {
            "name": self.name,
            "configured": True,
            "enabled": config.enabled,
            "channels": list(config.channels),
            "poll_interval_seconds": config.poll_interval_seconds,
            "policy": config.policy.to_dict(),
        }

    def status(self) -> dict[str, Any]:
        if not self.configured:
            return {"name": self.name, "configured": False}
        config = self._config()
        counts = {"actors": 0, "events": 0, "replied": 0}
        if loopback_database_path(self.home).exists():
            counts.update(self._open_ledger().status())
        return {
            "name": self.name,
            "configured": True,
            "enabled": config.enabled,
            "runtime_state": "never_run",
            "spool_pending": self._spool_count(),
            **counts,
            "permission_rules": len(self.permission_rules()),
        }

    def inject(
        self,
        author: str,
        text: str,
        *,
        channel: str = "",
        bot: bool = False,
    ) -> dict[str, Any]:
        config = self._config()
        channel = str(channel).strip().lower() or config.channels[0]
        if channel not in config.channels:
            raise ValueError(f"loopback channel is not allowlisted: {channel}")
        if not author or len(str(author)) > 128:
            raise ValueError("loopback author is invalid")
        if not str(text):
            raise ValueError("loopback text is empty")
        spool_dir = loopback_spool_dir(self.home)
        spool_dir.mkdir(parents=True, exist_ok=True)
        message_id = uuid.uuid4().hex
        path = spool_dir / f"m-{message_id}.json"
        _atomic_json(
            path,
            {
                "author": str(author).strip(),
                "channel": channel,
                "text": str(text),
                "bot": bool(bot),
            },
        )
        return {
            "message_id": message_id,
            "channel": channel,
            "spooled": True,
            "path": str(path),
        }

    def poll_once(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None = None,
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        config = self._config()
        if not config.enabled:
            return {"enabled": False, "ingested": 0}
        ledger = self._open_ledger()
        spool_dir = loopback_spool_dir(self.home)
        if not spool_dir.exists():
            return {"enabled": True, "seen": 0, "ingested": 0}
        seen = 0
        ingested = 0
        routed = 0
        decisions: dict[str, int] = {}
        destination = config.destination_id
        for path in sorted(spool_dir.glob("m-*.json")):
            if not _FILE_RE.fullmatch(path.name):
                continue
            seen += 1
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                channel = str(raw.get("channel", "")).strip().lower()
                if channel not in config.channels:
                    raise ValueError(f"loopback channel is not allowlisted: {channel}")
                event = ledger.ingest(raw, channel=channel, policy=config.policy)
                path.unlink()
            except Exception as exc:
                LOGGER.warning(
                    "loopback spool message rejected: %s",
                    type(exc).__name__,
                )
                continue
            if not event.get("new", False):
                continue
            ingested += 1
            if project_event is not None:
                try:
                    project_event(event)
                except Exception:
                    LOGGER.warning(
                        "loopback event persisted but relationship projection failed",
                        exc_info=True,
                    )
            if destination and queue_signal is not None:
                if "surface" not in event["evaluation"]["allowed_actions"]:
                    pass
                elif self.permission_denies(event["actor_key"], "surface"):
                    self.record_permission_decision(
                        event["actor_key"],
                        "surface",
                        "deny",
                        event_key=event["event_key"],
                    )
                else:
                    signal = build_signal(
                        platform="loopback",
                        adapter="loopback-spool-v1",
                        source_event_id=event["event_key"],
                        actor_key=event["actor_key"],
                        created_ms=event["created_ms"],
                        expires_ms=event["created_ms"]
                        + config.signal_ttl_seconds * 1000,
                        content_level=event["content_level"],
                        content=(
                            event["text"] if event["content_level"] == "mention" else ""
                        ),
                        labels=set(event["event_labels"]) | set(event["actor_labels"]),
                        evaluation=event["evaluation"],
                        provenance={
                            "channel": ledger.pseudonym(
                                "channel",
                                event["channel"],
                            ),
                            "revision": event["event_key"][:32],
                        },
                    )
                    queue_signal(destination, LOOPBACK_SIGNAL_KIND, signal)
                    routed += 1
            action = str(event["evaluation"]["action"])
            decisions[action] = decisions.get(action, 0) + 1
        return {
            "enabled": True,
            "seen": seen,
            "ingested": ingested,
            "routed": routed,
            "decisions": decisions,
        }

    async def run(
        self,
        stop: Any,
        queue_signal: Callable[[str, str, dict[str, Any]], str],
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        while not stop.is_set():
            delay = self._config().poll_interval_seconds
            try:
                await asyncio.to_thread(
                    self.poll_once,
                    queue_signal,
                    project_event,
                )
            except Exception as exc:
                LOGGER.warning("loopback poll failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def project_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        hub = self._relationship_hub()
        return _project_event(hub.book, event)

    def _relationship_hub(self) -> RelationshipHub:
        if self._hub is None:
            self._hub = RelationshipHub(self.home)
        return self._hub

    def actor(self, actor_key: str) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        config = self._config()
        ledger = self._open_ledger()
        stats = ledger.actor_stats(actor_key)
        if stats is None:
            raise ValueError("unknown loopback actor")
        evaluation = config.policy.evaluate(stats, set(stats["labels"]))
        gated, reasons = self.permitted_actions(
            actor_key,
            list(evaluation["allowed_actions"]),
        )
        return {
            **stats,
            "evaluation": evaluation,
            "permission": {
                "effective_allowed_actions": gated,
                "reasons": reasons,
                "rules": self.permission_rules(actor_key),
            },
        }

    def set_labels(
        self,
        actor_key: str,
        *,
        add: set[str] | frozenset[str],
        remove: set[str] | frozenset[str],
        source: str = "operator",
    ) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        self._config()
        return self._open_ledger().update_labels(
            actor_key,
            add=add,
            remove=remove,
            source=source,
        )

    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        event_key = validate_event_key(event_key)
        config = self._config()
        if not config.enabled:
            raise PermissionError("loopback adapter is disabled")
        content = str(content)
        if not 1 <= len(content) <= 2000:
            raise ValueError("loopback reply must contain 1 to 2000 characters")
        ledger = self._open_ledger()
        event = ledger.event(event_key)
        if event is None:
            raise ValueError("unknown social event")
        if self.permission_denies(event["actor_key"], "reply"):
            self.record_permission_decision(
                event["actor_key"],
                "reply",
                "deny",
                event_key=event_key,
            )
            raise PermissionError(
                f"reply blocked by a permission rule for {event['actor_key'][:8]}…"
            )
        stats = ledger.actor_stats(event["actor_key"])
        if stats is None:
            raise ValueError("loopback actor ledger is incomplete")
        evaluation = config.policy.evaluate(
            stats,
            set(stats["labels"]),
            set(event["event_labels"]),
        )
        if "reply" not in evaluation["allowed_actions"]:
            raise PermissionError("loopback reply threshold is not satisfied")
        reserved = ledger.reserve_reply(event_key, content)
        if not reserved and event["reply_message_id"]:
            return {
                "sent": False,
                "duplicate": True,
                "event_key": event_key,
            }
        outbox = loopback_outbox_dir(self.home)
        outbox.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            outbox / f"{event_key}.json",
            {
                "event_key": event_key,
                "content": content,
                "sent_ms": now_ms(),
            },
        )
        ledger.settle_reply(event_key)
        return {
            "sent": True,
            "duplicate": False,
            "event_key": event_key,
            "outbox": str(outbox / f"{event_key}.json"),
        }

    def project(self, *, limit: int = 1000) -> dict[str, Any]:
        self._config()
        ledger = self._open_ledger()
        hub = self._relationship_hub()
        events = ledger.events(limit=int(limit))
        projections = [_project_event(hub.book, event) for event in events]
        return {
            "events_examined": len(projections),
            "interactions_recorded": sum(1 for item in projections if item["recorded"]),
            "actors": sorted({item["actor_id"] for item in projections}),
            "note": (
                "Loopback evidence created no agent grant, platform trust, "
                "or authorization"
            ),
        }

    def relation(self, actor_key: str) -> dict[str, Any]:
        actor_key = validate_actor_key(actor_key)
        hub = RelationshipHub(self.home)
        actor_id = platform_actor_id(
            "loopback",
            namespace_actor_id=hub.identity.identity_id,
            platform_actor_key=actor_key,
        )
        subject = hub.book.primary_subject(actor_id)
        if subject is None:
            return {
                "observed": False,
                "actor_id": actor_id,
                "platform": self.name,
            }
        estimate = hub.book.relationship(subject.subject_ref)
        return {
            "observed": True,
            "actor_id": actor_id,
            "platform": self.name,
            "subject_ref": subject.subject_ref,
            "relationship": estimate.to_dict() if estimate is not None else None,
        }

    def _spool_count(self) -> int:
        spool_dir = loopback_spool_dir(self.home)
        if not spool_dir.exists():
            return 0
        return sum(
            1 for path in spool_dir.glob("m-*.json") if _FILE_RE.fullmatch(path.name)
        )

    def close(self) -> None:
        if self._ledger is not None:
            self._ledger.close()
        super().close()


def _project_event(book: RelationshipBook, event: Mapping[str, Any]) -> dict[str, Any]:
    actor_id = platform_actor_id(
        "loopback",
        namespace_actor_id=book.own_actor_id,
        platform_actor_key=str(event["actor_key"]),
    )
    evidence_ref = f"loopback-event:{event['event_key']}"
    subject = book.observe_typed_actor(
        ActorObservation(
            actor_id=actor_id,
            actor_kind="account.loopback",
            actor_label=f"Loopback account · {actor_id[-6:]}",
            proof=ActorProof(
                proof_type="loopback.spool.v1",
                scope="platform-observed",
                issuer_actor_id=book.own_actor_id,
                evidence_ref=evidence_ref,
                observed_ms=int(event["created_ms"]),
            ),
        ),
        subject_confidence=50,
        now=int(event["created_ms"]),
    )
    facets = {"message"}
    if "content:attachment" in event.get("event_labels", ()):
        facets.add("artifact")
    interaction = InteractionEvidence.create(
        actor_id=actor_id,
        subject_ref=subject.subject_ref,
        direction="incoming",
        facets=facets,
        context="social.loopback",
        outcome="received",
        evidence_ref=evidence_ref,
        occurred_ms=int(event["created_ms"]),
    )
    recorded = book.record_interaction(interaction)
    relationship = book.relationship(subject.subject_ref)
    if relationship is None:
        raise RuntimeError("loopback Actor Subject has no relationship estimate")
    if (
        relationship.state == "active"
        and relationship.circle == "public"
        and {"interaction:mention", "interaction:reply"}.intersection(
            event.get("event_labels", ())
        )
    ):
        book.set_circle(
            subject.subject_ref,
            "known",
            confidence=25,
            evidence_ref=evidence_ref,
            labels=("interaction:directed", "platform:loopback"),
            now=int(event["created_ms"]),
        )
    return {
        "actor_id": actor_id,
        "subject_ref": subject.subject_ref,
        "recorded": recorded,
    }


def _actor_key(value: str) -> str:
    text = str(value).strip().lower()
    if not _ACTOR_KEY_RE.fullmatch(text):
        raise ValueError("invalid loopback actor key")
    return text


def _event_key(value: str) -> str:
    text = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        raise ValueError("invalid loopback event key")
    return text


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
