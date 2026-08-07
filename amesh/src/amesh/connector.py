from __future__ import annotations

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .adapter import load_adapter
from .agent import AgentStore, agent_database_path
from .model import validate_action, validate_adapter_name, validate_event_key

MAX_BODY_BYTES = 16 * 1024


def amesh_audit_path(home: Path) -> Path:
    return Path(home) / "amesh-audit.sqlite3"


class ConnectorAudit:
    """Append-only audit of every authenticated connector request.

    Each entry records the authenticated agent, adapter, action, event, the
    outcome (authorized / denied / rejected), the HTTP status, and an optional
    error string. Tokens never appear here.
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
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS amesh_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                adapter TEXT NOT NULL,
                action TEXT NOT NULL,
                event_key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                code INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_ms INTEGER NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def record(
        self,
        *,
        agent_id: str = "",
        adapter: str = "",
        action: str = "",
        event_key: str = "",
        outcome: str = "",
        code: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        created_ms = int(time.time() * 1000)
        error = str(error)[:256]
        self._conn.execute(
            """
            INSERT INTO amesh_audit(
                agent_id, adapter, action, event_key, outcome, code, error, created_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, adapter, action, event_key, outcome, code, error, created_ms),
        )
        return {
            "agent_id": agent_id,
            "adapter": adapter,
            "action": action,
            "event_key": event_key,
            "outcome": outcome,
            "code": code,
            "error": error,
            "created_ms": created_ms,
        }

    def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("audit limit must be 1 to 10000")
        rows = self._conn.execute(
            "SELECT * FROM amesh_audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]


class _ConnectorHandler(BaseHTTPRequestHandler):
    connector: "EffectConnector | None" = None

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/v1/health":
            self._send(404, {"error": "not found"})
            return
        self._send(200, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path != "/v1/effects":
            self._send(404, {"error": "not found"})
            return
        self._handle_effect()

    def _handle_effect(self) -> None:
        if self.connector is None:  # pragma: no cover - always attached
            self._send(500, {"error": "connector is not initialized"})
            return
        audit = self.connector.audit
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            audit.record(outcome="rejected", code=401, error="bearer token required")
            self._send(401, {"error": "bearer token required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("request body is too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            audit.record(
                outcome="rejected", code=400, error=f"invalid request body: {exc}"
            )
            self._send(400, {"error": "invalid request body"})
            return
        adapter_name = str(body.get("adapter", "")).strip().lower()
        action = str(body.get("action", "")).strip().lower()
        event_key = str(body.get("event_key", "")).strip().lower()
        content = str(body.get("content", ""))
        try:
            adapter_name = validate_adapter_name(adapter_name)
            action = validate_action(action)
            event_key = validate_event_key(event_key)
            if action != "reply":
                raise ValueError("unsupported effect action")
        except ValueError as exc:
            audit.record(
                adapter=adapter_name,
                action=action,
                event_key=event_key,
                outcome="rejected",
                code=400,
                error=str(exc),
            )
            self._send(400, {"error": str(exc)})
            return

        store = AgentStore(agent_database_path(self.connector.home))
        adapter = None
        agent_id = ""
        try:
            try:
                agent_id = store.authenticate(token).agent_id
            except PermissionError as exc:
                audit.record(
                    adapter=adapter_name,
                    action=action,
                    event_key=event_key,
                    outcome="rejected",
                    code=401,
                    error=str(exc),
                )
                self._send(401, {"error": "invalid or disabled agent token"})
                return
            adapter = load_adapter(self.connector.home, adapter_name)
            adapter.require_agent(agent_id, action, token=token)
            result = adapter.reply(event_key, content)
            audit.record(
                agent_id=agent_id,
                adapter=adapter_name,
                action=action,
                event_key=event_key,
                outcome="authorized",
                code=200,
            )
            self._send(200, result)
        except PermissionError as exc:
            audit.record(
                agent_id=agent_id,
                adapter=adapter_name,
                action=action,
                event_key=event_key,
                outcome="denied",
                code=403,
                error=str(exc),
            )
            self._send(403, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            audit.record(
                agent_id=agent_id,
                adapter=adapter_name,
                action=action,
                event_key=event_key,
                outcome="rejected",
                code=400,
                error=str(exc),
            )
            self._send(400, {"error": str(exc)})
        finally:
            if adapter is not None:
                adapter.close()
            store.close()

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EffectConnector:
    """Local loopback HTTP boundary where Agents authenticate with bearer tokens.

    The connector authenticates the bearer token against the Amesh agent
    registry, requires an explicit adapter/action grant, executes the effect on
    the adapter, and records every request in the append-only audit. It binds
    to loopback by default and never exposes agent or platform tokens.
    """

    def __init__(self, home: Path, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.home = Path(home)
        self.audit = ConnectorAudit(amesh_audit_path(self.home))
        self.host = str(host)
        self.port = int(port)
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> "EffectConnector":
        handler = _ConnectorHandler
        handler.connector = self
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._server.server_address[1])
        return self

    def serve_forever(self) -> None:
        try:
            if self._server is None:  # pragma: no cover - call start() first
                raise RuntimeError("connector has not been started")
            self._server.serve_forever()
        finally:
            self.close()

    def shutdown(self) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
            self._server = None
        self.audit.close()

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()
            self._server = None
        self.audit.close()
