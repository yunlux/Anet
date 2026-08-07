"""Standalone profile, signal and feed primitives for Amesh."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .time import now_ms

DISCOVERY_SIGNAL_KIND = "amesh.discovery.signal"
DISCOVERY_PROTOCOL = "amesh.social.discovery"
DISCOVERY_VERSION = 1
DISCOVERY_INTENTS = ("know", "need", "offer", "capability")
DISCOVERY_VISIBILITIES = ("public", "tenant")
DISCOVERY_FEEDBACK = ("useful", "not_relevant", "spam")

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.:/-]{0,63}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_FIELDS = {
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
    return Path(home) / "amesh-discovery.sqlite3"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} must be bounded text")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _list(value: Any, label: str, *, maximum: int, token: bool) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = []
    for item in value:
        text = _text(item, label, 64).strip().lower()
        if not text or (token and not _TOKEN_RE.fullmatch(text)):
            raise ValueError(f"invalid {label} item")
        result.append(text)
    if result != sorted(set(result)):
        raise ValueError(f"{label} must be unique and sorted")
    return result


def _provenance(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 8:
        raise ValueError("discovery provenance must have 1 to 8 fields")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = str(key)
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", key_text):
            raise ValueError("invalid discovery provenance field")
        result[key_text] = _text(item, f"provenance {key_text}", 128)
    if "source" not in result or "adapter" not in result:
        raise ValueError("discovery provenance requires source and adapter")
    return result


def _digest(value: Mapping[str, Any]) -> str:
    seed = {key: value[key] for key in sorted(_FIELDS - {"signal_id"})}
    return hashlib.blake2s(
        _json(seed).encode("utf-8"), digest_size=16, person=b"amshds1"
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
    value = {
        "protocol": DISCOVERY_PROTOCOL,
        "version": DISCOVERY_VERSION,
        "signal_id": "0" * 32,
        "published_ms": _positive_int(published_ms, "published_ms"),
        "expires_ms": _positive_int(expires_ms, "expires_ms"),
        "intent": str(intent).strip().lower(),
        "summary": _text(summary, "summary", 1000).strip(),
        "topics": sorted({str(item).strip().lower() for item in topics}),
        "capabilities": sorted({str(item).strip().lower() for item in capabilities}),
        "languages": sorted({str(item).strip().lower() for item in languages}),
        "visibility": str(visibility).strip().lower(),
        "tenant": str(tenant).strip(),
        "provenance": dict(provenance),
    }
    value["signal_id"] = _digest(value)
    return validate_discovery_signal(value)


def validate_discovery_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("discovery signal has invalid fields")
    if value["protocol"] != DISCOVERY_PROTOCOL or value["version"] != DISCOVERY_VERSION:
        raise ValueError("unsupported Amesh discovery protocol")
    signal_id = _text(value["signal_id"], "signal_id", 32).strip().lower()
    if not _HEX32_RE.fullmatch(signal_id):
        raise ValueError("invalid discovery signal ID")
    published_ms = _positive_int(value["published_ms"], "published_ms")
    expires_ms = _positive_int(value["expires_ms"], "expires_ms")
    if not published_ms < expires_ms <= published_ms + 7 * 86_400_000:
        raise ValueError("invalid discovery signal lifetime")
    intent = _text(value["intent"], "intent", 32).strip().lower()
    if intent not in DISCOVERY_INTENTS:
        raise ValueError("unsupported discovery intent")
    summary = _text(value["summary"], "summary", 1000).strip()
    if not summary:
        raise ValueError("discovery summary cannot be empty")
    topics = _list(value["topics"], "topics", maximum=32, token=True)
    capabilities = _list(value["capabilities"], "capabilities", maximum=32, token=True)
    languages = _list(value["languages"], "languages", maximum=8, token=False)
    visibility = _text(value["visibility"], "visibility", 16).strip().lower()
    if visibility not in DISCOVERY_VISIBILITIES:
        raise ValueError("unsupported discovery visibility")
    tenant = _text(value["tenant"], "tenant", 64).strip()
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
    if _digest(normalized) != signal_id:
        raise ValueError("discovery signal digest does not match its body")
    return normalized


def _local_id(value: Any, label: str) -> str:
    text = _text(value, label, 64).strip().lower()
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"invalid {label}")
    return text


def _source_id(value: Any) -> str:
    text = _text(value, "source ID", 128).strip()
    if not _SOURCE_RE.fullmatch(text):
        raise ValueError("invalid source ID")
    return text


class DiscoveryStore:
    """Observer-local discovery state; it never grants agent authority."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovery_profiles (
                profile_id TEXT PRIMARY KEY, topics_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL, languages_json TEXT NOT NULL,
                tenant TEXT NOT NULL, enabled INTEGER NOT NULL, updated_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_subscriptions (
                subscription_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                intents_json TEXT NOT NULL, topics_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL, languages_json TEXT NOT NULL,
                min_score INTEGER NOT NULL, max_age_ms INTEGER NOT NULL,
                enabled INTEGER NOT NULL, created_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_signals (
                signal_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                body_json TEXT NOT NULL, received_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id TEXT NOT NULL, signal_id TEXT NOT NULL,
                score INTEGER NOT NULL, reasons_json TEXT NOT NULL,
                matched_ms INTEGER NOT NULL,
                UNIQUE(subscription_id, signal_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_feedback (
                subscription_id TEXT NOT NULL, signal_id TEXT NOT NULL,
                verdict TEXT NOT NULL, note TEXT NOT NULL, created_ms INTEGER NOT NULL,
                PRIMARY KEY(subscription_id, signal_id)
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def set_profile(self, profile_id: str, *, topics=(), capabilities=(), languages=(), tenant="", enabled=True) -> dict[str, Any]:
        profile_id = _local_id(profile_id, "profile ID")
        topics = sorted({str(item).strip().lower() for item in topics})
        capabilities = sorted({str(item).strip().lower() for item in capabilities})
        languages = sorted({str(item).strip().lower() for item in languages})
        _list(topics, "profile topics", maximum=64, token=True)
        _list(capabilities, "profile capabilities", maximum=64, token=True)
        _list(languages, "profile languages", maximum=8, token=False)
        tenant = _text(tenant, "profile tenant", 64).strip()
        self._conn.execute(
            """INSERT INTO discovery_profiles VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(profile_id) DO UPDATE SET topics_json=excluded.topics_json,
               capabilities_json=excluded.capabilities_json, languages_json=excluded.languages_json,
               tenant=excluded.tenant, enabled=excluded.enabled, updated_ms=excluded.updated_ms""",
            (profile_id, _json(topics), _json(capabilities), _json(languages), tenant, int(enabled), now_ms()),
        )
        return self.profile(profile_id)  # type: ignore[return-value]

    def profile(self, profile_id: str = "") -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM discovery_profiles" + (" WHERE profile_id = ?" if profile_id else " ORDER BY profile_id LIMIT 1"),
            (_local_id(profile_id, "profile ID"),) if profile_id else (),
        ).fetchone()
        if row is None:
            return None
        return {
            "profile_id": str(row["profile_id"]),
            "topics": json.loads(row["topics_json"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "languages": json.loads(row["languages_json"]),
            "tenant": str(row["tenant"]),
            "enabled": bool(row["enabled"]),
            "updated_ms": int(row["updated_ms"]),
        }

    def add_subscription(self, subscription_id: str, *, profile_id: str, intents=(), topics=(), capabilities=(), languages=(), min_score=1, max_age_seconds=7 * 86_400, enabled=True) -> dict[str, Any]:
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
        _list(topics, "subscription topics", maximum=64, token=True)
        _list(capabilities, "subscription capabilities", maximum=64, token=True)
        _list(languages, "subscription languages", maximum=8, token=False)
        if isinstance(min_score, bool) or not 0 <= int(min_score) <= 100:
            raise ValueError("subscription min_score must be 0 to 100")
        if isinstance(max_age_seconds, bool) or not 60 <= int(max_age_seconds) <= 7 * 86_400:
            raise ValueError("subscription max age is outside limits")
        self._conn.execute(
            """INSERT INTO discovery_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(subscription_id) DO UPDATE SET profile_id=excluded.profile_id,
               intents_json=excluded.intents_json, topics_json=excluded.topics_json,
               capabilities_json=excluded.capabilities_json, languages_json=excluded.languages_json,
               min_score=excluded.min_score, max_age_ms=excluded.max_age_ms, enabled=excluded.enabled""",
            (subscription_id, profile_id, _json(intents), _json(topics), _json(capabilities), _json(languages), int(min_score), int(max_age_seconds) * 1000, int(enabled), now_ms()),
        )
        return self.subscription(subscription_id)  # type: ignore[return-value]

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM discovery_subscriptions WHERE subscription_id = ?",
            (_local_id(subscription_id, "subscription ID"),),
        ).fetchone()
        if row is None:
            return None
        return {
            "subscription_id": str(row["subscription_id"]), "profile_id": str(row["profile_id"]),
            "intents": json.loads(row["intents_json"]), "topics": json.loads(row["topics_json"]),
            "capabilities": json.loads(row["capabilities_json"]), "languages": json.loads(row["languages_json"]),
            "min_score": int(row["min_score"]), "max_age_ms": int(row["max_age_ms"]),
            "enabled": bool(row["enabled"]), "created_ms": int(row["created_ms"]),
        }

    def subscriptions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT subscription_id FROM discovery_subscriptions ORDER BY subscription_id").fetchall()
        return [self.subscription(str(row["subscription_id"])) for row in rows]  # type: ignore[list-item]

    def ingest(self, signal: Mapping[str, Any], *, source_id: str, received_ms: int | None = None) -> dict[str, Any]:
        signal = validate_discovery_signal(dict(signal))
        source_id = _source_id(source_id)
        received_ms = int(received_ms or now_ms())
        existing = self._conn.execute("SELECT body_json, source_id FROM discovery_signals WHERE signal_id = ?", (signal["signal_id"],)).fetchone()
        if existing is not None:
            if json.loads(existing["body_json"]) != signal or str(existing["source_id"]) != source_id:
                raise ValueError("discovery signal ID conflicts with stored source/body")
            return {"signal_id": signal["signal_id"], "duplicate": True, "matches": 0}
        if signal["expires_ms"] <= received_ms:
            return {"signal_id": signal["signal_id"], "expired": True, "matches": 0}
        self._conn.execute("INSERT INTO discovery_signals VALUES (?, ?, ?, ?)", (signal["signal_id"], source_id, _json(signal), received_ms))
        matches = 0
        for subscription in self.subscriptions():
            profile = self.profile(subscription["profile_id"])
            if not profile or not profile["enabled"] or not subscription["enabled"]:
                continue
            score, reasons = _match_signal(signal, profile, subscription, received_ms)
            if score < 0 or score < subscription["min_score"]:
                continue
            self._conn.execute(
                "INSERT OR IGNORE INTO discovery_matches(subscription_id, signal_id, score, reasons_json, matched_ms) VALUES (?, ?, ?, ?, ?)",
                (subscription["subscription_id"], signal["signal_id"], score, _json(reasons), received_ms),
            )
            matches += 1
        return {"signal_id": signal["signal_id"], "duplicate": False, "matches": matches}

    def feed(self, subscription_id: str, *, after: int = 0, limit: int = 50, now_ms_value: int | None = None) -> dict[str, Any]:
        subscription_id = _local_id(subscription_id, "subscription ID")
        if int(after) < 0 or not 1 <= int(limit) <= 200:
            raise ValueError("invalid discovery feed cursor or limit")
        current = int(now_ms_value or now_ms())
        rows = self._conn.execute(
            """SELECT m.match_id, m.subscription_id, m.signal_id, m.score, m.reasons_json,
               m.matched_ms, s.source_id, s.body_json, f.verdict, f.note
               FROM discovery_matches m JOIN discovery_signals s ON s.signal_id=m.signal_id
               LEFT JOIN discovery_feedback f ON f.subscription_id=m.subscription_id AND f.signal_id=m.signal_id
               WHERE m.subscription_id=? AND m.match_id>? ORDER BY m.match_id ASC LIMIT ?""",
            (subscription_id, int(after), int(limit) * 4),
        ).fetchall()
        items = []
        for row in rows:
            signal = json.loads(row["body_json"])
            if int(signal["expires_ms"]) <= current:
                continue
            items.append({
                "cursor": int(row["match_id"]), "subscription_id": str(row["subscription_id"]),
                "signal_id": str(row["signal_id"]), "source_id": str(row["source_id"]),
                "score": int(row["score"]), "reasons": json.loads(row["reasons_json"]),
                "matched_ms": int(row["matched_ms"]), "signal": signal,
                "feedback": ({"verdict": str(row["verdict"]), "note": str(row["note"])} if row["verdict"] is not None else None),
            })
            if len(items) >= int(limit):
                break
        return {"subscription_id": subscription_id, "items": items, "next_cursor": items[-1]["cursor"] if items else int(after), "limited": len(items) == int(limit)}

    def add_feedback(self, subscription_id: str, signal_id: str, verdict: str, *, note: str = "") -> dict[str, Any]:
        subscription_id = _local_id(subscription_id, "subscription ID")
        signal_id = _text(signal_id, "signal ID", 32).strip().lower()
        verdict = _text(verdict, "feedback verdict", 32).strip().lower()
        note = _text(note, "feedback note", 256)
        if not _HEX32_RE.fullmatch(signal_id) or verdict not in DISCOVERY_FEEDBACK:
            raise ValueError("invalid discovery feedback")
        if self.subscription(subscription_id) is None or self._conn.execute("SELECT 1 FROM discovery_signals WHERE signal_id=?", (signal_id,)).fetchone() is None:
            raise ValueError("unknown discovery subscription or signal")
        existing = self._conn.execute("SELECT verdict, note FROM discovery_feedback WHERE subscription_id=? AND signal_id=?", (subscription_id, signal_id)).fetchone()
        if existing is not None:
            if (str(existing["verdict"]), str(existing["note"])) != (verdict, note):
                raise ValueError("feedback is immutable for a subscription/signal")
            return {"subscription_id": subscription_id, "signal_id": signal_id, "duplicate": True, "verdict": verdict, "note": note}
        self._conn.execute("INSERT INTO discovery_feedback VALUES (?, ?, ?, ?, ?)", (subscription_id, signal_id, verdict, note, now_ms()))
        return {"subscription_id": subscription_id, "signal_id": signal_id, "duplicate": False, "verdict": verdict, "note": note}

    def status(self) -> dict[str, int]:
        return {table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("discovery_profiles", "discovery_subscriptions", "discovery_signals", "discovery_matches", "discovery_feedback")}


def _match_signal(signal: Mapping[str, Any], profile: Mapping[str, Any], subscription: Mapping[str, Any], received_ms: int) -> tuple[int, list[str]]:
    if signal["visibility"] == "tenant" and signal["tenant"] != profile["tenant"]:
        return -1, ["tenant mismatch"]
    if subscription["intents"] and signal["intent"] not in subscription["intents"]:
        return -1, ["intent mismatch"]
    score = 0
    reasons: list[str] = []
    for name, weight, signal_key, profile_key in (
        ("topics", 45, "topics", "topics"),
        ("capabilities", 30, "capabilities", "capabilities"),
        ("languages", 10, "languages", "languages"),
    ):
        wanted = set(subscription[profile_key] or profile[profile_key])
        overlap = sorted(set(signal[signal_key]) & wanted)
        if overlap:
            score += weight
            reasons.append(name + ":" + ",".join(overlap))
    if subscription["intents"]:
        score += 10
        reasons.append("intent:" + str(signal["intent"]))
    if int(received_ms) - int(signal["published_ms"]) <= int(subscription["max_age_ms"]):
        score += 5
        reasons.append("freshness:within-subscription-window")
    return min(100, score), reasons or ["no declared profile overlap"]
