from __future__ import annotations

import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .model import (
    PermissionRule,
    validate_action,
    validate_adapter_name,
    validate_actor_key,
    validate_effect,
)


def amesh_database_path(home: Path) -> Path:
    return Path(home) / "amesh-permissions.sqlite3"


def amesh_config_path(home: Path) -> Path:
    return Path(home) / "amesh.json"


class PermissionStore:
    """Operator permission rules and their decision audit, stored per node home.

    Rules refine the Anet evidence thresholds. They never grant an action the
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
