from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Mapping
from pathlib import Path
from typing import Any

from .model import (
    PermissionRule,
    validate_action,
    validate_adapter_name,
    validate_actor_key,
    validate_effect,
)

SOCIAL_POLICY_VERSION = 1
SOCIAL_ACTIONS = (
    "observe",
    "surface",
    "reply",
    "amplify",
    "connect_candidate",
)
_LABEL_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{0,31}:[a-z0-9][a-z0-9_.-]{0,63}$"
)
_BLOCKING_LABELS = frozenset(
    {"risk:block", "risk:impersonation", "risk:malware", "risk:spam"}
)


def normalize_social_label(value: str, *, manual: bool = False) -> str:
    label = str(value).strip().lower()
    if not _LABEL_RE.fullmatch(label):
        raise ValueError("invalid social label")
    if manual and label.partition(":")[0] not in {
        "community",
        "interest",
        "language",
        "relationship",
        "risk",
        "status",
    }:
        raise ValueError("manual social label uses a reserved prefix")
    return label


@dataclass(frozen=True)
class SocialThreshold:
    min_score: int
    min_confidence: int
    required_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= int(self.min_score) <= 100:
            raise ValueError("social threshold score must be between 0 and 100")
        if not 0 <= int(self.min_confidence) <= 100:
            raise ValueError("social threshold confidence must be between 0 and 100")
        object.__setattr__(
            self,
            "required_labels",
            tuple(sorted({normalize_social_label(label) for label in self.required_labels})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_score": self.min_score,
            "min_confidence": self.min_confidence,
            "required_labels": list(self.required_labels),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SocialThreshold":
        if not isinstance(value, Mapping) or set(value) != {
            "min_score", "min_confidence", "required_labels"
        }:
            raise ValueError("invalid social threshold")
        labels = value["required_labels"]
        if not isinstance(labels, list):
            raise ValueError("social threshold labels must be a list")
        return cls(
            min_score=_exact_int(value["min_score"], "threshold min_score"),
            min_confidence=_exact_int(value["min_confidence"], "threshold min_confidence"),
            required_labels=tuple(str(item) for item in labels),
        )


@dataclass(frozen=True)
class SocialPolicy:
    surface: SocialThreshold = SocialThreshold(45, 0)
    reply: SocialThreshold = SocialThreshold(60, 25)
    amplify: SocialThreshold = SocialThreshold(72, 50)
    connect_candidate: SocialThreshold = SocialThreshold(
        82, 70, ("relationship:vouched",)
    )
    version: int = SOCIAL_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != SOCIAL_POLICY_VERSION:
            raise ValueError("unsupported social policy version")
        scores = tuple(
            item.min_score
            for item in (self.surface, self.reply, self.amplify, self.connect_candidate)
        )
        confidences = tuple(
            item.min_confidence
            for item in (self.surface, self.reply, self.amplify, self.connect_candidate)
        )
        if scores != tuple(sorted(scores)) or confidences != tuple(sorted(confidences)):
            raise ValueError("social policy thresholds must be monotonic")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "surface": self.surface.to_dict(),
            "reply": self.reply.to_dict(),
            "amplify": self.amplify.to_dict(),
            "connect_candidate": self.connect_candidate.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SocialPolicy":
        if not isinstance(value, Mapping) or set(value) != {
            "version", "surface", "reply", "amplify", "connect_candidate"
        }:
            raise ValueError("invalid social policy")
        return cls(
            version=_exact_int(value["version"], "social policy version"),
            surface=SocialThreshold.from_dict(value["surface"]),
            reply=SocialThreshold.from_dict(value["reply"]),
            amplify=SocialThreshold.from_dict(value["amplify"]),
            connect_candidate=SocialThreshold.from_dict(value["connect_candidate"]),
        )

    def evaluate(
        self,
        stats: Mapping[str, Any],
        labels: set[str] | frozenset[str],
        event_labels: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        combined = {
            normalize_social_label(label) for label in set(labels) | set(event_labels)
        }
        reputation = score_social_actor(stats, set(labels))
        allowed = ["observe"]
        reasons = list(reputation["reasons"])
        blocked = sorted(combined & _BLOCKING_LABELS)
        automation = bool(combined & {"actor:bot", "actor:webhook"})
        if blocked:
            reasons.append("blocked by labels: " + ",".join(blocked))
        else:
            if self._passes(self.surface, reputation, combined):
                allowed.append("surface")
            if "interaction:mention" in combined and not automation and self._passes(
                self.reply, reputation, combined
            ):
                allowed.append("reply")
            if not automation and self._passes(self.amplify, reputation, combined):
                allowed.append("amplify")
            if not automation and self._passes(
                self.connect_candidate, reputation, combined
            ):
                allowed.append("connect_candidate")
        action = allowed[-1]
        if action == "connect_candidate":
            reasons.append("candidate only; social evidence cannot grant authority")
        return {
            "action": action,
            "allowed_actions": allowed,
            "reasons": reasons[:32],
            "policy_version": self.version,
            "reputation": reputation,
        }

    @staticmethod
    def _passes(
        threshold: SocialThreshold,
        reputation: Mapping[str, Any],
        labels: set[str],
    ) -> bool:
        return (
            int(reputation["score"]) >= threshold.min_score
            and int(reputation["confidence"]) >= threshold.min_confidence
            and set(threshold.required_labels).issubset(labels)
        )


def score_social_actor(
    stats: Mapping[str, Any], labels: set[str] | frozenset[str]
) -> dict[str, Any]:
    normalized = {normalize_social_label(label) for label in labels}
    points = 0
    confidence = 0
    reasons: list[str] = []
    for key, multiplier, cap, confidence_multiplier, confidence_cap, label in (
        ("account_age_days", 1 / 365, 5, 5, 5, "account age"),
        ("mention_count", 2, 10, 3, 15, "bounded mentions"),
        ("reply_count", 4, 12, 5, 20, "bounded replies"),
        ("reaction_count", 1, 8, 1, 10, "bounded reactions"),
        ("pinned_count", 8, 16, 10, 20, "bounded pinned messages"),
    ):
        value = max(0, min(int(stats.get(key, 0)), 1_000_000))
        increment = min(int(value * multiplier), cap)
        if increment:
            points += increment
            confidence += min(value * confidence_multiplier, confidence_cap)
            reasons.append(f"{label} +{increment}")
    weights = {
        "community:moderator": (10, 20),
        "relationship:known": (10, 20),
        "relationship:vouched": (30, 50),
        "status:verified": (15, 30),
        "risk:concern": (-30, 30),
        "risk:impersonation": (-80, 80),
        "risk:malware": (-100, 100),
        "risk:spam": (-60, 60),
        "risk:block": (-100, 100),
    }
    for label in sorted(normalized):
        weight = weights.get(label)
        if weight:
            points += weight[0]
            confidence += weight[1]
            reasons.append(f"operator label {label} {weight[0]:+d}")
    raw_score = max(0, min(100, 50 + points))
    confidence = max(0, min(100, confidence))
    score = round(50 + ((raw_score - 50) * confidence / 100))
    return {
        "score": score,
        "raw_score": raw_score,
        "confidence": confidence,
        "algorithm": "amesh-social-evidence-v1",
        "reasons": reasons or ["no reputation evidence"],
    }


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def amesh_database_path(home: Path) -> Path:
    return Path(home) / "amesh-permissions.sqlite3"


def amesh_config_path(home: Path) -> Path:
    return Path(home) / "amesh.json"


class PermissionStore:
    """Operator permission rules and their decision audit, stored per Amesh home.

    Rules refine the platform evidence thresholds. They never grant an action the
    evidence already rejected; ``allow`` only preserves an allowed action, and
    ``deny`` removes it.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS amesh_permission_rules (
                rule_id TEXT PRIMARY KEY,
                adapter TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                action TEXT NOT NULL,
                effect TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_amesh_rules_lookup
                ON amesh_permission_rules(adapter, actor_key, action);
            CREATE TABLE IF NOT EXISTS amesh_permission_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                adapter TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                action TEXT NOT NULL,
                effect TEXT NOT NULL,
                event_key TEXT NOT NULL DEFAULT '',
                decided_ms INTEGER NOT NULL
            );
            """
        )

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
        ):
            try:
                os.chmod(candidate, 0o600)
            except (FileNotFoundError, PermissionError):
                pass

    def close(self) -> None:
        self._conn.close()

    def add_rule(
        self,
        adapter: str,
        actor_key: str,
        action: str,
        effect: str,
        *,
        reason: str = "",
    ) -> PermissionRule:
        adapter = validate_adapter_name(adapter)
        actor_key = validate_actor_key(actor_key, wildcard=True)
        action = validate_action(action, wildcard=True)
        effect = validate_effect(effect)
        reason = str(reason).strip()[:256]
        current = int(time.time() * 1000)
        rule_id = secrets.token_hex(16)
        self._conn.execute(
            """
            INSERT INTO amesh_permission_rules(
                rule_id, adapter, actor_key, action, effect,
                reason, created_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rule_id, adapter, actor_key, action, effect, reason, current),
        )
        return self.require_rule(rule_id)

    def remove_rule(self, rule_id: str) -> bool:
        rule_id = str(rule_id).strip().lower()
        cursor = self._conn.execute(
            "DELETE FROM amesh_permission_rules WHERE rule_id = ?",
            (rule_id,),
        )
        return cursor.rowcount == 1

    def require_rule(self, rule_id: str) -> PermissionRule:
        rule = self._rule(rule_id)
        if rule is None:
            raise ValueError(f"unknown permission rule: {rule_id}")
        return rule

    def _rule(self, rule_id: str) -> PermissionRule | None:
        rule_id = str(rule_id).strip().lower()
        row = self._conn.execute(
            "SELECT * FROM amesh_permission_rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_rule(row)

    def rules(self, *, adapter: str = "", actor_key: str = "") -> list[PermissionRule]:
        query = "SELECT * FROM amesh_permission_rules"
        clauses: list[str] = []
        params: list[str] = []
        if adapter:
            clauses.append("adapter = ?")
            params.append(validate_adapter_name(adapter))
        if actor_key and actor_key != "*":
            clauses.append("actor_key = ?")
            params.append(validate_actor_key(actor_key))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_ms, rule_id"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_rule(row) for row in rows]

    @staticmethod
    def _row_to_rule(row: Any) -> PermissionRule:
        return PermissionRule(
            rule_id=str(row["rule_id"]),
            adapter=str(row["adapter"]),
            actor_key=str(row["actor_key"]),
            action=str(row["action"]),
            effect=str(row["effect"]),
            reason=str(row["reason"]),
            created_ms=int(row["created_ms"]),
        )

    def effective(
        self,
        adapter: str,
        actor_key: str,
        action: str,
    ) -> PermissionRule | None:
        """Return the winning rule for one adapter/actor/action lookup.

        Exact actor and exact action outrank wildcards; on equal specificity a
        deny rule wins; the most recent rule breaks a remaining tie.
        """
        adapter = validate_adapter_name(adapter)
        actor_key = validate_actor_key(actor_key)
        action = validate_action(action)
        rows = self._conn.execute(
            """
            SELECT * FROM amesh_permission_rules
            WHERE adapter = ?
              AND (actor_key = ? OR actor_key = '*')
              AND (action = ? OR action = '*')
            """,
            (adapter, actor_key, action),
        ).fetchall()
        if not rows:
            return None

        def rank(row: Any) -> tuple[int, int, int, int]:
            specific_actor = int(str(row["actor_key"]) != "*")
            specific_action = int(str(row["action"]) != "*")
            deny_first = int(str(row["effect"]) != "deny")
            return (
                1 - specific_actor,
                1 - specific_action,
                deny_first,
                -int(row["created_ms"]),
            )

        best = min(rows, key=rank)
        return self._row_to_rule(best)

    def apply_allowed(
        self,
        adapter: str,
        actor_key: str,
        allowed_actions: list[str] | tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        """Filter an evidence-derived action list through operator rules.

        Returns (surviving actions in original order, human-readable reasons).
        ``observe`` is always preserved.
        """
        result: list[str] = []
        reasons: list[str] = []
        for action in allowed_actions:
            if action == "observe":
                result.append(action)
                continue
            rule = self.effective(adapter, actor_key, action)
            if rule is None:
                result.append(action)
                continue
            if rule.effect == "deny":
                reasons.append(
                    f"{action} denied by rule {rule.rule_id}"
                    + (f" ({rule.reason})" if rule.reason else "")
                )
                continue
            reasons.append(f"{action} kept by rule {rule.rule_id}")
            result.append(action)
        return result, reasons

    def record_decision(
        self,
        adapter: str,
        actor_key: str,
        action: str,
        effect: str,
        *,
        event_key: str = "",
    ) -> None:
        adapter = validate_adapter_name(adapter)
        actor_key = validate_actor_key(actor_key)
        action = validate_action(action)
        effect = validate_effect(effect)
        self._conn.execute(
            """
            INSERT INTO amesh_permission_decisions(
                adapter, actor_key, action, effect, event_key, decided_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (adapter, actor_key, action, effect, event_key, int(time.time() * 1000)),
        )

    def decisions(self, *, adapter: str = "", limit: int = 100) -> list[dict[str, Any]]:
        limit = int(limit)
        if not 1 <= limit <= 10_000:
            raise ValueError("permission decision limit must be 1 to 10000")
        query = "SELECT * FROM amesh_permission_decisions"
        params: list[str] = []
        if adapter:
            query += " WHERE adapter = ?"
            params.append(validate_adapter_name(adapter))
        query += " ORDER BY decided_ms DESC, decision_id DESC LIMIT ?"
        rows = self._conn.execute(query, params + [str(limit)]).fetchall()
        return [
            {
                "decision_id": int(row["decision_id"]),
                "adapter": str(row["adapter"]),
                "actor_key": str(row["actor_key"]),
                "action": str(row["action"]),
                "effect": str(row["effect"]),
                "event_key": str(row["event_key"]),
                "decided_ms": int(row["decided_ms"]),
            }
            for row in rows
        ]
