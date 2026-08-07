from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .signal import canonical_pack

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_ROUTE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ROUTE_STATES = ("pending", "retrying", "delivered", "failed", "expired")
MAX_ATTEMPTS = 8
BASE_RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 3600.0
MAX_SIGNAL_JSON_BYTES = 64 * 1024


def route_database_path(home: Path) -> Path:
    return Path(home) / "amesh-routes.sqlite3"


class RouteStore:
    """Durable route/outbox state machine for one Amesh home.

    A route is a signal destined for one destination. Enqueue is idempotent
    per (destination, signal_id); delivery is attempted with exponential
    backoff until it succeeds, exceeds ``MAX_ATTEMPTS`` (failed), or the
    signal expires. A destination/adapter policy rule can deny a route before
    it is stored.
    """

    def __init__(self, path: Path, *, default_allow: bool = True) -> None:
        self.path = Path(path)
        self.default_allow = default_allow
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
            CREATE TABLE IF NOT EXISTS amesh_routes(
                route_id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                adapter TEXT NOT NULL,
                kind TEXT NOT NULL,
                signal_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_ms INTEGER NOT NULL DEFAULT 0,
                expires_ms INTEGER NOT NULL DEFAULT 0,
                dedup_key TEXT NOT NULL UNIQUE,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_amesh_routes_due
                ON amesh_routes(state, next_retry_ms);
            CREATE TABLE IF NOT EXISTS amesh_route_policy(
                destination TEXT NOT NULL,
                adapter TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                PRIMARY KEY(destination, adapter)
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self,
        destination: str,
        adapter: str,
        kind: str,
        signal: Mapping[str, Any],
    ) -> dict[str, Any]:
        destination = str(destination).strip()
        if not destination or len(destination) > 128:
            raise ValueError("route destination is invalid")
        adapter = str(adapter).strip().lower()
        if not adapter or len(adapter) > 64:
            raise ValueError("route adapter is invalid")
        kind = str(kind).strip()
        if not kind or len(kind) > 64:
            raise ValueError("route kind is invalid")
        if not isinstance(signal, Mapping):
            raise ValueError("route signal must be an object")
        signal_id = str(signal.get("signal_id", "")).strip().lower()
        if not _HEX32_RE.fullmatch(signal_id):
            raise ValueError("route signal requires a valid signal_id")
        encoded = canonical_pack(signal)
        if len(encoded) > MAX_SIGNAL_JSON_BYTES:
            raise ValueError("route signal is too large")

        if not self.destination_allowed(destination, adapter):
            raise ValueError(
                f"route policy denies destination {destination!r} for adapter {adapter}"
            )

        dedup_key = _dedup_key(destination, signal_id)
        created_ms = int(time.time() * 1000)
        route_id = secrets.token_hex(16)
        expires_ms = _positive_or_zero(signal.get("expires_ms", 0))
        try:
            self._conn.execute(
                """
                INSERT INTO amesh_routes(
                    route_id, destination, adapter, kind, signal_json, state,
                    attempts, next_retry_ms, expires_ms, dedup_key,
                    created_ms, updated_ms, last_error
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?, ?, '')
                """,
                (
                    route_id,
                    destination,
                    adapter,
                    kind,
                    encoded.decode("utf-8"),
                    expires_ms,
                    dedup_key,
                    created_ms,
                    created_ms,
                ),
            )
            return {
                "route_id": route_id,
                "duplicate": False,
                "state": "pending",
            }
        except sqlite3.IntegrityError:
            existing = self._route_by_dedup(dedup_key)
            if existing is None:  # pragma: no cover - inserted above
                raise RuntimeError("route dedup key disappeared")
            return {
                "route_id": existing["route_id"],
                "duplicate": True,
                "state": existing["state"],
            }

    def destination_allowed(self, destination: str, adapter: str) -> bool:
        row = self._conn.execute(
            "SELECT allowed FROM amesh_route_policy WHERE destination=? AND adapter=?",
            (str(destination).strip(), str(adapter).strip().lower()),
        ).fetchone()
        if row is not None:
            return bool(row["allowed"])
        return self.default_allow

    def set_policy(
        self, destination: str, adapter: str, allowed: bool
    ) -> dict[str, Any]:
        destination = str(destination).strip()
        if not destination or len(destination) > 128:
            raise ValueError("route destination is invalid")
        adapter = str(adapter).strip().lower()
        if not adapter or len(adapter) > 64:
            raise ValueError("route adapter is invalid")
        self._conn.execute(
            """
            INSERT INTO amesh_route_policy(destination, adapter, allowed)
            VALUES (?, ?, ?)
            ON CONFLICT(destination, adapter) DO UPDATE SET allowed=excluded.allowed
            """,
            (destination, adapter, 1 if allowed else 0),
        )
        return {"destination": destination, "adapter": adapter, "allowed": allowed}

    def policy_rules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT destination, adapter, allowed FROM amesh_route_policy ORDER BY destination, adapter"
        ).fetchall()
        return [
            {
                "destination": str(row["destination"]),
                "adapter": str(row["adapter"]),
                "allowed": bool(row["allowed"]),
            }
            for row in rows
        ]

    def due(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("route limit must be 1 to 10000")
        rows = self._conn.execute(
            """
            SELECT route_id FROM amesh_routes
            WHERE state IN ('pending', 'retrying') AND next_retry_ms <= ?
            ORDER BY next_retry_ms, created_ms LIMIT ?
            """,
            (now, int(limit)),
        ).fetchall()
        result = []
        for row in rows:
            route = self._route(str(row["route_id"]))
            if route is not None:
                result.append(route)
        return result

    def _route(self, route_id: str) -> dict[str, Any] | None:
        route_id = _route_id(route_id)
        row = self._conn.execute(
            "SELECT * FROM amesh_routes WHERE route_id=?",
            (route_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_route(row)

    def _route_by_dedup(self, dedup_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM amesh_routes WHERE dedup_key=?",
            (dedup_key,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_route(row)

    @staticmethod
    def _row_to_route(row: Any) -> dict[str, Any]:
        signal = json.loads(str(row["signal_json"]))
        return {
            "route_id": str(row["route_id"]),
            "destination": str(row["destination"]),
            "adapter": str(row["adapter"]),
            "kind": str(row["kind"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "next_retry_ms": int(row["next_retry_ms"]),
            "expires_ms": int(row["expires_ms"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "last_error": str(row["last_error"]),
            "signal": signal,
        }

    def attempt(self, route_id: str, *, ok: bool, error: str = "") -> dict[str, Any]:
        route = self._route(route_id)
        if route is None:
            raise ValueError(f"unknown route: {route_id}")
        now = int(time.time() * 1000)
        if ok:
            self._conn.execute(
                """
                UPDATE amesh_routes
                SET state='delivered', attempts=attempts + 1,
                    updated_ms=?, last_error=''
                WHERE route_id=?
                """,
                (now, route_id),
            )
            return self._require(route_id)
        attempts = route["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            state, next_retry = "failed", 0
        else:
            state = "retrying"
            next_retry = now + int(_retry_delay(attempts) * 1000)
        self._conn.execute(
            """
            UPDATE amesh_routes
            SET state=?, attempts=?, next_retry_ms=?, updated_ms=?, last_error=?
            WHERE route_id=?
            """,
            (state, attempts, next_retry, now, str(error)[:256], route_id),
        )
        return self._require(route_id)

    def _require(self, route_id: str) -> dict[str, Any]:
        route = self._route(route_id)
        if route is None:  # pragma: no cover - updated above
            raise RuntimeError("route disappeared")
        return route

    def deliver_due(
        self,
        deliver: Callable[[Mapping[str, Any]], Any],
        *,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> dict[str, int]:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        delivered = 0
        failed = 0
        expired = 0
        for route in self.due(now_ms=now, limit=limit):
            if route["expires_ms"] and now > route["expires_ms"]:
                self._conn.execute(
                    """
                    UPDATE amesh_routes SET state='expired', updated_ms=?
                    WHERE route_id=?
                    """,
                    (now, route["route_id"]),
                )
                expired += 1
                continue
            try:
                deliver(route["signal"])
            except Exception as exc:
                self.attempt(route["route_id"], ok=False, error=str(exc))
                failed += 1
                continue
            self.attempt(route["route_id"], ok=True)
            delivered += 1
        return {"delivered": delivered, "failed": failed, "expired": expired}

    def retry(self, route_id: str) -> dict[str, Any]:
        route = self._require(route_id)
        if route["state"] not in ("failed", "expired"):
            raise ValueError(f"route {route_id} is not retryable")
        now = int(time.time() * 1000)
        self._conn.execute(
            """
            UPDATE amesh_routes
            SET state='retrying', attempts=0, next_retry_ms=?, updated_ms=?, last_error=''
            WHERE route_id=?
            """,
            (now, now, route_id),
        )
        return self._require(route_id)

    def list(self, *, state: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("route limit must be 1 to 10000")
        if state and state not in ROUTE_STATES:
            raise ValueError("invalid route state")
        query = "SELECT route_id FROM amesh_routes"
        params: list[Any] = []
        if state:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY created_ms DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            route = self._route(str(row["route_id"]))
            if route is not None:
                result.append(route)
        return result

    def status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM amesh_routes GROUP BY state"
        ).fetchall()
        counts = {state: 0 for state in ROUTE_STATES}
        for row in rows:
            counts[str(row["state"])] = int(row["n"])
        return counts


def _dedup_key(destination: str, signal_id: str) -> str:
    return _hash(f"{destination}\0{signal_id}")


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _retry_delay(attempts: int) -> float:
    exponent = min(max(1, int(attempts)) - 1, 12)
    return min(BASE_RETRY_SECONDS * float(2**exponent), MAX_RETRY_SECONDS)


def _route_id(value: str) -> str:
    text = str(value).strip().lower()
    if not _ROUTE_ID_RE.fullmatch(text):
        raise ValueError("invalid route ID")
    return text


def _positive_or_zero(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, int(value))
