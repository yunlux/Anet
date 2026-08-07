"""Agent discovery signals and the observer-local feed store.

The discovery plane is intentionally above Anet's packet/identity narrow
waist.  Packets provide sender authentication, encryption, delivery and
receipts; this module defines only a public-safe signal envelope plus a local
profile/subscription matcher.  A match is a candidate for attention, never a
trust or capability decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .encoding import canonical_pack

DISCOVERY_SIGNAL_KIND = "social.discovery.signal"
DISCOVERY_PROTOCOL = "anet.social.discovery"
DISCOVERY_VERSION = 1
DISCOVERY_INTENTS = ("know", "need", "offer", "capability")
DISCOVERY_VISIBILITIES = ("public", "tenant")
DISCOVERY_FEEDBACK = ("useful", "not_relevant", "spam")

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.:/-]{0,63}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_NODE_ID_RE = re.compile(r"^an1[a-z2-7]{17,125}$")
_DISCOVERY_FIELDS = {
    "protocol",
    "version",
    "signal_id",
    "published_ms",
    "expires_ms",
    "intent",
    "summary",
    "topics",
    "capabilities",
    "languages",
    "visibility",
    "tenant",
    "provenance",
}


def discovery_database_path(home: Path) -> Path:
    return Path(home) / "discovery.sqlite3"


def _string(value: Any, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if len(value) > limit:
        raise ValueError(f"{label} is too long")
    return value


def _bounded_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _token_list(value: Any, label: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = []
    for item in value:
        text = _string(item, label, limit=64).strip().lower()
        if not _TOKEN_RE.fullmatch(text):
            raise ValueError(f"invalid {label} item")
        result.append(text)
    if result != sorted(set(result)):
        raise ValueError(f"{label} must be unique and sorted")
    return result


def _text_list(value: Any, label: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = []
    for item in value:
        text = _string(item, label, limit=64).strip().lower()
        if not text:
            raise ValueError(f"{label} contains empty text")
        result.append(text)
    if result != sorted(set(result)):
        raise ValueError(f"{label} must be unique and sorted")
    return result


def _provenance(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("discovery provenance must be an object")
    if not value or len(value) > 8:
        raise ValueError("discovery provenance must have 1 to 8 fields")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = str(key)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", key_text):
            raise ValueError("invalid discovery provenance field")
        result[key_text] = _string(item, f"provenance {key_text}", limit=128)
    if "source" not in result or "adapter" not in result:
        raise ValueError("discovery provenance requires source and adapter")
    return result


def _signal_digest(value: Mapping[str, Any]) -> str:
    seed = {key: value[key] for key in sorted(_DISCOVERY_FIELDS - {"signal_id"})}
    return hashlib.blake2s(
        canonical_pack(seed), digest_size=16, person=b"anetds1"
    ).hexdigest()


def build_discovery_signal(
    *,
    published_ms: int,
    expires_ms: int,
    intent: str,
    summary: str,
    topics: list[str] | tuple[str, ...] | set[str] = (),
    capabilities: list[str] | tuple[str, ...] | set[str] = (),
    languages: list[str] | tuple[str, ...] | set[str] = (),
    visibility: str = "public",
    tenant: str = "",
    provenance: Mapping[str, str],
) -> dict[str, Any]:
    """Build a deterministic public-safe discovery signal.

    ``summary`` is intentionally bounded but cannot be classified for privacy
    by the protocol. Callers must apply their local privacy policy before
    calling this function.
    """

    value: dict[str, Any] = {
        "protocol": DISCOVERY_PROTOCOL,
        "version": DISCOVERY_VERSION,
        "signal_id": "0" * 32,
        "published_ms": _bounded_int(published_ms, "published_ms"),
        "expires_ms": _bounded_int(expires_ms, "expires_ms"),
        "intent": str(intent).strip().lower(),
        "summary": _string(summary, "summary", limit=1000).strip(),
        "topics": sorted({str(item).strip().lower() for item in topics}),
        "capabilities": sorted(
            {str(item).strip().lower() for item in capabilities}
        ),
        "languages": sorted({str(item).strip().lower() for item in languages}),
        "visibility": str(visibility).strip().lower(),
        "tenant": str(tenant).strip(),
        "provenance": dict(provenance),
    }
    value["signal_id"] = _signal_digest(value)
    return validate_discovery_signal(value)


def validate_discovery_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("discovery signal must be an object")
    if set(value) != _DISCOVERY_FIELDS:
        missing = _DISCOVERY_FIELDS - set(value)
        extra = set(value) - _DISCOVERY_FIELDS
        detail = []
        if missing:
            detail.append(f"missing {','.join(sorted(missing))}")
        if extra:
            detail.append(f"unknown {','.join(sorted(extra))}")
        raise ValueError("invalid discovery signal fields: " + "; ".join(detail))
    if value["protocol"] != DISCOVERY_PROTOCOL:
        raise ValueError("invalid discovery protocol")
    if value["version"] != DISCOVERY_VERSION:
        raise ValueError("unsupported discovery version")
    signal_id = _string(value["signal_id"], "signal_id", limit=32)
    if not _HEX32_RE.fullmatch(signal_id):
        raise ValueError("invalid discovery signal ID")
    published_ms = _bounded_int(value["published_ms"], "published_ms")
    expires_ms = _bounded_int(value["expires_ms"], "expires_ms")
    if not published_ms < expires_ms <= published_ms + 7 * 86_400_000:
        raise ValueError("invalid discovery signal lifetime")
    intent = _string(value["intent"], "intent").strip().lower()
    if intent not in DISCOVERY_INTENTS:
        raise ValueError("unsupported discovery intent")
    summary = _string(value["summary"], "summary", limit=1000)
    if not summary:
        raise ValueError("discovery summary cannot be empty")
    topics = _token_list(value["topics"], "topics", maximum=32)
    capabilities = _token_list(
        value["capabilities"], "capabilities", maximum=32
    )
    languages = _text_list(value["languages"], "languages", maximum=8)
    visibility = _string(value["visibility"], "visibility").strip().lower()
    if visibility not in DISCOVERY_VISIBILITIES:
        raise ValueError("unsupported discovery visibility")
    tenant = _string(value["tenant"], "tenant", limit=64).strip()
    if visibility == "tenant" and not tenant:
        raise ValueError("tenant visibility requires a tenant")
    if visibility == "public" and tenant:
        raise ValueError("public discovery signal cannot carry a tenant")
    provenance = _provenance(value["provenance"])
    normalized = {
        "protocol": DISCOVERY_PROTOCOL,
        "version": DISCOVERY_VERSION,
        "signal_id": signal_id,
        "published_ms": published_ms,
        "expires_ms": expires_ms,
        "intent": intent,
        "summary": summary,
        "topics": topics,
        "capabilities": capabilities,
        "languages": languages,
        "visibility": visibility,
        "tenant": tenant,
        "provenance": provenance,
    }
    if _signal_digest(normalized) != signal_id:
        raise ValueError("discovery signal digest does not match its body")
    return normalized


def _local_id(value: Any, label: str) -> str:
    text = _string(value, label, limit=64).strip().lower()
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"invalid {label}")
    return text


def _node_id(value: Any) -> str:
    text = _string(value, "sender Node ID", limit=128).strip().lower()
    if not _NODE_ID_RE.fullmatch(text):
        raise ValueError("invalid sender Node ID")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_ms() -> int:
    return int(time.time() * 1000)


class DiscoveryStore:
    """SQLite-backed local profiles, subscriptions, feed and feedback.

    The store is observer-local. It does not infer identity from a label or
    address, and it never mutates PeerBook trust or execution capability.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovery_profiles (
                profile_id TEXT PRIMARY KEY,
                topics_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                languages_json TEXT NOT NULL,
                tenant TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_subscriptions (
                subscription_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                intents_json TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                languages_json TEXT NOT NULL,
                min_score INTEGER NOT NULL,
                max_age_ms INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                created_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_signals (
                signal_id TEXT PRIMARY KEY,
                sender_node_id TEXT NOT NULL,
                body_json TEXT NOT NULL,
                received_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                score INTEGER NOT NULL,
                reasons_json TEXT NOT NULL,
                matched_ms INTEGER NOT NULL,
                UNIQUE(subscription_id, signal_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_feedback (
                subscription_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                note TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                PRIMARY KEY(subscription_id, signal_id)
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def set_profile(
        self,
        profile_id: str,
        *,
        topics: list[str] | tuple[str, ...] | set[str] = (),
        capabilities: list[str] | tuple[str, ...] | set[str] = (),
        languages: list[str] | tuple[str, ...] | set[str] = (),
        tenant: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        profile_id = _local_id(profile_id, "profile ID")
        topics = sorted({str(item).strip().lower() for item in topics})
        capabilities = sorted({str(item).strip().lower() for item in capabilities})
        languages = sorted({str(item).strip().lower() for item in languages})
        _token_list(topics, "profile topics", maximum=64)
        _token_list(capabilities, "profile capabilities", maximum=64)
        _text_list(languages, "profile languages", maximum=8)
        tenant = _string(tenant, "profile tenant", limit=64).strip()
        current = _now_ms()
        self._conn.execute(
            """
            INSERT INTO discovery_profiles(
                profile_id, topics_json, capabilities_json, languages_json,
                tenant, enabled, updated_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                topics_json = excluded.topics_json,
                capabilities_json = excluded.capabilities_json,
                languages_json = excluded.languages_json,
                tenant = excluded.tenant,
                enabled = excluded.enabled,
                updated_ms = excluded.updated_ms
            """,
            (
                profile_id,
                _json(topics),
                _json(capabilities),
                _json(languages),
                tenant,
                int(enabled),
                current,
            ),
        )
        return self.profile(profile_id)  # type: ignore[return-value]

    def profile(self, profile_id: str = "") -> dict[str, Any] | None:
        if profile_id:
            profile_id = _local_id(profile_id, "profile ID")
        row = self._conn.execute(
            "SELECT * FROM discovery_profiles"
            + (" WHERE profile_id = ?" if profile_id else " ORDER BY profile_id LIMIT 1"),
            (profile_id,) if profile_id else (),
        ).fetchone()
        if row is None:
            return None
        return self._profile_row(row)

    @staticmethod
    def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "profile_id": str(row["profile_id"]),
            "topics": json.loads(row["topics_json"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "languages": json.loads(row["languages_json"]),
            "tenant": str(row["tenant"]),
            "enabled": bool(row["enabled"]),
            "updated_ms": int(row["updated_ms"]),
        }

    def add_subscription(
        self,
        subscription_id: str,
        *,
        profile_id: str,
        intents: list[str] | tuple[str, ...] | set[str] = (),
        topics: list[str] | tuple[str, ...] | set[str] = (),
        capabilities: list[str] | tuple[str, ...] | set[str] = (),
        languages: list[str] | tuple[str, ...] | set[str] = (),
        min_score: int = 1,
        max_age_seconds: int = 7 * 86_400,
        enabled: bool = True,
    ) -> dict[str, Any]:
        subscription_id = _local_id(subscription_id, "subscription ID")
        profile_id = _local_id(profile_id, "profile ID")
        if self.profile(profile_id) is None:
            raise ValueError("subscription profile does not exist")
        intents = sorted({str(item).strip().lower() for item in intents})
        topics = sorted({str(item).strip().lower() for item in topics})
        capabilities = sorted({str(item).strip().lower() for item in capabilities})
        languages = sorted({str(item).strip().lower() for item in languages})
        if any(item not in DISCOVERY_INTENTS for item in intents):
            raise ValueError("subscription has unsupported intent")
        _token_list(topics, "subscription topics", maximum=64)
        _token_list(capabilities, "subscription capabilities", maximum=64)
        _text_list(languages, "subscription languages", maximum=8)
        if isinstance(min_score, bool) or not 0 <= int(min_score) <= 100:
            raise ValueError("subscription min_score must be 0 to 100")
        if isinstance(max_age_seconds, bool) or not 60 <= int(max_age_seconds) <= 7 * 86_400:
            raise ValueError("subscription max age is outside limits")
        current = _now_ms()
        self._conn.execute(
            """
            INSERT INTO discovery_subscriptions(
                subscription_id, profile_id, intents_json, topics_json,
                capabilities_json, languages_json, min_score, max_age_ms,
                enabled, created_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                profile_id = excluded.profile_id,
                intents_json = excluded.intents_json,
                topics_json = excluded.topics_json,
                capabilities_json = excluded.capabilities_json,
                languages_json = excluded.languages_json,
                min_score = excluded.min_score,
                max_age_ms = excluded.max_age_ms,
                enabled = excluded.enabled
            """,
            (
                subscription_id,
                profile_id,
                _json(intents),
                _json(topics),
                _json(capabilities),
                _json(languages),
                int(min_score),
                int(max_age_seconds) * 1000,
                int(enabled),
                current,
            ),
        )
        return self.subscription(subscription_id)  # type: ignore[return-value]

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        subscription_id = _local_id(subscription_id, "subscription ID")
        row = self._conn.execute(
            "SELECT * FROM discovery_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return self._subscription_row(row) if row else None

    @staticmethod
    def _subscription_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "subscription_id": str(row["subscription_id"]),
            "profile_id": str(row["profile_id"]),
            "intents": json.loads(row["intents_json"]),
            "topics": json.loads(row["topics_json"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "languages": json.loads(row["languages_json"]),
            "min_score": int(row["min_score"]),
            "max_age_ms": int(row["max_age_ms"]),
            "enabled": bool(row["enabled"]),
            "created_ms": int(row["created_ms"]),
        }

    def subscriptions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM discovery_subscriptions ORDER BY subscription_id"
        ).fetchall()
        return [self._subscription_row(row) for row in rows]

    def ingest(
        self,
        signal: Mapping[str, Any],
        *,
        sender_node_id: str,
        received_ms: int | None = None,
    ) -> dict[str, Any]:
        signal = validate_discovery_signal(dict(signal))
        sender_node_id = _node_id(sender_node_id)
        received_ms = int(received_ms or _now_ms())
        existing = self._conn.execute(
            "SELECT body_json, sender_node_id FROM discovery_signals WHERE signal_id = ?",
            (signal["signal_id"],),
        ).fetchone()
        if existing is not None:
            if json.loads(existing["body_json"]) != signal or str(existing["sender_node_id"]) != sender_node_id:
                raise ValueError("discovery signal ID conflicts with stored sender/body")
            return {"signal_id": signal["signal_id"], "duplicate": True, "matches": 0}
        if signal["expires_ms"] <= received_ms:
            return {"signal_id": signal["signal_id"], "expired": True, "matches": 0}
        self._conn.execute(
            "INSERT INTO discovery_signals(signal_id, sender_node_id, body_json, received_ms) VALUES (?, ?, ?, ?)",
            (signal["signal_id"], sender_node_id, _json(signal), received_ms),
        )
        matches = 0
        for subscription in self.subscriptions():
            profile = self.profile(subscription["profile_id"])
            if not profile or not profile["enabled"] or not subscription["enabled"]:
                continue
            score, reasons = _match_signal(signal, profile, subscription, received_ms)
            if score < 0 or score < subscription["min_score"]:
                continue
            self._conn.execute(
                """
                INSERT OR IGNORE INTO discovery_matches(
                    subscription_id, signal_id, score, reasons_json, matched_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    subscription["subscription_id"],
                    signal["signal_id"],
                    score,
                    _json(reasons),
                    received_ms,
                ),
            )
            matches += 1
        return {"signal_id": signal["signal_id"], "duplicate": False, "matches": matches}

    def feed(
        self,
        subscription_id: str,
        *,
        after: int = 0,
        limit: int = 50,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        subscription_id = _local_id(subscription_id, "subscription ID")
        if not 0 <= int(after):
            raise ValueError("feed cursor must be non-negative")
        if not 1 <= int(limit) <= 200:
            raise ValueError("feed limit must be 1 to 200")
        now_ms = int(now_ms or _now_ms())
        rows = self._conn.execute(
            """
            SELECT m.match_id, m.subscription_id, m.signal_id, m.score,
                   m.reasons_json, m.matched_ms, s.sender_node_id,
                   s.body_json, f.verdict, f.note
            FROM discovery_matches m
            JOIN discovery_signals s ON s.signal_id = m.signal_id
            LEFT JOIN discovery_feedback f
              ON f.subscription_id = m.subscription_id
             AND f.signal_id = m.signal_id
            WHERE m.subscription_id = ? AND m.match_id > ?
              AND json_extract(s.body_json, '$.expires_ms') > ?
            ORDER BY m.match_id ASC LIMIT ?
            """,
            (subscription_id, int(after), now_ms, int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "cursor": int(row["match_id"]),
                    "subscription_id": str(row["subscription_id"]),
                    "signal_id": str(row["signal_id"]),
                    "sender_node_id": str(row["sender_node_id"]),
                    "score": int(row["score"]),
                    "reasons": json.loads(row["reasons_json"]),
                    "matched_ms": int(row["matched_ms"]),
                    "signal": json.loads(row["body_json"]),
                    "feedback": (
                        {"verdict": str(row["verdict"]), "note": str(row["note"])}
                        if row["verdict"] is not None
                        else None
                    ),
                }
            )
        return {
            "subscription_id": subscription_id,
            "items": items,
            "next_cursor": items[-1]["cursor"] if items else int(after),
            "limited": len(items) == int(limit),
        }

    def add_feedback(
        self,
        subscription_id: str,
        signal_id: str,
        verdict: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        subscription_id = _local_id(subscription_id, "subscription ID")
        signal_id = _string(signal_id, "signal ID", limit=32).strip().lower()
        if not _HEX32_RE.fullmatch(signal_id):
            raise ValueError("invalid signal ID")
        verdict = _string(verdict, "feedback verdict").strip().lower()
        if verdict not in DISCOVERY_FEEDBACK:
            raise ValueError("unsupported feedback verdict")
        note = _string(note, "feedback note", limit=256)
        if self.subscription(subscription_id) is None:
            raise ValueError("unknown subscription")
        if self._conn.execute(
            "SELECT 1 FROM discovery_signals WHERE signal_id = ?", (signal_id,)
        ).fetchone() is None:
            raise ValueError("unknown discovery signal")
        existing = self._conn.execute(
            "SELECT verdict, note FROM discovery_feedback WHERE subscription_id = ? AND signal_id = ?",
            (subscription_id, signal_id),
        ).fetchone()
        if existing is not None:
            if (str(existing["verdict"]), str(existing["note"])) != (verdict, note):
                raise ValueError("feedback is immutable for a subscription/signal")
            return {"subscription_id": subscription_id, "signal_id": signal_id, "duplicate": True, "verdict": verdict, "note": note}
        self._conn.execute(
            "INSERT INTO discovery_feedback(subscription_id, signal_id, verdict, note, created_ms) VALUES (?, ?, ?, ?, ?)",
            (subscription_id, signal_id, verdict, note, _now_ms()),
        )
        return {"subscription_id": subscription_id, "signal_id": signal_id, "duplicate": False, "verdict": verdict, "note": note}

    def status(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "profiles": count("discovery_profiles"),
            "subscriptions": count("discovery_subscriptions"),
            "signals": count("discovery_signals"),
            "matches": count("discovery_matches"),
            "feedback": count("discovery_feedback"),
        }


def _match_signal(
    signal: Mapping[str, Any],
    profile: Mapping[str, Any],
    subscription: Mapping[str, Any],
    received_ms: int,
) -> tuple[int, list[str]]:
    if signal["visibility"] == "tenant" and signal["tenant"] != profile["tenant"]:
        return -1, ["tenant mismatch"]
    if subscription["intents"] and signal["intent"] not in subscription["intents"]:
        return -1, ["intent mismatch"]
    reasons: list[str] = []
    score = 0
    signal_topics = set(signal["topics"])
    signal_capabilities = set(signal["capabilities"])
    signal_languages = set(signal["languages"])
    wanted_topics = set(subscription["topics"] or profile["topics"])
    wanted_capabilities = set(subscription["capabilities"] or profile["capabilities"])
    wanted_languages = set(subscription["languages"] or profile["languages"])
    if wanted_topics:
        overlap = sorted(signal_topics & wanted_topics)
        if overlap:
            score += 45
            reasons.append("topics:" + ",".join(overlap))
    if wanted_capabilities:
        overlap = sorted(signal_capabilities & wanted_capabilities)
        if overlap:
            score += 30
            reasons.append("capabilities:" + ",".join(overlap))
    if wanted_languages:
        overlap = sorted(signal_languages & wanted_languages)
        if overlap:
            score += 10
            reasons.append("languages:" + ",".join(overlap))
    if subscription["intents"]:
        score += 10
        reasons.append("intent:" + str(signal["intent"]))
    age_ms = max(0, int(received_ms) - int(signal["published_ms"]))
    if age_ms <= int(subscription["max_age_ms"]):
        score += 5
        reasons.append("freshness:within-subscription-window")
    return min(100, score), reasons or ["no declared profile overlap"]
