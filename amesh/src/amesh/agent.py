from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENT_ACTIONS = ("observe", "surface", "reply", "amplify", "admin")
AGENT_EFFECTS = ("allow", "deny")


def agent_database_path(home: Path) -> Path:
    return Path(home) / "amesh-agents.sqlite3"


def _agent_id(value: Any) -> str:
    text = str(value).strip().lower()
    if (
        not text
        or len(text) > 64
        or not text.replace("_", "").replace("-", "").isalnum()
    ):
        raise ValueError("invalid Amesh agent ID")
    return text


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    name: str
    scopes: tuple[str, ...]
    enabled: bool
    created_ms: int
    last_seen_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "scopes": list(self.scopes),
            "enabled": self.enabled,
            "created_ms": self.created_ms,
            "last_seen_ms": self.last_seen_ms,
        }


class AgentStore:
    """Local agent registry and capability grant audit.

    Tokens are returned only at registration time. The database stores only a
    SHA-256 digest, while grants are explicit per adapter/action and default
    to deny for external effects.
    """

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
            CREATE TABLE IF NOT EXISTS amesh_agents(
                agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                scopes_json TEXT NOT NULL, enabled INTEGER NOT NULL,
                created_ms INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS amesh_agent_grants(
                grant_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, adapter TEXT NOT NULL,
                action TEXT NOT NULL, effect TEXT NOT NULL, reason TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_amesh_agent_grants
                ON amesh_agent_grants(agent_id, adapter, action);
            """
        )

    def close(self) -> None:
        self._conn.close()

    def register(
        self, agent_id: str, name: str, *, scopes: tuple[str, ...] | list[str] = ()
    ) -> dict[str, Any]:
        agent_id = _agent_id(agent_id)
        name = str(name).strip()
        if not name or len(name) > 128:
            raise ValueError("Amesh agent name is invalid")
        normalized = tuple(sorted({str(scope).strip().lower() for scope in scopes}))
        if any(scope not in AGENT_ACTIONS for scope in normalized):
            raise ValueError("unknown Amesh agent scope")
        token = secrets.token_urlsafe(32)
        current = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO amesh_agents VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                agent_id,
                name,
                hashlib.sha256(token.encode()).hexdigest(),
                json.dumps(normalized),
                current,
                current,
            ),
        )
        record = self.get(agent_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("registered Amesh agent disappeared")
        return {"agent": record.to_dict(), "token": token}

    def get(self, agent_id: str) -> AgentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM amesh_agents WHERE agent_id=?", (_agent_id(agent_id),)
        ).fetchone()
        if row is None:
            return None
        return AgentRecord(
            str(row["agent_id"]),
            str(row["name"]),
            tuple(json.loads(row["scopes_json"])),
            bool(row["enabled"]),
            int(row["created_ms"]),
            int(row["last_seen_ms"]),
        )

    def list(self) -> list[AgentRecord]:
        rows = self._conn.execute(
            "SELECT agent_id FROM amesh_agents ORDER BY agent_id"
        ).fetchall()
        return [self.get(str(row["agent_id"])) for row in rows]  # type: ignore[list-item]

    def revoke(self, agent_id: str) -> bool:
        changed = self._conn.execute(
            "UPDATE amesh_agents SET enabled=0 WHERE agent_id=?", (_agent_id(agent_id),)
        ).rowcount
        return changed == 1

    def authenticate(self, token: str) -> AgentRecord:
        digest = hashlib.sha256(str(token).encode()).hexdigest()
        row = self._conn.execute(
            "SELECT agent_id FROM amesh_agents WHERE token_hash=? AND enabled=1",
            (digest,),
        ).fetchone()
        if row is None:
            raise PermissionError("invalid or disabled Amesh agent token")
        agent_id = str(row["agent_id"])
        self._conn.execute(
            "UPDATE amesh_agents SET last_seen_ms=? WHERE agent_id=?",
            (int(time.time() * 1000), agent_id),
        )
        record = self.get(agent_id)
        if record is None:  # pragma: no cover
            raise PermissionError("Amesh agent disappeared")
        return record

    def grant(
        self, agent_id: str, adapter: str, action: str, effect: str, *, reason: str = ""
    ) -> dict[str, Any]:
        agent_id = _agent_id(agent_id)
        if self.get(agent_id) is None:
            raise ValueError("unknown Amesh agent")
        adapter = str(adapter).strip().lower()
        action = str(action).strip().lower()
        effect = str(effect).strip().lower()
        if not adapter or action not in AGENT_ACTIONS or effect not in AGENT_EFFECTS:
            raise ValueError("invalid Amesh agent grant")
        grant_id = secrets.token_hex(16)
        self._conn.execute(
            "INSERT INTO amesh_agent_grants VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                agent_id,
                adapter,
                action,
                effect,
                str(reason)[:256],
                int(time.time() * 1000),
            ),
        )
        return {
            "grant_id": grant_id,
            "agent_id": agent_id,
            "adapter": adapter,
            "action": action,
            "effect": effect,
            "reason": str(reason)[:256],
        }

    def authorize(self, agent_id: str, adapter: str, action: str) -> bool:
        agent_id = _agent_id(agent_id)
        record = self.get(agent_id)
        if record is None or not record.enabled or action not in record.scopes:
            return False
        row = self._conn.execute(
            "SELECT effect FROM amesh_agent_grants WHERE agent_id=? AND adapter=? AND action=? ORDER BY created_ms DESC, grant_id DESC LIMIT 1",
            (agent_id, str(adapter).lower(), str(action).lower()),
        ).fetchone()
        return row is not None and str(row["effect"]) == "allow"
