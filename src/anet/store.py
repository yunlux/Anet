from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .a2a_v1 import (
    ANET_STATE_BY_A2A,
    normalize_cancel_task_request,
    validate_a2a_stream_event,
)
from .agent_protocol import (
    TERMINAL_TASK_STATES,
    missing_task_capabilities,
    normalize_capability_policy,
    task_cancel,
    validate_task_message,
)
from .companion_protocol import (
    APPROVAL_DECISION_KIND,
    APPROVAL_REQUEST_KIND,
    validate_approval_decision_binding,
    validate_companion_message,
)
from .encoding import MAX_WIRE_BYTES, canonical_pack, pack, unpack
from .packet import (
    MAX_CLOCK_SKEW_MS,
    MAX_TTL_SECONDS,
    QOS_CLASSES,
    OpenedMessage,
    inspect_packet,
    now_ms,
)

if TYPE_CHECKING:
    from .control_plane import ControlPlaneStore


_CONSUMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_CLAIM_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_PREKEY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_A2A_STATE_TRANSITIONS = {
    "submitted": frozenset(
        {
            "submitted",
            "working",
            "input-required",
            "auth-required",
            "completed",
            "failed",
            "canceled",
            "rejected",
        }
    ),
    "working": frozenset(
        {
            "working",
            "input-required",
            "auth-required",
            "completed",
            "failed",
            "canceled",
            "rejected",
        }
    ),
    "input-required": frozenset(
        {
            "input-required",
            "auth-required",
            "working",
            "failed",
            "canceled",
            "rejected",
        }
    ),
    "auth-required": frozenset(
        {
            "auth-required",
            "input-required",
            "working",
            "failed",
            "canceled",
            "rejected",
        }
    ),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
    "rejected": frozenset(),
}


class PacketStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._harden_permissions()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._harden_permissions()

    def _harden_permissions(self) -> None:
        """Keep decrypted local state private on POSIX hosts."""
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

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS packets (
                    packet_id TEXT PRIMARY KEY,
                    destination_id TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    max_hops INTEGER NOT NULL,
                    depth INTEGER NOT NULL,
                    raw BLOB NOT NULL,
                    origin TEXT NOT NULL,
                    received_from TEXT NOT NULL DEFAULT '',
                    inserted_ms INTEGER NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_packets_pending
                    ON packets(expires_ms, delivered, depth);

                CREATE TABLE IF NOT EXISTS deliveries (
                    packet_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_ms INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(packet_id, peer_id)
                );

                CREATE TABLE IF NOT EXISTS inbox (
                    packet_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    received_ms INTEGER NOT NULL,
                    codec TEXT NOT NULL,
                    body BLOB NOT NULL,
                    causal BLOB NOT NULL,
                    reply_to TEXT NOT NULL DEFAULT '',
                    trusted INTEGER NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_received ON inbox(received_ms DESC);

                CREATE TABLE IF NOT EXISTS consumer_groups (
                    group_id TEXT PRIMARY KEY,
                    created_ms INTEGER NOT NULL,
                    start_after_rowid INTEGER NOT NULL DEFAULT 0,
                    kind_prefix TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    trusted_only INTEGER NOT NULL DEFAULT 1,
                    include_transient INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS consumer_deliveries (
                    group_id TEXT NOT NULL,
                    packet_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_until_ms INTEGER NOT NULL DEFAULT 0,
                    retry_after_ms INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    acked_ms INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(group_id, packet_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_consumer_claim_token
                    ON consumer_deliveries(claim_token) WHERE claim_token != '';
                CREATE INDEX IF NOT EXISTS idx_consumer_delivery_state
                    ON consumer_deliveries(group_id, state, lease_until_ms, retry_after_ms);

                CREATE TABLE IF NOT EXISTS agent_task_executions (
                    group_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    request_packet_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    execution_token TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    started_ms INTEGER NOT NULL DEFAULT 0,
                    updated_ms INTEGER NOT NULL DEFAULT 0,
                    completed_ms INTEGER NOT NULL DEFAULT 0,
                    output BLOB,
                    error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(group_id, sender_id, task_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_task_execution_token
                    ON agent_task_executions(execution_token)
                    WHERE execution_token != '';
                CREATE INDEX IF NOT EXISTS idx_agent_task_state
                    ON agent_task_executions(group_id, state, updated_ms);

                CREATE TABLE IF NOT EXISTS agent_task_cancellations (
                    group_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    cancel_packet_id TEXT NOT NULL,
                    cancel_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    state TEXT NOT NULL,
                    terminal_state TEXT NOT NULL DEFAULT '',
                    requested_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    applied_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(group_id, sender_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_task_cancel_state
                    ON agent_task_cancellations(group_id, state, updated_ms);

                CREATE TABLE IF NOT EXISTS companion_approval_requests (
                    request_id TEXT PRIMARY KEY,
                    human_id TEXT NOT NULL,
                    device_node_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    request_body BLOB NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    registered_ms INTEGER NOT NULL,
                    UNIQUE(human_id, device_node_id, nonce)
                );

                CREATE TABLE IF NOT EXISTS companion_approval_authorizations (
                    request_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL UNIQUE,
                    decision_packet_id TEXT NOT NULL,
                    decision_hash TEXT NOT NULL,
                    sender_node_id TEXT NOT NULL,
                    human_id TEXT NOT NULL,
                    device_node_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    state TEXT NOT NULL,
                    max_uses INTEGER NOT NULL,
                    grant_expires_ms INTEGER NOT NULL,
                    activated_ms INTEGER NOT NULL,
                    FOREIGN KEY(request_id)
                        REFERENCES companion_approval_requests(request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_approval_nonce
                    ON companion_approval_authorizations(
                        human_id, device_node_id, nonce
                    );

                CREATE TABLE IF NOT EXISTS companion_approval_effects (
                    request_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    effect_idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    execution_token TEXT NOT NULL DEFAULT '',
                    lease_until_ms INTEGER NOT NULL DEFAULT 0,
                    retry_after_ms INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    started_ms INTEGER NOT NULL DEFAULT 0,
                    updated_ms INTEGER NOT NULL DEFAULT 0,
                    completed_ms INTEGER NOT NULL DEFAULT 0,
                    result BLOB,
                    error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(request_id, effect_id),
                    FOREIGN KEY(request_id)
                        REFERENCES companion_approval_authorizations(request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_approval_execution_token
                    ON companion_approval_effects(execution_token)
                    WHERE execution_token != '';
                CREATE INDEX IF NOT EXISTS idx_companion_approval_effect_state
                    ON companion_approval_effects(
                        request_id, state, lease_until_ms, retry_after_ms
                    );

                CREATE TABLE IF NOT EXISTS a2a_gateway_principals (
                    principal_id TEXT PRIMARY KEY,
                    sender_node_id TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS a2a_gateway_tasks (
                    principal_id TEXT NOT NULL,
                    a2a_task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    destination_peer_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    tenant TEXT NOT NULL DEFAULT '',
                    protocol_version TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'submitted',
                    latest_anet_task_id TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    last_event_ms INTEGER NOT NULL DEFAULT 0,
                    cancel_state TEXT NOT NULL DEFAULT '',
                    cancel_requested_ms INTEGER NOT NULL DEFAULT 0,
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    PRIMARY KEY(principal_id, a2a_task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_a2a_gateway_context
                    ON a2a_gateway_tasks(principal_id, context_id, updated_ms);

                CREATE TABLE IF NOT EXISTS a2a_gateway_messages (
                    principal_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    a2a_task_id TEXT NOT NULL,
                    anet_task_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    request_body BLOB NOT NULL,
                    created_ms INTEGER NOT NULL,
                    PRIMARY KEY(principal_id, message_id),
                    UNIQUE(principal_id, anet_task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_a2a_gateway_task_messages
                    ON a2a_gateway_messages(
                        principal_id, a2a_task_id, created_ms
                    );

                CREATE TABLE IF NOT EXISTS a2a_gateway_events (
                    principal_id TEXT NOT NULL,
                    a2a_task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    source_anet_task_id TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    event BLOB NOT NULL,
                    created_ms INTEGER NOT NULL,
                    PRIMARY KEY(principal_id, a2a_task_id, sequence),
                    UNIQUE(principal_id, a2a_task_id, event_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_a2a_gateway_events_cursor
                    ON a2a_gateway_events(
                        principal_id, a2a_task_id, sequence
                    );

                CREATE TABLE IF NOT EXISTS a2a_gateway_dispatches (
                    principal_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    a2a_task_id TEXT NOT NULL,
                    anet_task_id TEXT NOT NULL,
                    destination_peer_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body BLOB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    owner TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    encryption_reservation_id TEXT NOT NULL,
                    lease_until_ms INTEGER NOT NULL DEFAULT 0,
                    retry_after_ms INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    packet_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    dispatched_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(principal_id, message_id),
                    UNIQUE(principal_id, anet_task_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_dispatch_claim
                    ON a2a_gateway_dispatches(claim_token)
                    WHERE claim_token != '';
                CREATE INDEX IF NOT EXISTS idx_a2a_dispatch_ready
                    ON a2a_gateway_dispatches(
                        state, retry_after_ms, lease_until_ms, created_ms
                    );

                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    integer_value INTEGER NOT NULL DEFAULT 0,
                    text_value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS local_prekeys (
                    prekey_id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL DEFAULT '',
                    bundle_version INTEGER NOT NULL DEFAULT 1,
                    public_key BLOB NOT NULL,
                    private_key BLOB,
                    generation INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'available',
                    consumed_ms INTEGER NOT NULL DEFAULT 0,
                    packet_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_local_prekeys_state
                    ON local_prekeys(state, expires_ms, generation);

                CREATE TABLE IF NOT EXISTS peer_prekey_bundles (
                    peer_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    bundle_version INTEGER NOT NULL DEFAULT 1,
                    intended_peer_id TEXT NOT NULL DEFAULT '',
                    bundle_hash TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    imported_ms INTEGER NOT NULL,
                    PRIMARY KEY(peer_id, generation)
                );

                CREATE TABLE IF NOT EXISTS peer_prekeys (
                    peer_id TEXT NOT NULL,
                    prekey_id TEXT NOT NULL,
                    bundle_version INTEGER NOT NULL DEFAULT 1,
                    public_key BLOB NOT NULL,
                    generation INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'available',
                    reservation_id TEXT NOT NULL DEFAULT '',
                    packet_id TEXT NOT NULL DEFAULT '',
                    used_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(peer_id, prekey_id)
                );
                CREATE INDEX IF NOT EXISTS idx_peer_prekeys_available
                    ON peer_prekeys(peer_id, state, expires_ms, generation);

                CREATE TABLE IF NOT EXISTS receipts (
                    packet_id TEXT PRIMARY KEY,
                    recipient_id TEXT NOT NULL,
                    delivered_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS packet_rejections (
                    packet_id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL DEFAULT '',
                    rejected_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS path_metrics (
                    peer_id TEXT NOT NULL,
                    path_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    consecutive_successes INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    ewma_rtt_ms REAL NOT NULL DEFAULT 0,
                    last_attempt_ms INTEGER NOT NULL DEFAULT 0,
                    last_ok_ms INTEGER NOT NULL DEFAULT 0,
                    last_failure_ms INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(peer_id, path_id)
                );

                CREATE TABLE IF NOT EXISTS route_state (
                    peer_id TEXT PRIMARY KEY,
                    selected_path TEXT NOT NULL,
                    switched_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS delivery_paths (
                    packet_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    path_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_ms INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(packet_id, peer_id, path_id)
                );
                """
            )
            packet_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(packets)").fetchall()
            }
            if "qos" not in packet_columns:
                self._conn.execute(
                    "ALTER TABLE packets ADD COLUMN qos TEXT NOT NULL DEFAULT 'normal'"
                )
            inbox_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(inbox)").fetchall()
            }
            if "qos" not in inbox_columns:
                self._conn.execute(
                    "ALTER TABLE inbox ADD COLUMN qos TEXT NOT NULL DEFAULT 'normal'"
                )
            if "visible" not in inbox_columns:
                self._conn.execute(
                    "ALTER TABLE inbox ADD COLUMN visible INTEGER NOT NULL DEFAULT 1"
                )
            self._conn.execute(
                "UPDATE inbox SET visible = 0 WHERE kind IN ('receipt', 'network.probe')"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packets_qos ON packets(delivered, qos, created_ms)"
            )
            local_prekey_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(local_prekeys)"
                ).fetchall()
            }
            if "peer_id" not in local_prekey_columns:
                self._conn.execute(
                    "ALTER TABLE local_prekeys ADD COLUMN peer_id TEXT NOT NULL DEFAULT ''"
                )
            if "bundle_version" not in local_prekey_columns:
                self._conn.execute(
                    "ALTER TABLE local_prekeys ADD COLUMN bundle_version INTEGER NOT NULL DEFAULT 1"
                )
            bundle_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(peer_prekey_bundles)"
                ).fetchall()
            }
            if "bundle_version" not in bundle_columns:
                self._conn.execute(
                    "ALTER TABLE peer_prekey_bundles ADD COLUMN bundle_version INTEGER NOT NULL DEFAULT 1"
                )
            if "intended_peer_id" not in bundle_columns:
                self._conn.execute(
                    "ALTER TABLE peer_prekey_bundles ADD COLUMN intended_peer_id TEXT NOT NULL DEFAULT ''"
                )
            peer_prekey_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(peer_prekeys)"
                ).fetchall()
            }
            if "bundle_version" not in peer_prekey_columns:
                self._conn.execute(
                    "ALTER TABLE peer_prekeys ADD COLUMN bundle_version INTEGER NOT NULL DEFAULT 1"
                )
            a2a_message_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(a2a_gateway_messages)"
                ).fetchall()
            }
            if "request_body" not in a2a_message_columns:
                self._conn.execute(
                    "ALTER TABLE a2a_gateway_messages ADD COLUMN request_body BLOB"
                )
            a2a_task_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(a2a_gateway_tasks)"
                ).fetchall()
            }
            a2a_task_migrations = {
                "tenant": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'submitted'",
                "last_sequence": "INTEGER NOT NULL DEFAULT 0",
                "last_event_ms": "INTEGER NOT NULL DEFAULT 0",
                "cancel_state": "TEXT NOT NULL DEFAULT ''",
                "cancel_requested_ms": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, declaration in a2a_task_migrations.items():
                if column not in a2a_task_columns:
                    self._conn.execute(
                        f"ALTER TABLE a2a_gateway_tasks ADD COLUMN {column} {declaration}"
                    )
            self._conn.execute("DROP INDEX IF EXISTS idx_local_prekeys_state")
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_prekeys_state
                ON local_prekeys(peer_id, state, expires_ms, generation)
                """
            )
            # Reconcile databases written by v0.1 before direct-destination
            # custody acknowledgements completed the packet globally.
            self._conn.execute(
                """
                UPDATE packets SET delivered = 1
                WHERE delivered = 0 AND EXISTS (
                    SELECT 1 FROM deliveries d
                    WHERE d.packet_id = packets.packet_id
                      AND d.peer_id = packets.destination_id
                      AND d.state = 'acked'
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_packet(
        self,
        raw: bytes,
        *,
        depth: int = 0,
        origin: str = "local",
        received_from: str = "",
    ) -> bool:
        info = inspect_packet(raw)
        depth = int(depth)
        if depth < 0 or depth > info.max_hops:
            raise ValueError("packet relay depth exceeds hop limit")
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO packets(
                    packet_id, destination_id, created_ms, expires_ms, max_hops,
                    depth, raw, origin, received_from, inserted_ms, delivered, qos
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    info.packet_id,
                    info.destination_id,
                    info.created_ms,
                    info.expires_ms,
                    info.max_hops,
                    depth,
                    sqlite3.Binary(raw),
                    str(origin),
                    str(received_from),
                    now_ms(),
                    info.qos,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                self._conn.execute(
                    "UPDATE packets SET depth = MIN(depth, ?) WHERE packet_id = ?",
                    (depth, info.packet_id),
                )
            return created

    def has_packet(self, packet_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM packets WHERE packet_id = ?",
                (str(packet_id),),
            ).fetchone()
        return row is not None

    def get_packet(self, packet_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM packets WHERE packet_id = ?",
                (str(packet_id),),
            ).fetchone()
        return bytes(row["raw"]) if row else None

    def pending_for_peer(
        self,
        peer_id: str,
        *,
        limit: int = 128,
        retry_after_ms: int = 1000,
        path_id: str | None = None,
        qos_allow: set[str]
        | frozenset[str]
        | tuple[str, ...]
        | list[str]
        | None = None,
    ) -> list[dict[str, Any]]:
        current = now_ms()
        qos_values: tuple[str, ...] | None = None
        if qos_allow is not None:
            qos_values = tuple(sorted({str(item) for item in qos_allow}))
            if not qos_values:
                return []
            if any(item not in QOS_CLASSES for item in qos_values):
                raise ValueError("invalid QoS filter")
        qos_clause = ""
        if qos_values is not None:
            qos_clause = f" AND p.qos IN ({','.join('?' for _ in qos_values)})"
        path_join = ""
        path_clause = ""
        params: list[Any] = [peer_id]
        if path_id is not None:
            path_join = """
                LEFT JOIN delivery_paths dp
                  ON dp.packet_id = p.packet_id
                 AND dp.peer_id = ?
                 AND dp.path_id = ?
            """
            path_clause = """
                  AND (
                    dp.state IS NULL
                    OR dp.state NOT IN ('custodied', 'acked')
                  )
            """
            params.extend([peer_id, str(path_id)])
        params.extend(
            [
                current,
                peer_id,
                current - max(0, int(retry_after_ms)),
            ]
        )
        if qos_values is not None:
            params.extend(qos_values)
        params.extend([peer_id, max(1, min(int(limit), 1024))])
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT p.packet_id, p.raw, p.depth, p.max_hops, p.destination_id, p.qos
                FROM packets p
                LEFT JOIN deliveries d
                  ON d.packet_id = p.packet_id AND d.peer_id = ?
                {path_join}
                WHERE p.expires_ms > ?
                  AND p.delivered = 0
                  AND p.depth < p.max_hops
                  AND p.received_from != ?
                  AND (d.state IS NULL OR d.state != 'acked')
                  AND (d.last_attempt_ms IS NULL OR d.last_attempt_ms <= ?)
                  {path_clause}
                  {qos_clause}
                ORDER BY
                  CASE WHEN p.destination_id = ? THEN 0 ELSE 1 END,
                  CASE p.qos
                    WHEN 'control' THEN 0
                    WHEN 'interactive' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                  END,
                  p.created_ms ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "packet_id": row["packet_id"],
                "raw": bytes(row["raw"]),
                "depth": int(row["depth"]),
                "max_hops": int(row["max_hops"]),
                "destination_id": row["destination_id"],
                "qos": row["qos"],
            }
            for row in rows
        ]

    def mark_attempt(
        self,
        packet_ids: list[str],
        peer_id: str,
        error: str = "",
        *,
        path_id: str = "unknown",
    ) -> None:
        if not packet_ids:
            return
        current = now_ms()
        with self._lock:
            for packet_id in packet_ids:
                self._conn.execute(
                    """
                    INSERT INTO deliveries(packet_id, peer_id, state, attempts, last_attempt_ms, last_error)
                    VALUES (?, ?, 'sent', 1, ?, ?)
                    ON CONFLICT(packet_id, peer_id) DO UPDATE SET
                        state = CASE WHEN deliveries.state = 'acked' THEN 'acked' ELSE 'sent' END,
                        attempts = deliveries.attempts + 1,
                        last_attempt_ms = excluded.last_attempt_ms,
                        last_error = excluded.last_error
                    """,
                    (str(packet_id), str(peer_id), current, str(error)[:1000]),
                )
                self._conn.execute(
                    """
                    INSERT INTO delivery_paths(
                        packet_id, peer_id, path_id, state, attempts, last_attempt_ms, last_error
                    ) VALUES (?, ?, ?, 'sent', 1, ?, ?)
                    ON CONFLICT(packet_id, peer_id, path_id) DO UPDATE SET
                        state = CASE
                            WHEN delivery_paths.state IN ('acked', 'custodied')
                            THEN delivery_paths.state
                            ELSE 'sent'
                        END,
                        attempts = delivery_paths.attempts + 1,
                        last_attempt_ms = excluded.last_attempt_ms,
                        last_error = excluded.last_error
                    """,
                    (
                        str(packet_id),
                        str(peer_id),
                        str(path_id),
                        current,
                        str(error)[:1000],
                    ),
                )

    def mark_custodied(
        self,
        packet_ids: list[str],
        peer_id: str,
        *,
        path_id: str,
    ) -> None:
        if not packet_ids:
            return
        current = now_ms()
        with self._lock:
            for packet_id in packet_ids:
                self._conn.execute(
                    """
                    INSERT INTO delivery_paths(
                        packet_id, peer_id, path_id, state, attempts,
                        last_attempt_ms, last_error
                    ) VALUES (?, ?, ?, 'custodied', 1, ?, '')
                    ON CONFLICT(packet_id, peer_id, path_id) DO UPDATE SET
                        state = CASE
                            WHEN delivery_paths.state = 'acked'
                            THEN 'acked'
                            ELSE 'custodied'
                        END,
                        last_attempt_ms = excluded.last_attempt_ms,
                        last_error = ''
                    """,
                    (
                        str(packet_id),
                        str(peer_id),
                        str(path_id),
                        current,
                    ),
                )

    def mark_acked(
        self,
        packet_ids: list[str],
        peer_id: str,
        *,
        path_id: str = "unknown",
    ) -> None:
        if not packet_ids:
            return
        current = now_ms()
        with self._lock:
            for packet_id in packet_ids:
                self._conn.execute(
                    """
                    INSERT INTO deliveries(packet_id, peer_id, state, attempts, last_attempt_ms, last_error)
                    VALUES (?, ?, 'acked', 1, ?, '')
                    ON CONFLICT(packet_id, peer_id) DO UPDATE SET
                        state = 'acked', last_attempt_ms = excluded.last_attempt_ms, last_error = ''
                    """,
                    (str(packet_id), str(peer_id), current),
                )
                self._conn.execute(
                    """
                    INSERT INTO delivery_paths(
                        packet_id, peer_id, path_id, state, attempts, last_attempt_ms, last_error
                    ) VALUES (?, ?, ?, 'acked', 1, ?, '')
                    ON CONFLICT(packet_id, peer_id, path_id) DO UPDATE SET
                        state = 'acked',
                        last_attempt_ms = excluded.last_attempt_ms,
                        last_error = ''
                    """,
                    (str(packet_id), str(peer_id), str(path_id), current),
                )
                # A custody acknowledgement from the packet's actual
                # destination completes delivery.  Relay acknowledgements
                # only suppress retransmission to that relay and must not
                # complete the packet globally.
                self._conn.execute(
                    """
                    UPDATE packets SET delivered = 1
                    WHERE packet_id = ? AND destination_id = ?
                    """,
                    (str(packet_id), str(peer_id)),
                )

    def add_inbox(
        self, message: OpenedMessage, *, trusted: bool, visible: bool = True
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO inbox(
                    packet_id, sender_id, kind, created_ms, received_ms, codec,
                    body, causal, reply_to, trusted, is_read, qos, visible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    message.packet_id,
                    message.sender_id,
                    message.kind,
                    message.created_ms,
                    now_ms(),
                    message.codec,
                    sqlite3.Binary(pack(message.body)),
                    sqlite3.Binary(pack(list(message.causal))),
                    message.reply_to,
                    1 if trusted else 0,
                    message.qos,
                    1 if visible else 0,
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _prekey_id(value: str) -> str:
        value = str(value).strip().lower()
        if not _PREKEY_ID_RE.fullmatch(value):
            raise ValueError("invalid prekey ID")
        return value

    @staticmethod
    def _prekey_peer_id(value: str) -> str:
        value = str(value).strip()
        if not value.startswith("an1") or len(value) < 20 or len(value) > 128:
            raise ValueError("invalid prekey peer ID")
        return value

    @staticmethod
    def _prekey_bytes(value: bytes, *, label: str) -> bytes:
        value = bytes(value)
        if len(value) != 32:
            raise ValueError(f"{label} must be 32 bytes")
        return value

    def add_local_prekey_batch(
        self,
        keys: list[dict[str, Any]],
        *,
        peer_id: str,
        bundle_version: int = 2,
        generation: int,
        created_ms: int,
        expires_ms: int,
    ) -> dict[str, Any]:
        peer_id = self._prekey_peer_id(peer_id)
        bundle_version = int(bundle_version)
        if bundle_version not in {1, 2}:
            raise ValueError("unsupported local prekey bundle version")
        generation = int(generation)
        created_ms = int(created_ms)
        expires_ms = int(expires_ms)
        if generation < 1:
            raise ValueError("prekey generation must be positive")
        if expires_ms <= created_ms:
            raise ValueError("prekey expiry must be after creation")
        if not keys or len(keys) > 1000:
            raise ValueError("prekey batch must contain 1 to 1000 keys")
        normalized: list[tuple[str, bytes, bytes]] = []
        seen: set[str] = set()
        for item in keys:
            prekey_id = self._prekey_id(str(item["prekey_id"]))
            if prekey_id in seen:
                raise ValueError("duplicate prekey ID in local batch")
            seen.add(prekey_id)
            public_key = self._prekey_bytes(
                bytes(item["public_key"]), label="prekey public key"
            )
            private_key = self._prekey_bytes(
                bytes(item["private_key"]), label="prekey private key"
            )
            normalized.append((prekey_id, public_key, private_key))

        inserted = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT integer_value FROM store_metadata WHERE key = ?",
                    (f"local_prekey_generation:{peer_id}",),
                ).fetchone()
                latest = int(row["integer_value"]) if row else 0
                if generation < latest:
                    raise ValueError("local prekey generation rollback")
                if generation > latest + 1:
                    raise ValueError("local prekey generation gap")
                if generation == latest and latest > 0:
                    existing_rows = self._conn.execute(
                        """
                        SELECT prekey_id, bundle_version, public_key,
                               created_ms, expires_ms
                        FROM local_prekeys WHERE peer_id = ? AND generation = ?
                        """,
                        (peer_id, generation),
                    ).fetchall()
                    existing = {
                        str(item["prekey_id"]): (
                            bytes(item["public_key"]),
                            int(item["bundle_version"]),
                            int(item["created_ms"]),
                            int(item["expires_ms"]),
                        )
                        for item in existing_rows
                    }
                    incoming = {
                        prekey_id: (
                            public_key,
                            bundle_version,
                            created_ms,
                            expires_ms,
                        )
                        for prekey_id, public_key, _ in normalized
                    }
                    if existing != incoming:
                        raise ValueError("local prekey generation equivocation")
                for prekey_id, public_key, private_key in normalized:
                    existing = self._conn.execute(
                        "SELECT * FROM local_prekeys WHERE prekey_id = ?",
                        (prekey_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            bytes(existing["public_key"]) != public_key
                            or str(existing["peer_id"]) != peer_id
                            or int(existing["bundle_version"]) != bundle_version
                            or int(existing["generation"]) != generation
                            or int(existing["created_ms"]) != created_ms
                            or int(existing["expires_ms"]) != expires_ms
                        ):
                            raise ValueError("local prekey ID collision")
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO local_prekeys(
                            prekey_id, peer_id, bundle_version, public_key,
                            private_key, generation,
                            created_ms, expires_ms, state, consumed_ms, packet_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'available', 0, '')
                        """,
                        (
                            prekey_id,
                            peer_id,
                            bundle_version,
                            sqlite3.Binary(public_key),
                            sqlite3.Binary(private_key),
                            generation,
                            created_ms,
                            expires_ms,
                        ),
                    )
                    inserted += 1
                self._conn.execute(
                    """
                    INSERT INTO store_metadata(key, integer_value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        integer_value = MAX(store_metadata.integer_value, excluded.integer_value)
                    """,
                    (f"local_prekey_generation:{peer_id}", generation),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {
            "peer_id": peer_id,
            "generation": generation,
            "inserted": inserted,
            "total": len(normalized),
        }

    def next_local_prekey_generation(self, peer_id: str) -> int:
        peer_id = self._prekey_peer_id(peer_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT integer_value FROM store_metadata WHERE key = ?",
                (f"local_prekey_generation:{peer_id}",),
            ).fetchone()
        return (int(row["integer_value"]) if row else 0) + 1

    def scope_legacy_local_prekeys(self, peer_id: str) -> dict[str, int | str]:
        """Bind unscoped v1 private keys to one explicitly selected peer.

        This migration is safe only when the operator/node has exactly one
        pinned peer. Callers enforce that invariant before invoking it.
        """
        peer_id = self._prekey_peer_id(peer_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT SUM(
                        CASE WHEN state = 'available' AND private_key IS NOT NULL
                             THEN 1 ELSE 0 END
                    ) AS n, COALESCE(MAX(generation), 0) AS generation
                    FROM local_prekeys WHERE peer_id = ''
                    """
                ).fetchone()
                count = int(row["n"] or 0)
                generation = int(row["generation"])
                if count:
                    self._conn.execute(
                        "UPDATE local_prekeys SET peer_id = ? WHERE peer_id = ''",
                        (peer_id,),
                    )
                    current = self._conn.execute(
                        """
                        SELECT COALESCE(MAX(generation), 0) AS generation
                        FROM local_prekeys WHERE peer_id = ?
                        """,
                        (peer_id,),
                    ).fetchone()
                    generation = int(current["generation"])
                    self._conn.execute(
                        """
                        INSERT INTO store_metadata(key, integer_value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            integer_value = MAX(
                                store_metadata.integer_value,
                                excluded.integer_value
                            )
                        """,
                        (f"local_prekey_generation:{peer_id}", generation),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {"peer_id": peer_id, "scoped": count, "generation": generation}

    def unscoped_local_prekey_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM local_prekeys
                WHERE peer_id = '' AND state = 'available'
                  AND private_key IS NOT NULL
                """
            ).fetchone()
        return int(row["n"])

    def retire_unscoped_local_prekeys(
        self, peer_ids: list[str] | tuple[str, ...]
    ) -> dict[str, Any]:
        peers = sorted({self._prekey_peer_id(item) for item in peer_ids})
        if not peers:
            raise ValueError("at least one peer is required for legacy retirement")
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT SUM(
                        CASE WHEN state = 'available' AND private_key IS NOT NULL
                             THEN 1 ELSE 0 END
                    ) AS n, COALESCE(MAX(generation), 0) AS generation
                    FROM local_prekeys WHERE peer_id = ''
                    """
                ).fetchone()
                count = int(row["n"] or 0)
                generation = int(row["generation"])
                if count:
                    self._conn.execute(
                        """
                        UPDATE local_prekeys
                        SET private_key = NULL, state = 'retired-unscoped',
                            consumed_ms = ?
                        WHERE peer_id = '' AND private_key IS NOT NULL
                        """,
                        (current,),
                    )
                for peer_id in peers:
                    self._conn.execute(
                        """
                        INSERT INTO store_metadata(key, integer_value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            integer_value = MAX(
                                store_metadata.integer_value,
                                excluded.integer_value
                            )
                        """,
                        (f"local_prekey_generation:{peer_id}", generation),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if count:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
        return {
            "retired": count,
            "legacy_generation": generation,
            "peer_ids": peers,
        }

    def local_prekey_public_batch(
        self,
        peer_id: str,
        generation: int | None = None,
        *,
        min_bundle_version: int = 2,
    ) -> dict[str, Any] | None:
        peer_id = self._prekey_peer_id(peer_id)
        min_bundle_version = int(min_bundle_version)
        if min_bundle_version not in {1, 2}:
            raise ValueError("invalid minimum prekey bundle version")
        with self._lock:
            if generation is None:
                generation_row = self._conn.execute(
                    """
                    SELECT MAX(generation) AS generation FROM local_prekeys
                    WHERE peer_id = ? AND bundle_version >= ?
                    """,
                    (peer_id, min_bundle_version),
                ).fetchone()
                if generation_row is None or generation_row["generation"] is None:
                    return None
                generation = int(generation_row["generation"])
            rows = self._conn.execute(
                """
                SELECT prekey_id, bundle_version, public_key, generation,
                       created_ms, expires_ms
                FROM local_prekeys
                WHERE peer_id = ? AND generation = ?
                  AND bundle_version >= ?
                ORDER BY rowid
                """,
                (peer_id, int(generation), min_bundle_version),
            ).fetchall()
        if not rows:
            return None
        return {
            "peer_id": peer_id,
            "generation": int(generation),
            "bundle_version": int(rows[0]["bundle_version"]),
            "created_ms": int(rows[0]["created_ms"]),
            "expires_ms": int(rows[0]["expires_ms"]),
            "keys": [
                {
                    "prekey_id": str(row["prekey_id"]),
                    "public_key": bytes(row["public_key"]),
                }
                for row in rows
            ],
        }

    def local_prekey_material(self, prekey_id: str) -> dict[str, Any] | None:
        prekey_id = self._prekey_id(prekey_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT prekey_id, peer_id, bundle_version, public_key,
                       private_key, generation, expires_ms
                FROM local_prekeys
                WHERE prekey_id = ? AND state = 'available'
                  AND private_key IS NOT NULL
                """,
                (prekey_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "prekey_id": row["prekey_id"],
            "peer_id": row["peer_id"],
            "bundle_version": int(row["bundle_version"]),
            "public_key": bytes(row["public_key"]),
            "private_key": bytes(row["private_key"]),
            "generation": int(row["generation"]),
            "expires_ms": int(row["expires_ms"]),
        }

    def packet_delivered(self, packet_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT delivered FROM packets WHERE packet_id = ?",
                (str(packet_id),),
            ).fetchone()
        return bool(row and row["delivered"])

    def commit_local_message(
        self,
        message: OpenedMessage,
        *,
        trusted: bool,
        visible: bool = True,
    ) -> bool:
        """Atomically persist a local message and erase its one-time private key."""
        current = now_ms()
        consumed_prekey = False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_inbox = self._conn.execute(
                    "SELECT 1 FROM inbox WHERE packet_id = ?",
                    (message.packet_id,),
                ).fetchone()
                if existing_inbox is not None:
                    self._conn.execute(
                        "UPDATE packets SET delivered = 1 WHERE packet_id = ?",
                        (message.packet_id,),
                    )
                    self._conn.execute("COMMIT")
                    return False

                if message.key_mode == "opk":
                    prekey_id = self._prekey_id(message.prekey_id)
                    row = self._conn.execute(
                        """
                        SELECT peer_id, state, private_key, expires_ms, packet_id
                        FROM local_prekeys WHERE prekey_id = ?
                        """,
                        (prekey_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("packet one-time prekey is unknown")
                    if (
                        row["state"] != "available"
                        or row["private_key"] is None
                    ):
                        raise ValueError("packet one-time prekey was already consumed")
                    if str(row["peer_id"]) != message.sender_id:
                        raise ValueError(
                            "packet sender is not authorized for this one-time prekey"
                        )
                elif message.key_mode != "static":
                    raise ValueError("invalid local packet key mode")

                cursor = self._conn.execute(
                    """
                    INSERT INTO inbox(
                        packet_id, sender_id, kind, created_ms, received_ms, codec,
                        body, causal, reply_to, trusted, is_read, qos, visible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        message.packet_id,
                        message.sender_id,
                        message.kind,
                        message.created_ms,
                        current,
                        message.codec,
                        sqlite3.Binary(pack(message.body)),
                        sqlite3.Binary(pack(list(message.causal))),
                        message.reply_to,
                        1 if trusted else 0,
                        message.qos,
                        1 if visible else 0,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to persist local message")
                self._conn.execute(
                    "UPDATE packets SET delivered = 1 WHERE packet_id = ?",
                    (message.packet_id,),
                )
                if message.key_mode == "opk":
                    cursor = self._conn.execute(
                        """
                        UPDATE local_prekeys
                        SET private_key = NULL, state = 'consumed',
                            consumed_ms = ?, packet_id = ?
                        WHERE prekey_id = ? AND state = 'available'
                          AND private_key IS NOT NULL AND peer_id = ?
                        """,
                        (
                            current,
                            message.packet_id,
                            message.prekey_id,
                            message.sender_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("packet one-time prekey consumption race")
                    consumed_prekey = True
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if consumed_prekey:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    # The logical deletion is committed. A busy reader can delay
                    # physical WAL truncation until a later checkpoint.
                    pass
        return True

    def import_peer_prekey_bundle(
        self,
        peer_id: str,
        keys: list[dict[str, Any]],
        *,
        bundle_version: int = 1,
        intended_peer_id: str = "",
        generation: int,
        bundle_hash: str,
        created_ms: int,
        expires_ms: int,
    ) -> dict[str, Any]:
        peer_id = str(peer_id).strip()
        bundle_version = int(bundle_version)
        intended_peer_id = str(intended_peer_id).strip()
        generation = int(generation)
        bundle_hash = str(bundle_hash).strip().lower()
        created_ms = int(created_ms)
        expires_ms = int(expires_ms)
        if not peer_id:
            raise ValueError("peer ID is required")
        if bundle_version not in {1, 2}:
            raise ValueError("unsupported prekey bundle version")
        if bundle_version >= 2:
            self._prekey_peer_id(intended_peer_id)
        if generation < 1:
            raise ValueError("prekey generation must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", bundle_hash):
            raise ValueError("invalid prekey bundle hash")
        if expires_ms <= created_ms or expires_ms <= now_ms():
            raise ValueError("prekey bundle is expired")
        if not keys or len(keys) > 1000:
            raise ValueError("prekey bundle must contain 1 to 1000 keys")
        normalized: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for item in keys:
            prekey_id = self._prekey_id(str(item["prekey_id"]))
            if prekey_id in seen:
                raise ValueError("duplicate prekey ID in peer bundle")
            seen.add(prekey_id)
            public_key = self._prekey_bytes(
                bytes(item["public_key"]), label="prekey public key"
            )
            normalized.append((prekey_id, public_key))

        inserted = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                latest_row = self._conn.execute(
                    """
                    SELECT generation, bundle_hash, bundle_version,
                           intended_peer_id
                    FROM peer_prekey_bundles
                    WHERE peer_id = ? ORDER BY generation DESC LIMIT 1
                    """,
                    (peer_id,),
                ).fetchone()
                if latest_row is not None:
                    latest = int(latest_row["generation"])
                    if generation < latest:
                        raise ValueError("peer prekey bundle generation rollback")
                    if generation == latest:
                        if (
                            str(latest_row["bundle_hash"]) != bundle_hash
                            or int(latest_row["bundle_version"]) != bundle_version
                            or str(latest_row["intended_peer_id"])
                            != intended_peer_id
                        ):
                            raise ValueError("peer prekey bundle equivocation")
                        self._conn.execute("COMMIT")
                        return {
                            "peer_id": peer_id,
                            "generation": generation,
                            "inserted": 0,
                            "total": len(normalized),
                            "duplicate": True,
                        }

                for prekey_id, public_key in normalized:
                    existing = self._conn.execute(
                        """
                        SELECT public_key, generation FROM peer_prekeys
                        WHERE peer_id = ? AND prekey_id = ?
                        """,
                        (peer_id, prekey_id),
                    ).fetchone()
                    if existing is not None:
                        raise ValueError("peer prekey ID reuse")
                    self._conn.execute(
                        """
                        INSERT INTO peer_prekeys(
                            peer_id, prekey_id, bundle_version, public_key,
                            generation, expires_ms,
                            state, reservation_id, packet_id, used_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, 'available', '', '', 0)
                        """,
                        (
                            peer_id,
                            prekey_id,
                            bundle_version,
                            sqlite3.Binary(public_key),
                            generation,
                            expires_ms,
                        ),
                    )
                    inserted += 1
                self._conn.execute(
                    """
                    INSERT INTO peer_prekey_bundles(
                        peer_id, generation, bundle_version, intended_peer_id,
                        bundle_hash, created_ms,
                        expires_ms, imported_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        peer_id,
                        generation,
                        bundle_version,
                        intended_peer_id,
                        bundle_hash,
                        created_ms,
                        expires_ms,
                        now_ms(),
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {
            "peer_id": peer_id,
            "bundle_version": bundle_version,
            "intended_peer_id": intended_peer_id,
            "generation": generation,
            "inserted": inserted,
            "total": len(normalized),
            "duplicate": False,
        }

    def reserve_peer_prekey(
        self,
        peer_id: str,
        *,
        reservation_id: str = "",
        min_bundle_version: int = 1,
    ) -> dict[str, Any] | None:
        peer_id = str(peer_id).strip()
        if not peer_id:
            raise ValueError("peer ID is required")
        min_bundle_version = int(min_bundle_version)
        if min_bundle_version not in {1, 2}:
            raise ValueError("invalid minimum prekey bundle version")
        reuse_reservation = bool(str(reservation_id).strip())
        reservation_id = (
            self._prekey_id(reservation_id)
            if reuse_reservation
            else secrets.token_hex(16)
        )
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if reuse_reservation:
                    reserved = self._conn.execute(
                        """
                        SELECT peer_id, prekey_id, bundle_version, public_key,
                               generation, expires_ms
                        FROM peer_prekeys
                        WHERE peer_id = ? AND state = 'reserved'
                          AND reservation_id = ? AND expires_ms > ?
                          AND bundle_version >= ?
                        """,
                        (
                            peer_id,
                            reservation_id,
                            current,
                            min_bundle_version,
                        ),
                    ).fetchone()
                    if reserved is not None:
                        self._conn.execute("COMMIT")
                        return {
                            "peer_id": peer_id,
                            "prekey_id": reserved["prekey_id"],
                            "bundle_version": int(reserved["bundle_version"]),
                            "public_key": bytes(reserved["public_key"]),
                            "generation": int(reserved["generation"]),
                            "expires_ms": int(reserved["expires_ms"]),
                            "reservation_id": reservation_id,
                        }
                row = self._conn.execute(
                    """
                    SELECT peer_id, prekey_id, bundle_version, public_key,
                           generation, expires_ms
                    FROM peer_prekeys
                    WHERE peer_id = ? AND state = 'available' AND expires_ms > ?
                      AND bundle_version >= ?
                    ORDER BY bundle_version DESC, generation ASC, rowid ASC LIMIT 1
                    """,
                    (peer_id, current, min_bundle_version),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                cursor = self._conn.execute(
                    """
                    UPDATE peer_prekeys
                    SET state = 'reserved', reservation_id = ?, used_ms = ?
                    WHERE peer_id = ? AND prekey_id = ? AND state = 'available'
                    """,
                    (reservation_id, current, peer_id, row["prekey_id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("prekey reservation race")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {
            "peer_id": peer_id,
            "prekey_id": row["prekey_id"],
            "bundle_version": int(row["bundle_version"]),
            "public_key": bytes(row["public_key"]),
            "generation": int(row["generation"]),
            "expires_ms": int(row["expires_ms"]),
            "reservation_id": reservation_id,
        }

    def bind_peer_prekey(
        self,
        peer_id: str,
        prekey_id: str,
        reservation_id: str,
        packet_id: str,
    ) -> None:
        peer_id = str(peer_id).strip()
        prekey_id = self._prekey_id(prekey_id)
        reservation_id = self._prekey_id(reservation_id)
        packet_id = str(packet_id).strip()
        if not packet_id:
            raise ValueError("packet ID is required")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE peer_prekeys
                SET state = 'used', packet_id = ?, used_ms = ?
                WHERE peer_id = ? AND prekey_id = ?
                  AND state = 'reserved' AND reservation_id = ?
                """,
                (packet_id, now_ms(), peer_id, prekey_id, reservation_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("prekey reservation is unavailable")

    def burn_peer_prekey(
        self, peer_id: str, prekey_id: str, reservation_id: str
    ) -> None:
        peer_id = str(peer_id).strip()
        prekey_id = self._prekey_id(prekey_id)
        reservation_id = self._prekey_id(reservation_id)
        with self._lock:
            self._conn.execute(
                """
                UPDATE peer_prekeys
                SET state = 'burned', used_ms = ?
                WHERE peer_id = ? AND prekey_id = ?
                  AND state = 'reserved' AND reservation_id = ?
                """,
                (now_ms(), peer_id, prekey_id, reservation_id),
            )

    def prekey_status(self, peer_id: str = "") -> dict[str, Any]:
        current = now_ms()
        peer_id = str(peer_id).strip()
        with self._lock:
            local_rows = self._conn.execute(
                """
                SELECT peer_id, bundle_version,
                    CASE
                        WHEN state = 'available' AND expires_ms <= ? THEN 'expired'
                        ELSE state
                    END AS effective_state,
                    COUNT(*) AS n
                FROM local_prekeys
                GROUP BY peer_id, bundle_version, effective_state
                ORDER BY peer_id, bundle_version, effective_state
                """,
                (current,),
            ).fetchall()
            local_generation_rows = self._conn.execute(
                """
                SELECT peer_id, MAX(generation) AS generation,
                       MAX(expires_ms) AS expires_ms
                FROM local_prekeys GROUP BY peer_id ORDER BY peer_id
                """
            ).fetchall()
            where = "WHERE peer_id = ?" if peer_id else ""
            params: tuple[Any, ...] = (peer_id,) if peer_id else ()
            peer_rows = self._conn.execute(
                f"""
                SELECT peer_id, bundle_version,
                    CASE
                        WHEN state = 'available' AND expires_ms <= {current} THEN 'expired'
                        ELSE state
                    END AS effective_state,
                    COUNT(*) AS n
                FROM peer_prekeys {where}
                GROUP BY peer_id, bundle_version, effective_state
                ORDER BY peer_id, bundle_version, effective_state
                """,
                params,
            ).fetchall()
            bundle_rows = self._conn.execute(
                f"""
                SELECT peer_id, MAX(generation) AS generation,
                       MAX(expires_ms) AS expires_ms,
                       MAX(bundle_version) AS bundle_version
                FROM peer_prekey_bundles {where}
                GROUP BY peer_id ORDER BY peer_id
                """,
                params,
            ).fetchall()
        local_counts: dict[str, int] = {}
        local_by_peer: dict[str, dict[str, Any]] = {}
        for row in local_rows:
            state = str(row["effective_state"])
            count = int(row["n"])
            local_counts[state] = local_counts.get(state, 0) + count
            scope = str(row["peer_id"])
            item = local_by_peer.setdefault(scope, {"counts": {}})
            item["counts"][state] = item["counts"].get(state, 0) + count
            by_version = item.setdefault("by_bundle_version", {})
            version_counts = by_version.setdefault(
                str(int(row["bundle_version"])), {}
            )
            version_counts[state] = count
        for row in local_generation_rows:
            scope = str(row["peer_id"])
            item = local_by_peer.setdefault(scope, {"counts": {}})
            item["generation"] = int(row["generation"])
            item["expires_ms"] = int(row["expires_ms"])
        peers: dict[str, dict[str, Any]] = {}
        for row in peer_rows:
            item = peers.setdefault(str(row["peer_id"]), {"counts": {}})
            state = str(row["effective_state"])
            count = int(row["n"])
            item["counts"][state] = item["counts"].get(state, 0) + count
            by_version = item.setdefault("by_bundle_version", {})
            version_counts = by_version.setdefault(
                str(int(row["bundle_version"])), {}
            )
            version_counts[state] = count
        for row in bundle_rows:
            item = peers.setdefault(str(row["peer_id"]), {"counts": {}})
            item["generation"] = int(row["generation"])
            item["expires_ms"] = int(row["expires_ms"])
            item["bundle_version"] = int(row["bundle_version"])
        return {
            "local": {
                "counts": local_counts,
                "by_peer": local_by_peer,
                "unscoped": sum(
                    int(value)
                    for value in local_by_peer.get("", {})
                    .get("counts", {})
                    .values()
                ),
            },
            "peers": peers,
        }

    def revoke_peer(self, peer_id: str) -> dict[str, int | str]:
        """Stop future work for a locally revoked peer and retire its key material."""
        peer_id = self._prekey_peer_id(peer_id)
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                pending = self._conn.execute(
                    """
                    UPDATE packets SET expires_ms = MIN(expires_ms, ?)
                    WHERE destination_id = ? AND delivered = 0 AND expires_ms > ?
                    """,
                    (current, peer_id, current),
                ).rowcount
                inbox = self._conn.execute(
                    "UPDATE inbox SET trusted = 0 WHERE sender_id = ? AND trusted = 1",
                    (peer_id,),
                ).rowcount
                claims = self._conn.execute(
                    """
                    UPDATE consumer_deliveries
                    SET state = 'revoked', claim_token = '', lease_until_ms = 0,
                        retry_after_ms = 0, last_error = 'peer trust revoked'
                    WHERE packet_id IN (SELECT packet_id FROM inbox WHERE sender_id = ?)
                      AND state NOT IN ('acked', 'revoked')
                    """,
                    (peer_id,),
                ).rowcount
                local_prekeys = self._conn.execute(
                    """
                    UPDATE local_prekeys
                    SET private_key = NULL, state = 'revoked', consumed_ms = ?
                    WHERE peer_id = ? AND state != 'revoked'
                    """,
                    (current, peer_id),
                ).rowcount
                peer_prekeys = self._conn.execute(
                    """
                    UPDATE peer_prekeys
                    SET state = 'revoked', reservation_id = '', used_ms = ?
                    WHERE peer_id = ? AND state != 'revoked'
                    """,
                    (current, peer_id),
                ).rowcount
                routes = self._conn.execute(
                    "DELETE FROM route_state WHERE peer_id = ?", (peer_id,)
                ).rowcount
                metrics = self._conn.execute(
                    "DELETE FROM path_metrics WHERE peer_id = ?", (peer_id,)
                ).rowcount
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if local_prekeys:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
        return {
            "peer_id": peer_id,
            "expired_pending_packets": pending,
            "reclassified_inbox": inbox,
            "revoked_consumer_deliveries": claims,
            "retired_local_prekeys": local_prekeys,
            "retired_peer_prekeys": peer_prekeys,
            "removed_routes": routes,
            "removed_path_metrics": metrics,
        }

    def peer_prekey_inventory(
        self, peer_id: str, *, min_bundle_version: int = 1
    ) -> dict[str, int]:
        peer_id = self._prekey_peer_id(peer_id)
        min_bundle_version = int(min_bundle_version)
        if min_bundle_version not in {1, 2}:
            raise ValueError("invalid minimum prekey bundle version")
        current = now_ms()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'available' AND expires_ms > ?
                             AND bundle_version >= ? THEN 1 ELSE 0 END) AS available,
                    COALESCE(MAX(CASE WHEN bundle_version >= ?
                                     THEN generation END), 0) AS generation
                FROM peer_prekeys WHERE peer_id = ?
                """,
                (current, min_bundle_version, min_bundle_version, peer_id),
            ).fetchone()
        return {
            "available": int(row["available"] or 0),
            "generation": int(row["generation"] or 0),
        }

    def prekey_request_state(self, peer_id: str) -> dict[str, int]:
        peer_id = self._prekey_peer_id(peer_id)
        keys = (
            f"prekey_request_ms:{peer_id}",
            f"prekey_request_generation:{peer_id}",
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, integer_value FROM store_metadata WHERE key IN (?, ?)",
                keys,
            ).fetchall()
        values = {str(row["key"]): int(row["integer_value"]) for row in rows}
        return {
            "last_requested_ms": values.get(keys[0], 0),
            "known_generation": values.get(keys[1], 0),
        }

    def record_prekey_request(
        self, peer_id: str, *, known_generation: int, requested_ms: int | None = None
    ) -> None:
        peer_id = self._prekey_peer_id(peer_id)
        requested_ms = now_ms() if requested_ms is None else int(requested_ms)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in (
                    (f"prekey_request_ms:{peer_id}", requested_ms),
                    (
                        f"prekey_request_generation:{peer_id}",
                        int(known_generation),
                    ),
                ):
                    self._conn.execute(
                        """
                        INSERT INTO store_metadata(key, integer_value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            integer_value = excluded.integer_value
                        """,
                        (key, value),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def list_inbox(
        self,
        *,
        limit: int = 50,
        unread_only: bool = False,
        include_untrusted: bool = True,
        include_transient: bool = False,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        if unread_only:
            conditions.append("is_read = 0")
        if not include_untrusted:
            conditions.append("trusted = 1")
        if not include_transient:
            conditions.append("visible = 1")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM inbox{where} ORDER BY received_ms DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._inbox_item(row) for row in rows]

    @staticmethod
    def _inbox_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "packet_id": row["packet_id"],
            "sender_id": row["sender_id"],
            "kind": row["kind"],
            "created_ms": int(row["created_ms"]),
            "received_ms": int(row["received_ms"]),
            "codec": row["codec"],
            "body": unpack(bytes(row["body"])),
            "causal": unpack(bytes(row["causal"])),
            "reply_to": row["reply_to"],
            "qos": row["qos"],
            "trusted": bool(row["trusted"]),
            "read": bool(row["is_read"]),
            "transient": not bool(row["visible"]),
        }

    @staticmethod
    def _consumer_id(value: str, *, label: str) -> str:
        value = str(value).strip()
        if not _CONSUMER_ID_RE.fullmatch(value):
            raise ValueError(f"invalid consumer {label}")
        return value

    @staticmethod
    def _claim_token(value: str) -> str:
        value = str(value).strip().lower()
        if not _CLAIM_TOKEN_RE.fullmatch(value):
            raise ValueError("invalid claim token")
        return value

    @staticmethod
    def _group_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "group_id": row["group_id"],
            "created_ms": int(row["created_ms"]),
            "start_after_rowid": int(row["start_after_rowid"]),
            "kind_prefix": row["kind_prefix"],
            "sender_id": row["sender_id"],
            "trusted_only": bool(row["trusted_only"]),
            "include_transient": bool(row["include_transient"]),
        }

    def open_consumer_group(
        self,
        group_id: str,
        *,
        start: str = "latest",
        kind_prefix: str = "",
        sender_id: str = "",
        trusted_only: bool = True,
        include_transient: bool = False,
    ) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        start = str(start).strip().lower()
        if start not in {"latest", "earliest"}:
            raise ValueError("consumer start must be latest or earliest")
        kind_prefix = str(kind_prefix).strip()
        sender_id = str(sender_id).strip()
        if len(kind_prefix) > 128 or len(sender_id) > 128:
            raise ValueError("consumer filter is too long")
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM consumer_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if existing is not None:
                item = self._group_item(existing)
                expected = {
                    "kind_prefix": kind_prefix,
                    "sender_id": sender_id,
                    "trusted_only": bool(trusted_only),
                    "include_transient": bool(include_transient),
                }
                if any(item[key] != value for key, value in expected.items()):
                    raise ValueError(
                        "consumer group already exists with different filters"
                    )
                item["created"] = False
                return item
            start_after = 0
            if start == "latest":
                start_after = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(rowid), 0) AS n FROM inbox"
                    ).fetchone()["n"]
                )
            created_ms = now_ms()
            self._conn.execute(
                """
                INSERT INTO consumer_groups(
                    group_id, created_ms, start_after_rowid, kind_prefix,
                    sender_id, trusted_only, include_transient
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    created_ms,
                    start_after,
                    kind_prefix,
                    sender_id,
                    1 if trusted_only else 0,
                    1 if include_transient else 0,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM consumer_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        item = self._group_item(row)
        item["created"] = True
        return item

    def _require_consumer_group(self, group_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM consumer_groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown consumer group: {group_id}")
        return row

    @staticmethod
    def _consumer_filters(
        group: sqlite3.Row, *, alias: str = "i"
    ) -> tuple[list[str], list[Any]]:
        conditions = [f"{alias}.rowid > ?"]
        params: list[Any] = [int(group["start_after_rowid"])]
        if bool(group["trusted_only"]):
            conditions.append(f"{alias}.trusted = 1")
        if not bool(group["include_transient"]):
            conditions.append(f"{alias}.visible = 1")
        if group["kind_prefix"]:
            conditions.append(f"substr({alias}.kind, 1, length(?)) = ?")
            params.extend([group["kind_prefix"], group["kind_prefix"]])
        if group["sender_id"]:
            conditions.append(f"{alias}.sender_id = ?")
            params.append(group["sender_id"])
        return conditions, params

    def claim_consumer_messages(
        self,
        group_id: str,
        owner: str,
        *,
        limit: int = 1,
        lease_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        limit = max(1, min(int(limit), 100))
        lease_ms = int(max(5.0, min(float(lease_seconds), 86400.0)) * 1000)
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                group = self._require_consumer_group(group_id)
                conditions, filter_params = self._consumer_filters(group)
                conditions.append(
                    """(
                        d.packet_id IS NULL
                        OR (d.state = 'leased' AND d.lease_until_ms <= ?)
                        OR (d.state = 'retry' AND d.retry_after_ms <= ?)
                    )"""
                )
                params: list[Any] = [group_id, *filter_params, current, current, limit]
                rows = self._conn.execute(
                    f"""
                    SELECT i.*, i.rowid AS inbox_rowid,
                           COALESCE(d.attempts, 0) AS prior_attempts
                    FROM inbox i
                    LEFT JOIN consumer_deliveries d
                      ON d.group_id = ? AND d.packet_id = i.packet_id
                    WHERE {" AND ".join(conditions)}
                    ORDER BY i.received_ms ASC, i.rowid ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                claims: list[dict[str, Any]] = []
                for row in rows:
                    token = secrets.token_hex(16)
                    lease_until = current + lease_ms
                    self._conn.execute(
                        """
                        INSERT INTO consumer_deliveries(
                            group_id, packet_id, state, owner, claim_token,
                            lease_until_ms, retry_after_ms, attempts, acked_ms, last_error
                        ) VALUES (?, ?, 'leased', ?, ?, ?, 0, 1, 0, '')
                        ON CONFLICT(group_id, packet_id) DO UPDATE SET
                            state = 'leased',
                            owner = excluded.owner,
                            claim_token = excluded.claim_token,
                            lease_until_ms = excluded.lease_until_ms,
                            retry_after_ms = 0,
                            attempts = consumer_deliveries.attempts + 1,
                            acked_ms = 0,
                            last_error = ''
                        """,
                        (group_id, row["packet_id"], owner, token, lease_until),
                    )
                    item = self._inbox_item(row)
                    item.update(
                        {
                            "consumer_group": group_id,
                            "claim_owner": owner,
                            "claim_token": token,
                            "lease_until_ms": lease_until,
                            "delivery_attempt": int(row["prior_attempts"]) + 1,
                            "content_security": (
                                "Authenticated sender does not make payload instructions safe; "
                                "apply local policy before tools or side effects."
                            ),
                        }
                    )
                    claims.append(item)
                self._conn.execute("COMMIT")
                return claims
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def acknowledge_claim(
        self, group_id: str, owner: str, claim_token: str
    ) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        current = now_ms()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE consumer_deliveries
                SET state = 'acked', owner = '', claim_token = '',
                    lease_until_ms = 0, retry_after_ms = 0, acked_ms = ?, last_error = ''
                WHERE group_id = ? AND owner = ? AND claim_token = ? AND state = 'leased'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_task_executions t
                    WHERE t.group_id = consumer_deliveries.group_id
                      AND t.claim_token = consumer_deliveries.claim_token
                      AND t.state IN ('working', 'canceling')
                  )
                """,
                (current, group_id, owner, claim_token),
            )
        if cursor.rowcount != 1:
            with self._lock:
                typed_task = self._conn.execute(
                    """
                    SELECT 1 FROM agent_task_executions
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND state IN ('working', 'canceling')
                    """,
                    (group_id, owner, claim_token),
                ).fetchone()
            if typed_task is not None:
                raise ValueError(
                    "typed Agent task claims must use settle_agent_task"
                )
            raise ValueError("claim is stale, unknown, or owned by another worker")
        return {"group_id": group_id, "state": "acked", "acked_ms": current}

    def reject_claim(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        *,
        retry_seconds: float = 0.0,
        error: str = "",
    ) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        retry_after = now_ms() + int(
            max(0.0, min(float(retry_seconds), 86400.0)) * 1000
        )
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE consumer_deliveries
                SET state = 'retry', owner = '', claim_token = '',
                    lease_until_ms = 0, retry_after_ms = ?, acked_ms = 0, last_error = ?
                WHERE group_id = ? AND owner = ? AND claim_token = ? AND state = 'leased'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_task_executions t
                    WHERE t.group_id = consumer_deliveries.group_id
                      AND t.claim_token = consumer_deliveries.claim_token
                      AND t.state IN ('working', 'canceling')
                  )
                """,
                (retry_after, str(error)[:1000], group_id, owner, claim_token),
            )
        if cursor.rowcount != 1:
            with self._lock:
                typed_task = self._conn.execute(
                    """
                    SELECT 1 FROM agent_task_executions
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND state IN ('working', 'canceling')
                    """,
                    (group_id, owner, claim_token),
                ).fetchone()
            if typed_task is not None:
                raise ValueError(
                    "typed Agent task claims must use settle_agent_task"
                )
            raise ValueError("claim is stale, unknown, or owned by another worker")
        return {"group_id": group_id, "state": "retry", "retry_after_ms": retry_after}

    def renew_claim(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        *,
        lease_seconds: float = 300.0,
    ) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        lease_until = now_ms() + int(
            max(5.0, min(float(lease_seconds), 86400.0)) * 1000
        )
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE consumer_deliveries SET lease_until_ms = ?
                WHERE group_id = ? AND owner = ? AND claim_token = ? AND state = 'leased'
                """,
                (lease_until, group_id, owner, claim_token),
            )
        if cursor.rowcount != 1:
            raise ValueError("claim is stale, unknown, or owned by another worker")
        return {"group_id": group_id, "state": "leased", "lease_until_ms": lease_until}

    def consumer_group_status(self, group_id: str) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        current = now_ms()
        with self._lock:
            group = self._require_consumer_group(group_id)
            conditions, params = self._consumer_filters(group)
            matching = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM inbox i WHERE {' AND '.join(conditions)}",
                    params,
                ).fetchone()["n"]
            )
            states = self._conn.execute(
                """
                SELECT state, COUNT(*) AS n FROM consumer_deliveries
                WHERE group_id = ? GROUP BY state
                """,
                (group_id,),
            ).fetchall()
            available_conditions = [
                *conditions,
                """(
                    d.packet_id IS NULL
                    OR (d.state = 'leased' AND d.lease_until_ms <= ?)
                    OR (d.state = 'retry' AND d.retry_after_ms <= ?)
                )""",
            ]
            available = int(
                self._conn.execute(
                    f"""
                    SELECT COUNT(*) AS n FROM inbox i
                    LEFT JOIN consumer_deliveries d
                      ON d.group_id = ? AND d.packet_id = i.packet_id
                    WHERE {" AND ".join(available_conditions)}
                    """,
                    [group_id, *params, current, current],
                ).fetchone()["n"]
            )
        item = self._group_item(group)
        item.update(
            {
                "matching": matching,
                "available": available,
                "states": {str(row["state"]): int(row["n"]) for row in states},
            }
        )
        return item

    @staticmethod
    def _companion_approval_id(value: str, *, label: str) -> str:
        normalized = str(value).strip().lower()
        if not _CLAIM_TOKEN_RE.fullmatch(normalized):
            raise ValueError(f"{label} must be 32 lowercase hexadecimal characters")
        return normalized

    @staticmethod
    def _companion_approval_hash(value: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_pack(value)).hexdigest()

    @staticmethod
    def _require_approval_grant(
        control_store: "ControlPlaneStore",
        *,
        human_id: str,
        device_node_id: str,
        current: int,
    ) -> Any:
        if control_store.is_human_device_revoked(human_id, device_node_id):
            raise PermissionError("approval device is revoked")
        try:
            grant = control_store.human_device_grant(
                human_id,
                device_node_id,
                now=current,
            )
        except ValueError as exc:
            raise PermissionError("approval device grant is not current") from exc
        if grant is None:
            raise PermissionError("approval device has no current grant")
        if "approval.sign" not in grant.capabilities:
            raise PermissionError("approval device grant lacks approval.sign")
        return grant

    def register_companion_approval_request(
        self,
        request_body: dict[str, Any],
        *,
        current_ms: int | None = None,
    ) -> dict[str, Any]:
        normalized = validate_companion_message(
            APPROVAL_REQUEST_KIND,
            request_body,
        )
        current = now_ms() if current_ms is None else int(current_ms)
        if normalized["created_ms"] > current + MAX_CLOCK_SKEW_MS:
            raise ValueError("approval request creation time is too far in the future")
        if normalized["expires_ms"] <= current:
            raise ValueError("approval request expired")
        request_hash = self._companion_approval_hash(normalized)
        encoded = pack(normalized)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT request_hash FROM companion_approval_requests
                    WHERE request_id = ?
                    """,
                    (normalized["request_id"],),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_hash"]) != request_hash:
                        raise ValueError("approval request ID conflicts with another body")
                    self._conn.execute("COMMIT")
                    return {
                        "request_id": normalized["request_id"],
                        "registered": False,
                        "request_hash": request_hash,
                    }
                try:
                    self._conn.execute(
                        """
                        INSERT INTO companion_approval_requests(
                            request_id, human_id, device_node_id, nonce,
                            request_hash, request_body, created_ms, expires_ms,
                            registered_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized["request_id"],
                            normalized["human_id"],
                            normalized["device_node_id"],
                            normalized["nonce"],
                            request_hash,
                            sqlite3.Binary(encoded),
                            normalized["created_ms"],
                            normalized["expires_ms"],
                            current,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        "approval nonce is already bound to another request"
                    ) from exc
                self._conn.execute("COMMIT")
                return {
                    "request_id": normalized["request_id"],
                    "registered": True,
                    "request_hash": request_hash,
                }
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def activate_companion_approval(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        control_store: "ControlPlaneStore",
        *,
        current_ms: int | None = None,
    ) -> dict[str, Any]:
        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        current = now_ms() if current_ms is None else int(current_ms)

        with self._lock:
            claim = self._conn.execute(
                """
                SELECT i.packet_id, i.sender_id, i.kind, i.body, i.trusted,
                       d.lease_until_ms
                FROM consumer_deliveries d
                JOIN inbox i ON i.packet_id = d.packet_id
                WHERE d.group_id = ? AND d.owner = ? AND d.claim_token = ?
                  AND d.state = 'leased'
                """,
                (group_id, owner, claim_token),
            ).fetchone()
        if claim is None or int(claim["lease_until_ms"]) <= current:
            raise ValueError("approval claim is stale, expired, or unknown")
        if not bool(claim["trusted"]):
            raise PermissionError("approval decision requires a trusted sender")
        if str(claim["kind"]) != APPROVAL_DECISION_KIND:
            raise ValueError("claim does not contain an ApprovalDecision")
        decision = validate_companion_message(
            APPROVAL_DECISION_KIND,
            unpack(bytes(claim["body"])),
        )
        sender_id = str(claim["sender_id"])
        if decision["device_node_id"] != sender_id:
            raise PermissionError("approval decision device does not match Packet sender")
        if decision["expires_ms"] <= current:
            raise ValueError("approval decision expired")

        with self._lock:
            request_row = self._conn.execute(
                """
                SELECT request_body, request_hash
                FROM companion_approval_requests WHERE request_id = ?
                """,
                (decision["request_id"],),
            ).fetchone()
        if request_row is None:
            raise PermissionError("approval decision has no local request")
        request = validate_companion_message(
            APPROVAL_REQUEST_KIND,
            unpack(bytes(request_row["request_body"])),
        )
        validate_approval_decision_binding(request, decision)
        if decision["scope"]["grant_expires_ms"] <= current:
            raise ValueError("approval grant scope expired")
        self._require_approval_grant(
            control_store,
            human_id=decision["human_id"],
            device_node_id=decision["device_node_id"],
            current=current,
        )

        decision_hash = self._companion_approval_hash(decision)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                live_claim = self._conn.execute(
                    """
                    SELECT i.packet_id, i.sender_id, i.body
                    FROM consumer_deliveries d
                    JOIN inbox i ON i.packet_id = d.packet_id
                    WHERE d.group_id = ? AND d.owner = ? AND d.claim_token = ?
                      AND d.state = 'leased' AND d.lease_until_ms > ?
                    """,
                    (group_id, owner, claim_token, current),
                ).fetchone()
                if live_claim is None:
                    raise ValueError("approval claim changed before activation")
                live_decision = validate_companion_message(
                    APPROVAL_DECISION_KIND,
                    unpack(bytes(live_claim["body"])),
                )
                if (
                    str(live_claim["sender_id"]) != sender_id
                    or self._companion_approval_hash(live_decision) != decision_hash
                ):
                    raise ValueError("approval claim changed before activation")
                live_request = self._conn.execute(
                    """
                    SELECT request_hash FROM companion_approval_requests
                    WHERE request_id = ?
                    """,
                    (decision["request_id"],),
                ).fetchone()
                if (
                    live_request is None
                    or str(live_request["request_hash"])
                    != str(request_row["request_hash"])
                ):
                    raise ValueError("approval request changed before activation")

                existing = self._conn.execute(
                    """
                    SELECT decision_hash, sender_node_id
                    FROM companion_approval_authorizations
                    WHERE request_id = ?
                    """,
                    (decision["request_id"],),
                ).fetchone()
                activated = existing is None
                if existing is not None:
                    if (
                        str(existing["decision_hash"]) != decision_hash
                        or str(existing["sender_node_id"]) != sender_id
                    ):
                        raise ValueError(
                            "approval request already has another decision"
                        )
                else:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO companion_approval_authorizations(
                                request_id, decision_id, decision_packet_id,
                                decision_hash, sender_node_id, human_id,
                                device_node_id, nonce, decision, state, max_uses,
                                grant_expires_ms, activated_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                decision["request_id"],
                                decision["decision_id"],
                                str(live_claim["packet_id"]),
                                decision_hash,
                                sender_id,
                                decision["human_id"],
                                decision["device_node_id"],
                                decision["nonce"],
                                decision["decision"],
                                (
                                    "active"
                                    if decision["decision"] == "approved"
                                    else "rejected"
                                ),
                                (
                                    decision["scope"]["max_uses"]
                                    if decision["decision"] == "approved"
                                    else 0
                                ),
                                decision["scope"]["grant_expires_ms"],
                                current,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(
                            "approval decision or nonce is already consumed"
                        ) from exc
                updated = self._conn.execute(
                    """
                    UPDATE consumer_deliveries
                    SET state = 'acked', owner = '', claim_token = '',
                        lease_until_ms = 0, retry_after_ms = 0,
                        acked_ms = ?, last_error = ''
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND state = 'leased'
                    """,
                    (current, group_id, owner, claim_token),
                )
                if updated.rowcount != 1:
                    raise ValueError("approval claim changed before acknowledgement")
                self._conn.execute("COMMIT")
                return {
                    "request_id": decision["request_id"],
                    "decision_id": decision["decision_id"],
                    "state": (
                        "active"
                        if decision["decision"] == "approved"
                        else "rejected"
                    ),
                    "activated": activated,
                    "max_uses": (
                        decision["scope"]["max_uses"]
                        if decision["decision"] == "approved"
                        else 0
                    ),
                    "grant_expires_ms": decision["scope"]["grant_expires_ms"],
                }
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _companion_effect_item(row: sqlite3.Row) -> dict[str, Any]:
        result = None
        if row["result"] is not None:
            result = unpack(bytes(row["result"]))
        return {
            "request_id": str(row["request_id"]),
            "effect_id": str(row["effect_id"]),
            "effect_idempotency_key": str(row["effect_idempotency_key"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "lease_until_ms": int(row["lease_until_ms"]),
            "retry_after_ms": int(row["retry_after_ms"]),
            "started_ms": int(row["started_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "completed_ms": int(row["completed_ms"]),
            "result": result,
            "error": str(row["error"]),
        }

    def begin_companion_approval_effect(
        self,
        request_id: str,
        effect_id: str,
        owner: str,
        control_store: "ControlPlaneStore",
        *,
        lease_seconds: float = 300.0,
        current_ms: int | None = None,
    ) -> dict[str, Any]:
        request_id = self._companion_approval_id(
            request_id,
            label="request_id",
        )
        effect_id = self._companion_approval_id(effect_id, label="effect_id")
        owner = self._consumer_id(owner, label="owner")
        current = now_ms() if current_ms is None else int(current_ms)
        lease_ms = int(max(5.0, min(float(lease_seconds), 86400.0)) * 1000)
        with self._lock:
            authorization = self._conn.execute(
                """
                SELECT * FROM companion_approval_authorizations
                WHERE request_id = ? AND state = 'active'
                """,
                (request_id,),
            ).fetchone()
        if authorization is None:
            raise PermissionError("approval request is not active")
        if int(authorization["grant_expires_ms"]) <= current:
            raise PermissionError("approval authorization expired")
        self._require_approval_grant(
            control_store,
            human_id=str(authorization["human_id"]),
            device_node_id=str(authorization["device_node_id"]),
            current=current,
        )

        effect_key = hashlib.sha256(
            canonical_pack(
                ["anet.companion.approval-effect.v1", request_id, effect_id]
            )
        ).hexdigest()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current_authorization = self._conn.execute(
                    """
                    SELECT max_uses, grant_expires_ms
                    FROM companion_approval_authorizations
                    WHERE request_id = ? AND state = 'active'
                    """,
                    (request_id,),
                ).fetchone()
                if (
                    current_authorization is None
                    or int(current_authorization["grant_expires_ms"]) <= current
                ):
                    raise PermissionError("approval authorization expired")
                existing = self._conn.execute(
                    """
                    SELECT * FROM companion_approval_effects
                    WHERE request_id = ? AND effect_id = ?
                    """,
                    (request_id, effect_id),
                ).fetchone()
                if existing is not None:
                    state = str(existing["state"])
                    if state == "executed" or state == "rejected":
                        self._conn.execute("COMMIT")
                        item = self._companion_effect_item(existing)
                        item["execution_token"] = ""
                        item["acquired"] = False
                        return item
                    if state == "working" and int(existing["lease_until_ms"]) > current:
                        if str(existing["owner"]) != owner:
                            raise ValueError("approval effect is leased by another worker")
                        self._conn.execute("COMMIT")
                        item = self._companion_effect_item(existing)
                        item["execution_token"] = str(existing["execution_token"])
                        item["acquired"] = False
                        return item
                    if state == "retry" and int(existing["retry_after_ms"]) > current:
                        raise ValueError("approval effect retry is not due")
                    execution_token = secrets.token_hex(16)
                    self._conn.execute(
                        """
                        UPDATE companion_approval_effects
                        SET state = 'working', owner = ?, execution_token = ?,
                            lease_until_ms = ?, retry_after_ms = 0,
                            attempts = attempts + 1, updated_ms = ?, error = ''
                        WHERE request_id = ? AND effect_id = ?
                        """,
                        (
                            owner,
                            execution_token,
                            current + lease_ms,
                            current,
                            request_id,
                            effect_id,
                        ),
                    )
                else:
                    reserved = int(
                        self._conn.execute(
                            """
                            SELECT COUNT(*) AS n
                            FROM companion_approval_effects
                            WHERE request_id = ?
                              AND state IN ('working', 'retry', 'executed')
                            """,
                            (request_id,),
                        ).fetchone()["n"]
                    )
                    if reserved >= int(current_authorization["max_uses"]):
                        raise PermissionError("approval use limit exhausted")
                    execution_token = secrets.token_hex(16)
                    self._conn.execute(
                        """
                        INSERT INTO companion_approval_effects(
                            request_id, effect_id, effect_idempotency_key,
                            state, owner, execution_token, lease_until_ms,
                            retry_after_ms, attempts, started_ms, updated_ms,
                            completed_ms, result, error
                        ) VALUES (?, ?, ?, 'working', ?, ?, ?, 0, 1, ?, ?, 0, NULL, '')
                        """,
                        (
                            request_id,
                            effect_id,
                            effect_key,
                            owner,
                            execution_token,
                            current + lease_ms,
                            current,
                            current,
                        ),
                    )
                row = self._conn.execute(
                    """
                    SELECT * FROM companion_approval_effects
                    WHERE request_id = ? AND effect_id = ?
                    """,
                    (request_id, effect_id),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._companion_effect_item(row)
                item["execution_token"] = execution_token
                item["acquired"] = True
                return item
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def settle_companion_approval_effect(
        self,
        request_id: str,
        effect_id: str,
        execution_token: str,
        *,
        outcome: str,
        result: Any = None,
        error: str = "",
        retry_seconds: float = 0.0,
        current_ms: int | None = None,
    ) -> dict[str, Any]:
        request_id = self._companion_approval_id(
            request_id,
            label="request_id",
        )
        effect_id = self._companion_approval_id(effect_id, label="effect_id")
        execution_token = self._claim_token(execution_token)
        outcome = str(outcome).strip().lower()
        if outcome not in {"executed", "retry", "rejected"}:
            raise ValueError("invalid approval effect outcome")
        error = str(error).strip()
        if outcome == "executed" and error:
            raise ValueError("executed approval effect cannot contain an error")
        if outcome == "rejected" and not error:
            raise ValueError("rejected approval effect requires an error")
        encoded_result = pack(result) if result is not None else None
        if encoded_result is not None and len(encoded_result) > MAX_WIRE_BYTES:
            raise ValueError("approval effect result is too large")
        current = now_ms() if current_ms is None else int(current_ms)
        retry_after = current + int(
            max(0.0, min(float(retry_seconds), 86400.0)) * 1000
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM companion_approval_effects
                    WHERE request_id = ? AND effect_id = ?
                      AND execution_token = ? AND state = 'working'
                    """,
                    (request_id, effect_id, execution_token),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "approval execution token is stale or fenced"
                    )
                if outcome == "executed":
                    self._conn.execute(
                        """
                        UPDATE companion_approval_effects
                        SET state = 'executed', owner = '', execution_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            updated_ms = ?, completed_ms = ?, result = ?, error = ''
                        WHERE request_id = ? AND effect_id = ?
                        """,
                        (
                            current,
                            current,
                            (
                                sqlite3.Binary(encoded_result)
                                if encoded_result is not None
                                else None
                            ),
                            request_id,
                            effect_id,
                        ),
                    )
                elif outcome == "retry":
                    self._conn.execute(
                        """
                        UPDATE companion_approval_effects
                        SET state = 'retry', owner = '', execution_token = '',
                            lease_until_ms = 0, retry_after_ms = ?,
                            updated_ms = ?, result = NULL, error = ?
                        WHERE request_id = ? AND effect_id = ?
                        """,
                        (
                            retry_after,
                            current,
                            error[:1000],
                            request_id,
                            effect_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE companion_approval_effects
                        SET state = 'rejected', owner = '', execution_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            updated_ms = ?, completed_ms = ?, result = NULL,
                            error = ?
                        WHERE request_id = ? AND effect_id = ?
                        """,
                        (
                            current,
                            current,
                            error[:1000],
                            request_id,
                            effect_id,
                        ),
                    )
                updated = self._conn.execute(
                    """
                    SELECT * FROM companion_approval_effects
                    WHERE request_id = ? AND effect_id = ?
                    """,
                    (request_id, effect_id),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._companion_effect_item(updated)
                item["execution_token"] = ""
                return item
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def companion_approval_effect(
        self,
        request_id: str,
        effect_id: str,
    ) -> dict[str, Any] | None:
        request_id = self._companion_approval_id(
            request_id,
            label="request_id",
        )
        effect_id = self._companion_approval_id(effect_id, label="effect_id")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM companion_approval_effects
                WHERE request_id = ? AND effect_id = ?
                """,
                (request_id, effect_id),
            ).fetchone()
        return self._companion_effect_item(row) if row is not None else None

    @staticmethod
    def _agent_task_item(row: sqlite3.Row) -> dict[str, Any]:
        output = None
        if row["output"] is not None:
            output = unpack(bytes(row["output"]))
        return {
            "group_id": str(row["group_id"]),
            "sender_id": str(row["sender_id"]),
            "task_id": str(row["task_id"]),
            "request_packet_id": str(row["request_packet_id"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "started_ms": int(row["started_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "completed_ms": int(row["completed_ms"]),
            "output": output,
            "error": str(row["error"]),
        }

    @staticmethod
    def _a2a_external_id(value: str, *, label: str) -> str:
        value = str(value).strip()
        if (
            not value
            or len(value) > 1024
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(f"invalid {label}")
        return value

    @staticmethod
    def _a2a_node_id(value: str, *, label: str) -> str:
        value = str(value).strip()
        if not value.startswith("an1") or len(value) < 20 or len(value) > 128:
            raise ValueError(f"invalid {label}")
        return value

    @staticmethod
    def _a2a_task_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "principal_id": str(row["principal_id"]),
            "sender_node_id": str(row["sender_node_id"]),
            "a2a_task_id": str(row["a2a_task_id"]),
            "context_id": str(row["context_id"]),
            "destination_peer_id": str(row["destination_peer_id"]),
            "skill_id": str(row["skill_id"]),
            "tenant": str(row["tenant"]),
            "protocol_version": str(row["protocol_version"]),
            "state": str(row["state"]),
            "latest_anet_task_id": str(row["latest_anet_task_id"]),
            "message_count": int(row["message_count"]),
            "last_sequence": int(row["last_sequence"]),
            "last_event_ms": int(row["last_event_ms"]),
            "cancel_state": str(row["cancel_state"]),
            "cancel_requested_ms": int(row["cancel_requested_ms"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
        }

    @staticmethod
    def _a2a_message_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "principal_id": str(row["principal_id"]),
            "message_id": str(row["message_id"]),
            "a2a_task_id": str(row["a2a_task_id"]),
            "anet_task_id": str(row["anet_task_id"]),
            "created_ms": int(row["created_ms"]),
        }

    @staticmethod
    def _a2a_dispatch_item(
        row: sqlite3.Row,
        *,
        include_body: bool = False,
        include_claim: bool = False,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "principal_id": str(row["principal_id"]),
            "message_id": str(row["message_id"]),
            "a2a_task_id": str(row["a2a_task_id"]),
            "anet_task_id": str(row["anet_task_id"]),
            "destination_peer_id": str(row["destination_peer_id"]),
            "kind": str(row["kind"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "packet_id": str(row["packet_id"]),
            "last_error": str(row["last_error"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "dispatched_ms": int(row["dispatched_ms"]),
        }
        if include_body:
            item["body"] = unpack(bytes(row["body"]))
        if include_claim:
            item.update(
                {
                    "owner": str(row["owner"]),
                    "claim_token": str(row["claim_token"]),
                    "encryption_reservation_id": str(
                        row["encryption_reservation_id"]
                    ),
                    "lease_until_ms": int(row["lease_until_ms"]),
                }
            )
        return item

    @staticmethod
    def _agent_task_cancellation_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "group_id": str(row["group_id"]),
            "sender_id": str(row["sender_id"]),
            "task_id": str(row["task_id"]),
            "reason": str(row["reason"]),
            "state": str(row["state"]),
            "terminal_state": str(row["terminal_state"]),
            "requested_ms": int(row["requested_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "applied_ms": int(row["applied_ms"]),
        }

    @staticmethod
    def _a2a_event_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "source_anet_task_id": str(row["source_anet_task_id"]),
            "event": unpack(bytes(row["event"])),
            "created_ms": int(row["created_ms"]),
        }

    def bind_a2a_principal(
        self,
        principal_id: str,
        sender_node_id: str,
        *,
        allowed_sender_nodes: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        """Persist one authenticated gateway principal to one Anet Node ID."""

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        sender_node_id = self._a2a_node_id(
            sender_node_id,
            label="A2A sender Node ID",
        )
        allowed = {
            self._a2a_node_id(value, label="allowed A2A sender Node ID")
            for value in allowed_sender_nodes
        }
        if sender_node_id not in allowed:
            raise PermissionError(
                "A2A principal sender is outside the local binding policy"
            )
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_principals
                    WHERE principal_id = ?
                    """,
                    (principal_id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing["sender_node_id"]) != sender_node_id
                ):
                    raise PermissionError(
                        "A2A principal is already bound to another Node ID"
                    )
                if existing is None:
                    self._conn.execute(
                        """
                        INSERT INTO a2a_gateway_principals(
                            principal_id, sender_node_id, created_ms, updated_ms
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (principal_id, sender_node_id, current, current),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE a2a_gateway_principals SET updated_ms = ?
                        WHERE principal_id = ?
                        """,
                        (current, principal_id),
                    )
                row = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_principals
                    WHERE principal_id = ?
                    """,
                    (principal_id,),
                ).fetchone()
                self._conn.execute("COMMIT")
                return {
                    "principal_id": str(row["principal_id"]),
                    "sender_node_id": str(row["sender_node_id"]),
                    "created_ms": int(row["created_ms"]),
                    "updated_ms": int(row["updated_ms"]),
                    "duplicate": existing is not None,
                }
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def a2a_principal(self, principal_id: str) -> dict[str, Any] | None:
        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM a2a_gateway_principals
                WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "principal_id": str(row["principal_id"]),
            "sender_node_id": str(row["sender_node_id"]),
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
        }

    def register_a2a_message(
        self,
        principal_id: str,
        *,
        authenticated_sender_id: str,
        a2a_task_id: str,
        context_id: str,
        message_id: str,
        destination_peer_id: str,
        skill_id: str,
        tenant: str = "",
        protocol_version: str,
        request_body: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically bind one A2A message and its Anet task request.

        The principal must already be bound to one authenticated sender Node ID.
        Repeating the same message is idempotent only when every mapping field
        and the canonical Anet request body match.
        """

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        a2a_task_id = self._a2a_external_id(
            a2a_task_id,
            label="A2A task ID",
        )
        context_id = self._a2a_external_id(
            context_id,
            label="A2A context ID",
        )
        message_id = self._a2a_external_id(
            message_id,
            label="A2A message ID",
        )
        destination_peer_id = self._a2a_node_id(
            destination_peer_id,
            label="A2A destination peer ID",
        )
        skill_id = self._consumer_id(skill_id, label="A2A skill ID")
        tenant = str(tenant).strip()
        if len(tenant) > 1024 or any(ord(character) < 32 for character in tenant):
            raise ValueError("invalid A2A tenant")
        protocol_version = str(protocol_version).strip()
        if protocol_version != "1.0":
            raise ValueError("unsupported A2A gateway protocol version")

        normalized = validate_task_message("agent.task.request", request_body)
        anet_task_id = str(normalized["task_id"])
        a2a_context = normalized.get("context", {}).get("a2a")
        if not isinstance(a2a_context, dict):
            raise ValueError("Anet task request lacks A2A gateway context")
        expected_context = {
            "protocolVersion": protocol_version,
            "messageId": message_id,
            "contextId": context_id,
            "taskId": a2a_task_id,
            "skillId": skill_id,
            "tenant": tenant,
        }
        if any(a2a_context.get(key) != value for key, value in expected_context.items()):
            raise ValueError("Anet task request does not match A2A gateway mapping")
        request_hash = hashlib.blake2s(
            canonical_pack(normalized),
            digest_size=32,
            person=b"aneta2a1",
        ).hexdigest()
        encoded_request = pack(normalized)
        current = now_ms()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                principal = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_principals
                    WHERE principal_id = ?
                    """,
                    (principal_id,),
                ).fetchone()
                if principal is None:
                    raise PermissionError(
                        "A2A principal has no authenticated Node ID binding"
                    )
                if str(principal["sender_node_id"]) != authenticated_sender_id:
                    raise PermissionError(
                        "authenticated A2A sender does not match principal binding"
                    )

                existing_message = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_messages
                    WHERE principal_id = ? AND message_id = ?
                    """,
                    (principal_id, message_id),
                ).fetchone()
                if existing_message is not None:
                    if (
                        str(existing_message["a2a_task_id"]) != a2a_task_id
                        or str(existing_message["anet_task_id"]) != anet_task_id
                        or str(existing_message["request_hash"]) != request_hash
                    ):
                        raise ValueError(
                            "A2A messageId was reused with a different mapping or body"
                        )
                    task_row = self._conn.execute(
                        """
                        SELECT t.*, p.sender_node_id
                        FROM a2a_gateway_tasks t
                        JOIN a2a_gateway_principals p
                          ON p.principal_id = t.principal_id
                        WHERE t.principal_id = ? AND t.a2a_task_id = ?
                        """,
                        (principal_id, a2a_task_id),
                    ).fetchone()
                    if existing_message["request_body"] is None:
                        self._conn.execute(
                            """
                            UPDATE a2a_gateway_messages SET request_body = ?
                            WHERE principal_id = ? AND message_id = ?
                            """,
                            (
                                sqlite3.Binary(encoded_request),
                                principal_id,
                                message_id,
                            ),
                        )
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO a2a_gateway_dispatches(
                            principal_id, message_id, a2a_task_id, anet_task_id,
                            destination_peer_id, kind, body,
                            encryption_reservation_id, created_ms, updated_ms
                        ) VALUES (?, ?, ?, ?, ?, 'agent.task.request', ?, ?, ?, ?)
                        """,
                        (
                            principal_id,
                            message_id,
                            a2a_task_id,
                            anet_task_id,
                            str(task_row["destination_peer_id"]),
                            sqlite3.Binary(encoded_request),
                            secrets.token_hex(16),
                            current,
                            current,
                        ),
                    )
                    dispatch_row = self._conn.execute(
                        """
                        SELECT * FROM a2a_gateway_dispatches
                        WHERE principal_id = ? AND message_id = ?
                        """,
                        (principal_id, message_id),
                    ).fetchone()
                    self._conn.execute("COMMIT")
                    item = self._a2a_task_item(task_row)
                    item.update(
                        {
                            "message": self._a2a_message_item(existing_message),
                            "dispatch": self._a2a_dispatch_item(dispatch_row),
                            "duplicate": True,
                        }
                    )
                    return item

                task = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_tasks
                    WHERE principal_id = ? AND a2a_task_id = ?
                    """,
                    (principal_id, a2a_task_id),
                ).fetchone()
                if task is None:
                    if a2a_task_id != anet_task_id:
                        raise ValueError(
                            "new A2A task ID must match its first Anet task ID"
                        )
                    self._conn.execute(
                        """
                        INSERT INTO a2a_gateway_tasks(
                            principal_id, a2a_task_id, context_id,
                            destination_peer_id, skill_id, tenant, protocol_version,
                            latest_anet_task_id, message_count,
                            created_ms, updated_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            principal_id,
                            a2a_task_id,
                            context_id,
                            destination_peer_id,
                            skill_id,
                            tenant,
                            protocol_version,
                            anet_task_id,
                            current,
                            current,
                        ),
                    )
                else:
                    if str(task["state"]) in TERMINAL_TASK_STATES:
                        raise ValueError(
                            "terminal A2A task cannot accept a follow-up message"
                        )
                    if str(task["cancel_state"]):
                        raise ValueError(
                            "A2A task with a cancellation request cannot accept a follow-up"
                        )
                    expected = {
                        "context_id": context_id,
                        "destination_peer_id": destination_peer_id,
                        "skill_id": skill_id,
                        "tenant": tenant,
                        "protocol_version": protocol_version,
                    }
                    if any(str(task[key]) != value for key, value in expected.items()):
                        raise ValueError(
                            "A2A follow-up changed task context, peer, skill, or version"
                        )

                conflicting_task = self._conn.execute(
                    """
                    SELECT message_id FROM a2a_gateway_messages
                    WHERE principal_id = ? AND anet_task_id = ?
                    """,
                    (principal_id, anet_task_id),
                ).fetchone()
                if conflicting_task is not None:
                    raise ValueError(
                        "Anet task ID is already bound to another A2A message"
                    )
                self._conn.execute(
                    """
                    INSERT INTO a2a_gateway_messages(
                        principal_id, message_id, a2a_task_id,
                        anet_task_id, request_hash, request_body, created_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal_id,
                        message_id,
                        a2a_task_id,
                        anet_task_id,
                        request_hash,
                        sqlite3.Binary(encoded_request),
                        current,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO a2a_gateway_dispatches(
                        principal_id, message_id, a2a_task_id, anet_task_id,
                        destination_peer_id, kind, body,
                        encryption_reservation_id, created_ms, updated_ms
                    ) VALUES (?, ?, ?, ?, ?, 'agent.task.request', ?, ?, ?, ?)
                    """,
                    (
                        principal_id,
                        message_id,
                        a2a_task_id,
                        anet_task_id,
                        destination_peer_id,
                        sqlite3.Binary(encoded_request),
                        secrets.token_hex(16),
                        current,
                        current,
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE a2a_gateway_tasks
                    SET latest_anet_task_id = ?, message_count = message_count + 1,
                        updated_ms = ?
                    WHERE principal_id = ? AND a2a_task_id = ?
                    """,
                    (anet_task_id, current, principal_id, a2a_task_id),
                )
                task_row = self._conn.execute(
                    """
                    SELECT t.*, p.sender_node_id
                    FROM a2a_gateway_tasks t
                    JOIN a2a_gateway_principals p
                      ON p.principal_id = t.principal_id
                    WHERE t.principal_id = ? AND t.a2a_task_id = ?
                    """,
                    (principal_id, a2a_task_id),
                ).fetchone()
                message_row = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_messages
                    WHERE principal_id = ? AND message_id = ?
                    """,
                    (principal_id, message_id),
                ).fetchone()
                dispatch_row = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE principal_id = ? AND message_id = ?
                    """,
                    (principal_id, message_id),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._a2a_task_item(task_row)
                item.update(
                    {
                        "message": self._a2a_message_item(message_row),
                        "dispatch": self._a2a_dispatch_item(dispatch_row),
                        "duplicate": False,
                    }
                )
                return item
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def a2a_gateway_task(
        self,
        principal_id: str,
        a2a_task_id: str,
        *,
        authenticated_sender_id: str,
    ) -> dict[str, Any] | None:
        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        a2a_task_id = self._a2a_external_id(
            a2a_task_id,
            label="A2A task ID",
        )
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.*, p.sender_node_id
                FROM a2a_gateway_tasks t
                JOIN a2a_gateway_principals p
                  ON p.principal_id = t.principal_id
                WHERE t.principal_id = ? AND t.a2a_task_id = ?
                  AND p.sender_node_id = ?
                """,
                (principal_id, a2a_task_id, authenticated_sender_id),
            ).fetchone()
        return self._a2a_task_item(row) if row else None

    def a2a_gateway_message(
        self,
        principal_id: str,
        message_id: str,
        *,
        authenticated_sender_id: str,
    ) -> dict[str, Any] | None:
        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        message_id = self._a2a_external_id(
            message_id,
            label="A2A message ID",
        )
        with self._lock:
            row = self._conn.execute(
                """
                SELECT m.* FROM a2a_gateway_messages m
                JOIN a2a_gateway_principals p
                  ON p.principal_id = m.principal_id
                WHERE m.principal_id = ? AND m.message_id = ?
                  AND p.sender_node_id = ?
                """,
                (principal_id, message_id, authenticated_sender_id),
            ).fetchone()
        return self._a2a_message_item(row) if row else None

    def request_a2a_task_cancellation(
        self,
        principal_id: str,
        request: dict[str, Any],
        *,
        authenticated_sender_id: str,
    ) -> dict[str, Any]:
        """Atomically persist idempotent cancel intents for every internal task."""

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                raw_id = (
                    str(request.get("id", "")).strip()
                    if isinstance(request, dict)
                    else ""
                )
                a2a_task_id = self._a2a_external_id(
                    raw_id,
                    label="A2A task ID",
                )
                task_row = self._conn.execute(
                    """
                    SELECT t.*, p.sender_node_id
                    FROM a2a_gateway_tasks t
                    JOIN a2a_gateway_principals p
                      ON p.principal_id = t.principal_id
                    WHERE t.principal_id = ? AND t.a2a_task_id = ?
                    """,
                    (principal_id, a2a_task_id),
                ).fetchone()
                if task_row is None:
                    raise ValueError("unknown A2A task")
                task_item = self._a2a_task_item(task_row)
                normalize_cancel_task_request(
                    request,
                    authenticated_sender_id=authenticated_sender_id,
                    existing_task=task_item,
                    expected_tenant=str(task_row["tenant"]),
                )
                if str(task_row["sender_node_id"]) != authenticated_sender_id:
                    raise PermissionError(
                        "authenticated A2A sender does not own the task"
                    )
                if str(task_row["cancel_state"]):
                    rows = self._conn.execute(
                        """
                        SELECT * FROM a2a_gateway_dispatches
                        WHERE principal_id = ? AND a2a_task_id = ?
                          AND kind = 'agent.task.cancel'
                        ORDER BY created_ms, rowid
                        """,
                        (principal_id, a2a_task_id),
                    ).fetchall()
                    self._conn.execute("COMMIT")
                    task_item["duplicate"] = True
                    task_item["cancel_dispatches"] = [
                        self._a2a_dispatch_item(row) for row in rows
                    ]
                    return task_item

                targets = self._conn.execute(
                    """
                    SELECT anet_task_id FROM a2a_gateway_messages
                    WHERE principal_id = ? AND a2a_task_id = ?
                    ORDER BY created_ms, rowid
                    """,
                    (principal_id, a2a_task_id),
                ).fetchall()
                if not targets:
                    raise RuntimeError("A2A task has no internal task mapping")
                reason = "A2A task cancellation requested"
                for target in targets:
                    target_task_id = str(target["anet_task_id"])
                    cancel_id = hashlib.blake2s(
                        canonical_pack(
                            [principal_id, a2a_task_id, target_task_id]
                        ),
                        digest_size=16,
                        person=b"a2acxl1",
                    ).hexdigest()
                    cancel_message_id = f"cancel-{cancel_id}"
                    body = task_cancel(
                        task_id=target_task_id,
                        reason=reason,
                    )
                    self._conn.execute(
                        """
                        INSERT INTO a2a_gateway_dispatches(
                            principal_id, message_id, a2a_task_id, anet_task_id,
                            destination_peer_id, kind, body,
                            encryption_reservation_id, created_ms, updated_ms
                        ) VALUES (?, ?, ?, ?, ?, 'agent.task.cancel', ?, ?, ?, ?)
                        """,
                        (
                            principal_id,
                            cancel_message_id,
                            a2a_task_id,
                            cancel_id,
                            str(task_row["destination_peer_id"]),
                            sqlite3.Binary(pack(body)),
                            secrets.token_hex(16),
                            current,
                            current,
                        ),
                    )
                self._conn.execute(
                    """
                    UPDATE a2a_gateway_tasks
                    SET cancel_state = 'requested', cancel_requested_ms = ?,
                        updated_ms = ?
                    WHERE principal_id = ? AND a2a_task_id = ?
                    """,
                    (current, current, principal_id, a2a_task_id),
                )
                updated_task = self._conn.execute(
                    """
                    SELECT t.*, p.sender_node_id
                    FROM a2a_gateway_tasks t
                    JOIN a2a_gateway_principals p
                      ON p.principal_id = t.principal_id
                    WHERE t.principal_id = ? AND t.a2a_task_id = ?
                    """,
                    (principal_id, a2a_task_id),
                ).fetchone()
                rows = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE principal_id = ? AND a2a_task_id = ?
                      AND kind = 'agent.task.cancel'
                    ORDER BY created_ms, rowid
                    """,
                    (principal_id, a2a_task_id),
                ).fetchall()
                self._conn.execute("COMMIT")
                item = self._a2a_task_item(updated_task)
                item["duplicate"] = False
                item["cancel_dispatches"] = [
                    self._a2a_dispatch_item(row) for row in rows
                ]
                return item
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def a2a_gateway_dispatch(
        self,
        principal_id: str,
        message_id: str,
        *,
        authenticated_sender_id: str,
    ) -> dict[str, Any] | None:
        """Read non-secret dispatch state within the authenticated principal."""

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        message_id = self._a2a_external_id(
            message_id,
            label="A2A message ID",
        )
        with self._lock:
            row = self._conn.execute(
                """
                SELECT d.* FROM a2a_gateway_dispatches d
                JOIN a2a_gateway_principals p
                  ON p.principal_id = d.principal_id
                WHERE d.principal_id = ? AND d.message_id = ?
                  AND p.sender_node_id = ?
                """,
                (principal_id, message_id, authenticated_sender_id),
            ).fetchone()
        return self._a2a_dispatch_item(row) if row else None

    def claim_a2a_dispatches(
        self,
        owner: str,
        *,
        limit: int = 16,
        lease_seconds: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Lease durable gateway intents for local packet construction."""

        owner = self._consumer_id(owner, label="A2A dispatch owner")
        limit = max(1, min(int(limit), 128))
        lease_ms = int(max(5.0, min(float(lease_seconds), 3600.0)) * 1000)
        current = now_ms()
        claimed: list[dict[str, Any]] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE (
                        state IN ('pending', 'retry') AND retry_after_ms <= ?
                    ) OR (
                        state = 'leased' AND lease_until_ms <= ?
                    )
                    ORDER BY created_ms, rowid
                    LIMIT ?
                    """,
                    (current, current, limit),
                ).fetchall()
                for row in rows:
                    token = secrets.token_hex(16)
                    lease_until = current + lease_ms
                    cursor = self._conn.execute(
                        """
                        UPDATE a2a_gateway_dispatches
                        SET state = 'leased', owner = ?, claim_token = ?,
                            lease_until_ms = ?, retry_after_ms = 0,
                            attempts = attempts + 1, updated_ms = ?,
                            last_error = ''
                        WHERE principal_id = ? AND message_id = ?
                          AND (
                            state IN ('pending', 'retry')
                            OR (state = 'leased' AND lease_until_ms <= ?)
                          )
                        """,
                        (
                            owner,
                            token,
                            lease_until,
                            current,
                            str(row["principal_id"]),
                            str(row["message_id"]),
                            current,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("A2A dispatch claim race")
                    claimed_row = self._conn.execute(
                        """
                        SELECT * FROM a2a_gateway_dispatches
                        WHERE principal_id = ? AND message_id = ?
                        """,
                        (str(row["principal_id"]), str(row["message_id"])),
                    ).fetchone()
                    claimed.append(
                        self._a2a_dispatch_item(
                            claimed_row,
                            include_body=True,
                            include_claim=True,
                        )
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return claimed

    def retry_a2a_dispatch(
        self,
        owner: str,
        claim_token: str,
        *,
        error: str,
        retry_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Release a failed claim and rotate its prekey reservation identity."""

        owner = self._consumer_id(owner, label="A2A dispatch owner")
        claim_token = self._claim_token(claim_token)
        error = str(error).strip()
        if not error:
            raise ValueError("A2A dispatch retry requires an error")
        current = now_ms()
        retry_after = current + int(
            max(0.0, min(float(retry_seconds), 86400.0)) * 1000
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE owner = ? AND claim_token = ? AND state = 'leased'
                    """,
                    (owner, claim_token),
                ).fetchone()
                if claimed is None:
                    raise ValueError(
                        "A2A dispatch claim is stale, unknown, or owned by another worker"
                    )
                self._conn.execute(
                    """
                    UPDATE peer_prekeys
                    SET state = 'burned', used_ms = ?
                    WHERE peer_id = ? AND state = 'reserved'
                      AND reservation_id = ?
                    """,
                    (
                        current,
                        str(claimed["destination_peer_id"]),
                        str(claimed["encryption_reservation_id"]),
                    ),
                )
                cursor = self._conn.execute(
                    """
                    UPDATE a2a_gateway_dispatches
                    SET state = 'retry', owner = '', claim_token = '',
                        encryption_reservation_id = ?, lease_until_ms = 0,
                        retry_after_ms = ?, updated_ms = ?, last_error = ?
                    WHERE owner = ? AND claim_token = ? AND state = 'leased'
                    """,
                    (
                        secrets.token_hex(16),
                        retry_after,
                        current,
                        error[:1000],
                        owner,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to release A2A dispatch claim")
                row = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE principal_id = ? AND message_id = ?
                    """,
                    (
                        str(claimed["principal_id"]),
                        str(claimed["message_id"]),
                    ),
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._a2a_dispatch_item(row)

    def commit_a2a_dispatch_packet(
        self,
        owner: str,
        claim_token: str,
        raw: bytes,
        *,
        prekey_id: str = "",
        prekey_reservation_id: str = "",
    ) -> dict[str, Any]:
        """Atomically persist a sealed Packet and settle its dispatch claim."""

        owner = self._consumer_id(owner, label="A2A dispatch owner")
        claim_token = self._claim_token(claim_token)
        raw = bytes(raw)
        info = inspect_packet(raw)
        prekey_id = str(prekey_id).strip()
        prekey_reservation_id = str(prekey_reservation_id).strip()
        if info.key_mode == "opk":
            prekey_id = self._prekey_id(prekey_id)
            prekey_reservation_id = self._prekey_id(prekey_reservation_id)
            if info.prekey_id != prekey_id:
                raise ValueError("A2A dispatch Packet prekey does not match claim")
        elif prekey_id or prekey_reservation_id:
            raise ValueError("static A2A dispatch Packet cannot bind a prekey")

        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                dispatch = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE owner = ? AND claim_token = ? AND state = 'leased'
                      AND lease_until_ms > ?
                    """,
                    (owner, claim_token, current),
                ).fetchone()
                if dispatch is None:
                    raise ValueError(
                        "A2A dispatch claim is stale, expired, or owned by another worker"
                    )
                if str(dispatch["destination_peer_id"]) != info.destination_id:
                    raise ValueError(
                        "A2A dispatch Packet destination does not match intent"
                    )
                if info.key_mode == "opk":
                    if (
                        str(dispatch["encryption_reservation_id"])
                        != prekey_reservation_id
                    ):
                        raise ValueError(
                            "A2A dispatch prekey reservation does not match intent"
                        )
                    cursor = self._conn.execute(
                        """
                        UPDATE peer_prekeys
                        SET state = 'used', packet_id = ?, used_ms = ?
                        WHERE peer_id = ? AND prekey_id = ?
                          AND state = 'reserved' AND reservation_id = ?
                        """,
                        (
                            info.packet_id,
                            current,
                            info.destination_id,
                            prekey_id,
                            prekey_reservation_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("prekey reservation is unavailable")

                cursor = self._conn.execute(
                    """
                    INSERT INTO packets(
                        packet_id, destination_id, created_ms, expires_ms,
                        max_hops, depth, raw, origin, received_from,
                        inserted_ms, delivered, qos
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, 'a2a-gateway', '', ?, 0, ?)
                    """,
                    (
                        info.packet_id,
                        info.destination_id,
                        info.created_ms,
                        info.expires_ms,
                        info.max_hops,
                        sqlite3.Binary(raw),
                        current,
                        info.qos,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to persist A2A dispatch Packet")
                cursor = self._conn.execute(
                    """
                    UPDATE a2a_gateway_dispatches
                    SET state = 'dispatched', owner = '', claim_token = '',
                        lease_until_ms = 0, retry_after_ms = 0,
                        packet_id = ?, last_error = '', updated_ms = ?,
                        dispatched_ms = ?
                    WHERE principal_id = ? AND message_id = ?
                      AND owner = ? AND claim_token = ? AND state = 'leased'
                    """,
                    (
                        info.packet_id,
                        current,
                        current,
                        str(dispatch["principal_id"]),
                        str(dispatch["message_id"]),
                        owner,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to settle A2A dispatch claim")
                if str(dispatch["kind"]) == "agent.task.cancel":
                    remaining = int(
                        self._conn.execute(
                            """
                            SELECT COUNT(*) AS n FROM a2a_gateway_dispatches
                            WHERE principal_id = ? AND a2a_task_id = ?
                              AND kind = 'agent.task.cancel'
                              AND state != 'dispatched'
                            """,
                            (
                                str(dispatch["principal_id"]),
                                str(dispatch["a2a_task_id"]),
                            ),
                        ).fetchone()["n"]
                    )
                    if remaining == 0:
                        self._conn.execute(
                            """
                            UPDATE a2a_gateway_tasks
                            SET cancel_state = 'dispatched', updated_ms = ?
                            WHERE principal_id = ? AND a2a_task_id = ?
                              AND cancel_state = 'requested'
                            """,
                            (
                                current,
                                str(dispatch["principal_id"]),
                                str(dispatch["a2a_task_id"]),
                            ),
                        )
                row = self._conn.execute(
                    """
                    SELECT * FROM a2a_gateway_dispatches
                    WHERE principal_id = ? AND message_id = ?
                    """,
                    (
                        str(dispatch["principal_id"]),
                        str(dispatch["message_id"]),
                    ),
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._a2a_dispatch_item(row)

    def append_a2a_task_events(
        self,
        principal_id: str,
        *,
        authenticated_sender_id: str,
        a2a_task_id: str,
        source_anet_task_id: str,
        events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        """Append idempotent A2A stream events and advance aggregate state."""

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        a2a_task_id = self._a2a_external_id(
            a2a_task_id,
            label="A2A task ID",
        )
        source_anet_task_id = self._a2a_external_id(
            source_anet_task_id,
            label="source Anet task ID",
        )
        if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= 64:
            raise ValueError("A2A event append requires a bounded non-empty batch")
        current = now_ms()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                task = self._conn.execute(
                    """
                    SELECT t.*, p.sender_node_id
                    FROM a2a_gateway_tasks t
                    JOIN a2a_gateway_principals p
                      ON p.principal_id = t.principal_id
                    WHERE t.principal_id = ? AND t.a2a_task_id = ?
                    """,
                    (principal_id, a2a_task_id),
                ).fetchone()
                if task is None:
                    raise ValueError("unknown A2A task")
                if str(task["sender_node_id"]) != authenticated_sender_id:
                    raise PermissionError(
                        "authenticated A2A sender does not own the task"
                    )
                source = self._conn.execute(
                    """
                    SELECT 1 FROM a2a_gateway_messages
                    WHERE principal_id = ? AND a2a_task_id = ?
                      AND anet_task_id = ?
                    """,
                    (principal_id, a2a_task_id, source_anet_task_id),
                ).fetchone()
                if source is None:
                    raise ValueError("source Anet task is not bound to the A2A task")

                normalized_events: list[tuple[dict[str, Any], bytes, str]] = []
                total_bytes = 0
                for event in events:
                    normalized = validate_a2a_stream_event(
                        event,
                        task_id=a2a_task_id,
                        context_id=str(task["context_id"]),
                    )
                    encoded = pack(normalized)
                    total_bytes += len(encoded)
                    if total_bytes > MAX_WIRE_BYTES:
                        raise ValueError("A2A event batch is too large")
                    event_hash = hashlib.blake2s(
                        canonical_pack([source_anet_task_id, normalized]),
                        digest_size=32,
                        person=b"a2aevt1",
                    ).hexdigest()
                    normalized_events.append((normalized, encoded, event_hash))

                state = str(task["state"])
                sequence = int(task["last_sequence"])
                appended = 0
                duplicates = 0
                for normalized, encoded, event_hash in normalized_events:
                    existing = self._conn.execute(
                        """
                        SELECT sequence FROM a2a_gateway_events
                        WHERE principal_id = ? AND a2a_task_id = ?
                          AND event_hash = ?
                        """,
                        (principal_id, a2a_task_id, event_hash),
                    ).fetchone()
                    if existing is not None:
                        duplicates += 1
                        continue
                    if str(task["latest_anet_task_id"]) != source_anet_task_id:
                        raise ValueError(
                            "source Anet task is stale after a newer A2A message"
                        )
                    wrapper = next(iter(normalized))
                    if wrapper == "statusUpdate":
                        next_state = ANET_STATE_BY_A2A[
                            normalized[wrapper]["status"]["state"]
                        ]
                        if next_state not in _A2A_STATE_TRANSITIONS[state]:
                            raise ValueError(
                                f"invalid A2A task transition: {state} -> {next_state}"
                            )
                        state = next_state
                    elif state in TERMINAL_TASK_STATES:
                        raise ValueError(
                            "terminal A2A task cannot accept a new artifact"
                        )
                    sequence += 1
                    self._conn.execute(
                        """
                        INSERT INTO a2a_gateway_events(
                            principal_id, a2a_task_id, sequence,
                            source_anet_task_id, event_hash, event, created_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            principal_id,
                            a2a_task_id,
                            sequence,
                            source_anet_task_id,
                            event_hash,
                            sqlite3.Binary(encoded),
                            current,
                        ),
                    )
                    appended += 1

                if appended:
                    cancel_state = str(task["cancel_state"])
                    if cancel_state:
                        if state == "canceled":
                            cancel_state = "confirmed"
                        elif state in TERMINAL_TASK_STATES:
                            cancel_state = "too_late"
                    self._conn.execute(
                        """
                        UPDATE a2a_gateway_tasks
                        SET state = ?, last_sequence = ?, last_event_ms = ?,
                            cancel_state = ?, updated_ms = ?
                        WHERE principal_id = ? AND a2a_task_id = ?
                        """,
                        (
                            state,
                            sequence,
                            current,
                            cancel_state,
                            current,
                            principal_id,
                            a2a_task_id,
                        ),
                    )
                self._conn.execute("COMMIT")
                return {
                    "principal_id": principal_id,
                    "a2a_task_id": a2a_task_id,
                    "state": state,
                    "last_sequence": sequence,
                    "appended": appended,
                    "duplicates": duplicates,
                }
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def read_a2a_task_events(
        self,
        principal_id: str,
        a2a_task_id: str,
        *,
        authenticated_sender_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a principal-scoped resumable page after a monotonic cursor."""

        principal_id = self._consumer_id(principal_id, label="A2A principal ID")
        authenticated_sender_id = self._a2a_node_id(
            authenticated_sender_id,
            label="authenticated A2A sender Node ID",
        )
        a2a_task_id = self._a2a_external_id(
            a2a_task_id,
            label="A2A task ID",
        )
        after_sequence = int(after_sequence)
        limit = int(limit)
        if after_sequence < 0:
            raise ValueError("A2A event cursor cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("A2A event page limit must be between 1 and 1000")
        with self._lock:
            task = self._conn.execute(
                """
                SELECT t.*, p.sender_node_id
                FROM a2a_gateway_tasks t
                JOIN a2a_gateway_principals p
                  ON p.principal_id = t.principal_id
                WHERE t.principal_id = ? AND t.a2a_task_id = ?
                """,
                (principal_id, a2a_task_id),
            ).fetchone()
            if task is None:
                raise ValueError("unknown A2A task")
            if str(task["sender_node_id"]) != authenticated_sender_id:
                raise PermissionError(
                    "authenticated A2A sender does not own the task"
                )
            rows = self._conn.execute(
                """
                SELECT * FROM a2a_gateway_events
                WHERE principal_id = ? AND a2a_task_id = ?
                  AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (principal_id, a2a_task_id, after_sequence, limit),
            ).fetchall()
        items = [self._a2a_event_item(row) for row in rows]
        next_sequence = (
            items[-1]["sequence"] if items else after_sequence
        )
        last_sequence = int(task["last_sequence"])
        return {
            "principal_id": principal_id,
            "a2a_task_id": a2a_task_id,
            "state": str(task["state"]),
            "after_sequence": after_sequence,
            "next_sequence": next_sequence,
            "last_sequence": last_sequence,
            "caught_up": next_sequence >= last_sequence,
            "events": items,
        }

    def apply_agent_task_cancellation(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        *,
        allowed_senders: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        """Persist a trusted cancel request and fence the matching execution."""

        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        sender_policy = {
            str(sender).strip() for sender in allowed_senders if str(sender).strip()
        }
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                claim = self._conn.execute(
                    """
                    SELECT d.*, i.sender_id, i.kind, i.body, i.trusted
                    FROM consumer_deliveries d
                    JOIN inbox i ON i.packet_id = d.packet_id
                    WHERE d.group_id = ? AND d.owner = ? AND d.claim_token = ?
                      AND d.state = 'leased' AND d.lease_until_ms > ?
                    """,
                    (group_id, owner, claim_token, current),
                ).fetchone()
                if claim is None:
                    raise ValueError(
                        "claim is stale, expired, unknown, or owned by another worker"
                    )
                if not bool(claim["trusted"]):
                    raise PermissionError(
                        "typed Agent task cancellation requires a trusted sender"
                    )
                if str(claim["kind"]) != "agent.task.cancel":
                    raise ValueError("claim does not contain an Agent task cancellation")
                normalized = validate_task_message(
                    str(claim["kind"]),
                    unpack(bytes(claim["body"])),
                )
                sender_id = str(claim["sender_id"])
                task_id = str(normalized["task_id"])
                if "*" not in sender_policy and sender_id not in sender_policy:
                    raise PermissionError(
                        "task cancellation sender is outside the local execution policy"
                    )
                cancel_hash = hashlib.blake2s(
                    canonical_pack(normalized),
                    digest_size=32,
                    person=b"anetcxl1",
                ).hexdigest()
                existing_cancel = self._conn.execute(
                    """
                    SELECT * FROM agent_task_cancellations
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()
                if (
                    existing_cancel is not None
                    and str(existing_cancel["cancel_hash"]) != cancel_hash
                ):
                    raise ValueError(
                        "task cancellation was repeated with a different body"
                    )
                if existing_cancel is None:
                    self._conn.execute(
                        """
                        INSERT INTO agent_task_cancellations(
                            group_id, sender_id, task_id, cancel_packet_id,
                            cancel_hash, reason, state, terminal_state,
                            requested_ms, updated_ms, applied_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, 'requested', '', ?, ?, 0)
                        """,
                        (
                            group_id,
                            sender_id,
                            task_id,
                            str(claim["packet_id"]),
                            cancel_hash,
                            str(normalized["reason"]),
                            current,
                            current,
                        ),
                    )

                execution = self._conn.execute(
                    """
                    SELECT * FROM agent_task_executions
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()
                cancel_state = "requested"
                terminal_state = ""
                cooperative = False
                if execution is not None:
                    execution_state = str(execution["state"])
                    if execution_state in TERMINAL_TASK_STATES:
                        terminal_state = execution_state
                        cancel_state = (
                            "applied"
                            if execution_state == "canceled"
                            else "too_late"
                        )
                    elif execution_state in {"working", "canceling"}:
                        cooperative = True
                        self._conn.execute(
                            """
                            UPDATE agent_task_executions
                            SET state = 'canceling', updated_ms = ?, error = ?
                            WHERE group_id = ? AND sender_id = ? AND task_id = ?
                            """,
                            (
                                current,
                                str(normalized["reason"]),
                                group_id,
                                sender_id,
                                task_id,
                            ),
                        )
                    else:
                        terminal_state = "canceled"
                        cancel_state = "applied"
                        self._conn.execute(
                            """
                            UPDATE agent_task_executions
                            SET state = 'canceled', owner = '', claim_token = '',
                                execution_token = '', updated_ms = ?,
                                completed_ms = ?, output = NULL, error = ?
                            WHERE group_id = ? AND sender_id = ? AND task_id = ?
                            """,
                            (
                                current,
                                current,
                                str(normalized["reason"]),
                                group_id,
                                sender_id,
                                task_id,
                            ),
                        )
                        self._conn.execute(
                            """
                            UPDATE consumer_deliveries
                            SET state = 'acked', owner = '', claim_token = '',
                                lease_until_ms = 0, retry_after_ms = 0,
                                acked_ms = ?, last_error = ''
                            WHERE group_id = ? AND packet_id = ?
                              AND state != 'acked'
                            """,
                            (
                                current,
                                group_id,
                                str(execution["request_packet_id"]),
                            ),
                        )

                self._conn.execute(
                    """
                    UPDATE agent_task_cancellations
                    SET state = ?, terminal_state = ?, updated_ms = ?,
                        applied_ms = CASE WHEN ? = 'applied' THEN ? ELSE applied_ms END
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (
                        cancel_state,
                        terminal_state,
                        current,
                        cancel_state,
                        current,
                        group_id,
                        sender_id,
                        task_id,
                    ),
                )
                cursor = self._conn.execute(
                    """
                    UPDATE consumer_deliveries
                    SET state = 'acked', owner = '', claim_token = '',
                        lease_until_ms = 0, retry_after_ms = 0,
                        acked_ms = ?, last_error = ''
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND state = 'leased'
                    """,
                    (current, group_id, owner, claim_token),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to acknowledge task cancellation")
                row = self._conn.execute(
                    """
                    SELECT * FROM agent_task_cancellations
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._agent_task_cancellation_item(row)
                item.update(
                    {
                        "claim_acked": True,
                        "cooperative_stop_required": cooperative,
                    }
                )
                return item
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def agent_task_cancellation(
        self,
        group_id: str,
        owner: str,
        execution_token: str,
    ) -> dict[str, Any] | None:
        """Check whether the current execution token must stop cooperatively."""

        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        execution_token = self._claim_token(execution_token)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.* FROM agent_task_executions t
                JOIN agent_task_cancellations c
                  ON c.group_id = t.group_id
                 AND c.sender_id = t.sender_id
                 AND c.task_id = t.task_id
                WHERE t.group_id = ? AND t.owner = ?
                  AND t.execution_token = ? AND t.state = 'canceling'
                """,
                (group_id, owner, execution_token),
            ).fetchone()
        return self._agent_task_cancellation_item(row) if row else None

    def begin_agent_task(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        *,
        allowed_senders: set[str] | frozenset[str],
        allowed_capabilities: (
            list[str] | tuple[str, ...] | set[str] | frozenset[str]
        ),
    ) -> dict[str, Any]:
        """Atomically acquire the logical task behind one consumer claim.

        The authenticated sender, consumer group, and task ID form the
        idempotency key. A duplicate request is safe only when its canonical
        request body matches the first request exactly.
        """

        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        sender_policy = {
            str(sender).strip() for sender in allowed_senders if str(sender).strip()
        }
        capability_policy = normalize_capability_policy(allowed_capabilities)
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                claim = self._conn.execute(
                    """
                    SELECT d.*, i.sender_id, i.kind, i.body, i.trusted
                    FROM consumer_deliveries d
                    JOIN inbox i ON i.packet_id = d.packet_id
                    WHERE d.group_id = ? AND d.owner = ? AND d.claim_token = ?
                      AND d.state = 'leased' AND d.lease_until_ms > ?
                    """,
                    (group_id, owner, claim_token, current),
                ).fetchone()
                if claim is None:
                    raise ValueError(
                        "claim is stale, expired, unknown, or owned by another worker"
                    )
                if not bool(claim["trusted"]):
                    raise PermissionError(
                        "typed Agent task execution requires a trusted sender"
                    )
                if str(claim["kind"]) != "agent.task.request":
                    raise ValueError("claim does not contain an Agent task request")
                body = unpack(bytes(claim["body"]))
                normalized = validate_task_message(str(claim["kind"]), body)
                task_id = str(normalized["task_id"])
                sender_id = str(claim["sender_id"])
                if "*" not in sender_policy and sender_id not in sender_policy:
                    raise PermissionError(
                        "task sender is outside the local execution policy"
                    )
                missing = missing_task_capabilities(
                    normalized["required_capabilities"],
                    capability_policy,
                )
                if missing:
                    raise PermissionError(
                        "task requires capabilities outside the local execution policy: "
                        + ",".join(missing)
                    )
                request_hash = hashlib.blake2s(
                    canonical_pack(normalized),
                    digest_size=32,
                    person=b"anettsk1",
                ).hexdigest()
                existing = self._conn.execute(
                    """
                    SELECT * FROM agent_task_executions
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()
                if existing is not None and existing["request_hash"] != request_hash:
                    raise ValueError(
                        "task_id was reused with a different request body"
                    )
                cancellation = self._conn.execute(
                    """
                    SELECT * FROM agent_task_cancellations
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                      AND state IN ('requested', 'applied')
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()

                if existing is None and cancellation is not None:
                    reason = str(cancellation["reason"])
                    self._conn.execute(
                        """
                        INSERT INTO agent_task_executions(
                            group_id, sender_id, task_id, request_packet_id,
                            request_hash, state, owner, claim_token,
                            execution_token, attempts, started_ms, updated_ms,
                            completed_ms, output, error
                        ) VALUES (?, ?, ?, ?, ?, 'canceled', '', '', '', 0,
                                  0, ?, ?, NULL, ?)
                        """,
                        (
                            group_id,
                            sender_id,
                            task_id,
                            str(claim["packet_id"]),
                            request_hash,
                            current,
                            current,
                            reason,
                        ),
                    )
                    self._conn.execute(
                        """
                        UPDATE agent_task_cancellations
                        SET state = 'applied', terminal_state = 'canceled',
                            updated_ms = ?, applied_ms = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (current, current, group_id, sender_id, task_id),
                    )
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'acked', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            acked_ms = ?, last_error = ''
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (current, group_id, owner, claim_token),
                    )
                    row = self._conn.execute(
                        """
                        SELECT * FROM agent_task_executions
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (group_id, sender_id, task_id),
                    ).fetchone()
                    self._conn.execute("COMMIT")
                    item = self._agent_task_item(row)
                    item.update(
                        {
                            "execute": False,
                            "duplicate": False,
                            "claim_acked": True,
                            "execution_token": "",
                            "cancel_requested": True,
                        }
                    )
                    return item

                if existing is not None and str(existing["state"]) in TERMINAL_TASK_STATES:
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'acked', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            acked_ms = ?, last_error = ''
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (current, group_id, owner, claim_token),
                    )
                    self._conn.execute("COMMIT")
                    item = self._agent_task_item(existing)
                    item.update(
                        {
                            "execute": False,
                            "duplicate": True,
                            "claim_acked": True,
                            "execution_token": "",
                        }
                    )
                    return item

                if existing is not None and str(existing["state"]) == "canceling":
                    if (
                        str(existing["owner"]) == owner
                        and str(existing["claim_token"]) == claim_token
                    ):
                        self._conn.execute("COMMIT")
                        item = self._agent_task_item(existing)
                        item.update(
                            {
                                "execute": False,
                                "duplicate": False,
                                "claim_acked": False,
                                "execution_token": str(
                                    existing["execution_token"]
                                ),
                                "cancel_requested": True,
                            }
                        )
                        return item
                    active = self._conn.execute(
                        """
                        SELECT 1 FROM consumer_deliveries
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased' AND lease_until_ms > ?
                        """,
                        (
                            group_id,
                            str(existing["owner"]),
                            str(existing["claim_token"]),
                            current,
                        ),
                    ).fetchone()
                    if active is not None:
                        self._conn.execute(
                            """
                            UPDATE consumer_deliveries
                            SET state = 'acked', owner = '', claim_token = '',
                                lease_until_ms = 0, retry_after_ms = 0,
                                acked_ms = ?, last_error = ''
                            WHERE group_id = ? AND owner = ? AND claim_token = ?
                              AND state = 'leased'
                            """,
                            (current, group_id, owner, claim_token),
                        )
                        self._conn.execute("COMMIT")
                        item = self._agent_task_item(existing)
                        item.update(
                            {
                                "execute": False,
                                "duplicate": True,
                                "claim_acked": True,
                                "execution_token": "",
                                "cancel_requested": True,
                            }
                        )
                        return item

                    reason = (
                        str(cancellation["reason"])
                        if cancellation is not None
                        else str(existing["error"]) or "task cancellation requested"
                    )
                    self._conn.execute(
                        """
                        UPDATE agent_task_executions
                        SET state = 'canceled', owner = '', claim_token = '',
                            execution_token = '', updated_ms = ?,
                            completed_ms = ?, output = NULL, error = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (
                            current,
                            current,
                            reason,
                            group_id,
                            sender_id,
                            task_id,
                        ),
                    )
                    self._conn.execute(
                        """
                        UPDATE agent_task_cancellations
                        SET state = 'applied', terminal_state = 'canceled',
                            updated_ms = ?, applied_ms = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (current, current, group_id, sender_id, task_id),
                    )
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'acked', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            acked_ms = ?, last_error = ''
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (current, group_id, owner, claim_token),
                    )
                    row = self._conn.execute(
                        """
                        SELECT * FROM agent_task_executions
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (group_id, sender_id, task_id),
                    ).fetchone()
                    self._conn.execute("COMMIT")
                    item = self._agent_task_item(row)
                    item.update(
                        {
                            "execute": False,
                            "duplicate": True,
                            "claim_acked": True,
                            "execution_token": "",
                            "cancel_requested": True,
                        }
                    )
                    return item

                if (
                    existing is not None
                    and cancellation is not None
                    and str(existing["state"]) == "retry"
                ):
                    reason = str(cancellation["reason"])
                    self._conn.execute(
                        """
                        UPDATE agent_task_executions
                        SET state = 'canceled', owner = '', claim_token = '',
                            execution_token = '', updated_ms = ?,
                            completed_ms = ?, output = NULL, error = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (
                            current,
                            current,
                            reason,
                            group_id,
                            sender_id,
                            task_id,
                        ),
                    )
                    self._conn.execute(
                        """
                        UPDATE agent_task_cancellations
                        SET state = 'applied', terminal_state = 'canceled',
                            updated_ms = ?, applied_ms = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (current, current, group_id, sender_id, task_id),
                    )
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'acked', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            acked_ms = ?, last_error = ''
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (current, group_id, owner, claim_token),
                    )
                    row = self._conn.execute(
                        """
                        SELECT * FROM agent_task_executions
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (group_id, sender_id, task_id),
                    ).fetchone()
                    self._conn.execute("COMMIT")
                    item = self._agent_task_item(row)
                    item.update(
                        {
                            "execute": False,
                            "duplicate": True,
                            "claim_acked": True,
                            "execution_token": "",
                            "cancel_requested": True,
                        }
                    )
                    return item

                if existing is not None and str(existing["state"]) == "working":
                    if (
                        str(existing["owner"]) == owner
                        and str(existing["claim_token"]) == claim_token
                    ):
                        self._conn.execute("COMMIT")
                        item = self._agent_task_item(existing)
                        item.update(
                            {
                                "execute": True,
                                "duplicate": False,
                                "claim_acked": False,
                                "execution_token": str(
                                    existing["execution_token"]
                                ),
                            }
                        )
                        return item
                    active = self._conn.execute(
                        """
                        SELECT 1 FROM consumer_deliveries
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased' AND lease_until_ms > ?
                        """,
                        (
                            group_id,
                            str(existing["owner"]),
                            str(existing["claim_token"]),
                            current,
                        ),
                    ).fetchone()
                    if active is not None:
                        self._conn.execute(
                            """
                            UPDATE consumer_deliveries
                            SET state = 'acked', owner = '', claim_token = '',
                                lease_until_ms = 0, retry_after_ms = 0,
                                acked_ms = ?, last_error = ''
                            WHERE group_id = ? AND owner = ? AND claim_token = ?
                              AND state = 'leased'
                            """,
                            (current, group_id, owner, claim_token),
                        )
                        self._conn.execute("COMMIT")
                        item = self._agent_task_item(existing)
                        item.update(
                            {
                                "execute": False,
                                "duplicate": True,
                                "claim_acked": True,
                                "execution_token": "",
                            }
                        )
                        return item

                execution_token = secrets.token_hex(16)
                if existing is None:
                    self._conn.execute(
                        """
                        INSERT INTO agent_task_executions(
                            group_id, sender_id, task_id, request_packet_id,
                            request_hash, state, owner, claim_token,
                            execution_token, attempts, started_ms, updated_ms,
                            completed_ms, output, error
                        ) VALUES (?, ?, ?, ?, ?, 'working', ?, ?, ?, 1, ?, ?, 0, NULL, '')
                        """,
                        (
                            group_id,
                            sender_id,
                            task_id,
                            str(claim["packet_id"]),
                            request_hash,
                            owner,
                            claim_token,
                            execution_token,
                            current,
                            current,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE agent_task_executions
                        SET request_packet_id = ?, state = 'working', owner = ?,
                            claim_token = ?, execution_token = ?,
                            attempts = attempts + 1, updated_ms = ?,
                            completed_ms = 0, output = NULL, error = ''
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (
                            str(claim["packet_id"]),
                            owner,
                            claim_token,
                            execution_token,
                            current,
                            group_id,
                            sender_id,
                            task_id,
                        ),
                    )
                row = self._conn.execute(
                    """
                    SELECT * FROM agent_task_executions
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (group_id, sender_id, task_id),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._agent_task_item(row)
                item.update(
                    {
                        "execute": True,
                        "duplicate": existing is not None,
                        "claim_acked": False,
                        "execution_token": execution_token,
                    }
                )
                return item
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def settle_agent_task(
        self,
        group_id: str,
        owner: str,
        claim_token: str,
        execution_token: str,
        *,
        state: str,
        output: Any = None,
        error: str = "",
        retry_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Persist a task outcome and settle its consumer claim atomically."""

        group_id = self._consumer_id(group_id, label="group ID")
        owner = self._consumer_id(owner, label="owner")
        claim_token = self._claim_token(claim_token)
        execution_token = self._claim_token(execution_token)
        state = str(state).strip().lower()
        if state not in {*TERMINAL_TASK_STATES, "retry"}:
            raise ValueError(
                "task settlement state must be completed, failed, canceled, rejected, or retry"
            )
        error = str(error).strip()
        if state == "completed" and error:
            raise ValueError("a completed task cannot contain an error")
        if state in TERMINAL_TASK_STATES - {"completed"} and not error:
            raise ValueError("an unsuccessful task settlement requires an error")
        if len(error) > 8192:
            raise ValueError("task settlement error is too long")
        encoded_output = pack(output)
        if len(encoded_output) > MAX_WIRE_BYTES:
            raise ValueError("task settlement output is too large")
        current = now_ms()
        retry_after = current + int(
            max(0.0, min(float(retry_seconds), 86400.0)) * 1000
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                execution = self._conn.execute(
                    """
                    SELECT * FROM agent_task_executions
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND execution_token = ? AND state IN ('working', 'canceling')
                    """,
                    (group_id, owner, claim_token, execution_token),
                ).fetchone()
                claim = self._conn.execute(
                    """
                    SELECT * FROM consumer_deliveries
                    WHERE group_id = ? AND owner = ? AND claim_token = ?
                      AND state = 'leased'
                    """,
                    (group_id, owner, claim_token),
                ).fetchone()
                if execution is None or claim is None:
                    raise ValueError(
                        "task execution is stale, unknown, or owned by another worker"
                    )
                if str(execution["state"]) == "canceling" and state != "canceled":
                    raise ValueError(
                        "task cancellation is pending; only canceled settlement is allowed"
                    )
                if state == "retry":
                    self._conn.execute(
                        """
                        UPDATE agent_task_executions
                        SET state = 'retry', owner = '', claim_token = '',
                            execution_token = '', updated_ms = ?, error = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (
                            current,
                            error[:8192],
                            group_id,
                            str(execution["sender_id"]),
                            str(execution["task_id"]),
                        ),
                    )
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'retry', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = ?,
                            acked_ms = 0, last_error = ?
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (
                            retry_after,
                            error[:1000],
                            group_id,
                            owner,
                            claim_token,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE agent_task_executions
                        SET state = ?, owner = '', claim_token = '',
                            execution_token = '', updated_ms = ?,
                            completed_ms = ?, output = ?, error = ?
                        WHERE group_id = ? AND sender_id = ? AND task_id = ?
                        """,
                        (
                            state,
                            current,
                            current,
                            encoded_output,
                            error[:8192],
                            group_id,
                            str(execution["sender_id"]),
                            str(execution["task_id"]),
                        ),
                    )
                    self._conn.execute(
                        """
                        UPDATE consumer_deliveries
                        SET state = 'acked', owner = '', claim_token = '',
                            lease_until_ms = 0, retry_after_ms = 0,
                            acked_ms = ?, last_error = ''
                        WHERE group_id = ? AND owner = ? AND claim_token = ?
                          AND state = 'leased'
                        """,
                        (current, group_id, owner, claim_token),
                    )
                    if state == "canceled":
                        self._conn.execute(
                            """
                            UPDATE agent_task_cancellations
                            SET state = 'applied', terminal_state = 'canceled',
                                updated_ms = ?, applied_ms = ?
                            WHERE group_id = ? AND sender_id = ? AND task_id = ?
                              AND state = 'requested'
                            """,
                            (
                                current,
                                current,
                                group_id,
                                str(execution["sender_id"]),
                                str(execution["task_id"]),
                            ),
                        )
                row = self._conn.execute(
                    """
                    SELECT * FROM agent_task_executions
                    WHERE group_id = ? AND sender_id = ? AND task_id = ?
                    """,
                    (
                        group_id,
                        str(execution["sender_id"]),
                        str(execution["task_id"]),
                    ),
                ).fetchone()
                self._conn.execute("COMMIT")
                item = self._agent_task_item(row)
                item["claim_state"] = "retry" if state == "retry" else "acked"
                if state == "retry":
                    item["retry_after_ms"] = retry_after
                return item
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_read(self, packet_ids: list[str]) -> None:
        if not packet_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE inbox SET is_read = 1 WHERE packet_id = ?",
                [(str(packet_id),) for packet_id in packet_ids],
            )

    def record_receipt(self, packet_id: str, recipient_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO receipts(packet_id, recipient_id, delivered_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(packet_id) DO UPDATE SET
                    recipient_id = excluded.recipient_id,
                    delivered_ms = excluded.delivered_ms
                """,
                (str(packet_id), str(recipient_id), now_ms()),
            )
            self._conn.execute(
                "UPDATE packets SET delivered = 1 WHERE packet_id = ?",
                (str(packet_id),),
            )

    def receipt(self, packet_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM receipts WHERE packet_id = ?",
                (str(packet_id),),
            ).fetchone()
        return dict(row) if row else None

    def delivery_paths(self, packet_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_paths
                WHERE packet_id = ? ORDER BY peer_id, path_id
                """,
                (str(packet_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_path_state(
        self, packet_id: str, peer_id: str, path_id: str
    ) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT state
                FROM delivery_paths
                WHERE packet_id = ? AND peer_id = ? AND path_id = ?
                """,
                (str(packet_id), str(peer_id), str(path_id)),
            ).fetchone()
        return None if row is None else str(row["state"])

    def mark_local_delivered(self, packet_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE packets SET delivered = 1 WHERE packet_id = ?",
                (str(packet_id),),
            )

    def mark_local_rejected(
        self, packet_id: str, *, peer_id: str = "", reason: str = ""
    ) -> None:
        current = now_ms()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE packets SET delivered = 1 WHERE packet_id = ?",
                    (str(packet_id),),
                )
                self._conn.execute(
                    """
                    INSERT INTO packet_rejections(
                        packet_id, peer_id, rejected_ms, reason
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(packet_id) DO NOTHING
                    """,
                    (
                        str(packet_id),
                        str(peer_id)[:128],
                        current,
                        str(reason)[:1000],
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def packet_rejection(self, packet_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM packet_rejections WHERE packet_id = ?",
                (str(packet_id),),
            ).fetchone()
        return dict(row) if row else None

    def local_packets(self, destination_id: str, *, limit: int = 1000) -> list[bytes]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw FROM packets
                WHERE destination_id = ? AND delivered = 0 AND expires_ms > ?
                ORDER BY created_ms ASC LIMIT ?
                """,
                (str(destination_id), now_ms(), max(1, min(int(limit), 10000))),
            ).fetchall()
        return [bytes(row["raw"]) for row in rows]

    def export_packets(
        self, *, destination_id: str = "", limit: int = 10000
    ) -> list[bytes]:
        current = now_ms()
        params: list[Any] = [current]
        where = "expires_ms > ? AND delivered = 0"
        if destination_id:
            where += " AND destination_id = ?"
            params.append(destination_id)
        params.append(max(1, min(int(limit), 100000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT raw FROM packets WHERE {where} ORDER BY created_ms ASC LIMIT ?",
                params,
            ).fetchall()
        return [bytes(row["raw"]) for row in rows]

    def purge(
        self, *, transient_retention_ms: int = 7 * 86400 * 1000
    ) -> dict[str, int]:
        current = now_ms()
        transient_cutoff = current - max(60_000, int(transient_retention_ms))
        local_prekey_cutoff = (
            current - MAX_TTL_SECONDS * 1000 - MAX_CLOCK_SKEW_MS
        )
        with self._lock:
            expired = self._conn.execute(
                "DELETE FROM packets WHERE expires_ms <= ?",
                (current,),
            ).rowcount
            orphan_deliveries = self._conn.execute(
                "DELETE FROM deliveries WHERE packet_id NOT IN (SELECT packet_id FROM packets)"
            ).rowcount
            orphan_paths = self._conn.execute(
                "DELETE FROM delivery_paths WHERE packet_id NOT IN (SELECT packet_id FROM packets)"
            ).rowcount
            orphan_receipts = self._conn.execute(
                "DELETE FROM receipts WHERE packet_id NOT IN (SELECT packet_id FROM packets)"
            ).rowcount
            orphan_rejections = self._conn.execute(
                """
                DELETE FROM packet_rejections
                WHERE packet_id NOT IN (SELECT packet_id FROM packets)
                """
            ).rowcount
            old_transient = self._conn.execute(
                "DELETE FROM inbox WHERE visible = 0 AND received_ms <= ?",
                (transient_cutoff,),
            ).rowcount
            orphan_consumer_deliveries = self._conn.execute(
                "DELETE FROM consumer_deliveries WHERE packet_id NOT IN (SELECT packet_id FROM inbox)"
            ).rowcount
            expired_local_prekeys = self._conn.execute(
                """
                UPDATE local_prekeys
                SET private_key = NULL, state = 'expired', consumed_ms = ?
                WHERE state = 'available' AND expires_ms <= ?
                  AND private_key IS NOT NULL
                """,
                (current, local_prekey_cutoff),
            ).rowcount
            expired_peer_prekeys = self._conn.execute(
                """
                UPDATE peer_prekeys SET state = 'expired', used_ms = ?
                WHERE state = 'available' AND expires_ms <= ?
                """,
                (current, current),
            ).rowcount
            if expired_local_prekeys:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
        return {
            "expired_packets": expired,
            "orphan_deliveries": orphan_deliveries,
            "orphan_delivery_paths": orphan_paths,
            "orphan_receipts": orphan_receipts,
            "orphan_rejections": orphan_rejections,
            "old_transient_inbox": old_transient,
            "orphan_consumer_deliveries": orphan_consumer_deliveries,
            "expired_local_prekeys": expired_local_prekeys,
            "expired_peer_prekeys": expired_peer_prekeys,
        }

    def record_path_result(
        self,
        peer_id: str,
        path_id: str,
        *,
        success: bool,
        latency_ms: float,
        error: str = "",
    ) -> None:
        current = now_ms()
        peer_id = str(peer_id)
        path_id = str(path_id)
        latency_ms = max(0.0, float(latency_ms))
        with self._lock:
            row = self._conn.execute(
                "SELECT ewma_rtt_ms FROM path_metrics WHERE peer_id = ? AND path_id = ?",
                (peer_id, path_id),
            ).fetchone()
            previous = float(row["ewma_rtt_ms"]) if row else 0.0
            if success:
                ewma = (
                    latency_ms
                    if previous <= 0
                    else (0.25 * latency_ms + 0.75 * previous)
                )
            else:
                ewma = previous
            self._conn.execute(
                """
                INSERT INTO path_metrics(
                    peer_id, path_id, attempts, successes, failures,
                    consecutive_successes, consecutive_failures, ewma_rtt_ms,
                    last_attempt_ms, last_ok_ms, last_failure_ms, last_error
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id, path_id) DO UPDATE SET
                    attempts = path_metrics.attempts + 1,
                    successes = path_metrics.successes + excluded.successes,
                    failures = path_metrics.failures + excluded.failures,
                    consecutive_successes = CASE
                        WHEN excluded.successes = 1 THEN path_metrics.consecutive_successes + 1 ELSE 0 END,
                    consecutive_failures = CASE
                        WHEN excluded.failures = 1 THEN path_metrics.consecutive_failures + 1 ELSE 0 END,
                    ewma_rtt_ms = excluded.ewma_rtt_ms,
                    last_attempt_ms = excluded.last_attempt_ms,
                    last_ok_ms = CASE
                        WHEN excluded.successes = 1 THEN excluded.last_ok_ms ELSE path_metrics.last_ok_ms END,
                    last_failure_ms = CASE
                        WHEN excluded.failures = 1 THEN excluded.last_failure_ms ELSE path_metrics.last_failure_ms END,
                    last_error = excluded.last_error
                """,
                (
                    peer_id,
                    path_id,
                    1 if success else 0,
                    0 if success else 1,
                    1 if success else 0,
                    0 if success else 1,
                    ewma,
                    current,
                    current if success else 0,
                    0 if success else current,
                    "" if success else str(error)[:1000],
                ),
            )

    def path_metrics(self, peer_id: str = "") -> list[dict[str, Any]]:
        where = " WHERE peer_id = ?" if peer_id else ""
        params: tuple[Any, ...] = (str(peer_id),) if peer_id else ()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM path_metrics{where} ORDER BY peer_id, path_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def path_metric(self, peer_id: str, path_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM path_metrics WHERE peer_id = ? AND path_id = ?",
                (str(peer_id), str(path_id)),
            ).fetchone()
        return dict(row) if row else None

    def set_route(
        self, peer_id: str, selected_path: str, reason: str
    ) -> dict[str, Any]:
        current = now_ms()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM route_state WHERE peer_id = ?",
                (str(peer_id),),
            ).fetchone()
            if existing and existing["selected_path"] == str(selected_path):
                self._conn.execute(
                    "UPDATE route_state SET reason = ? WHERE peer_id = ?",
                    (str(reason)[:500], str(peer_id)),
                )
                row = self._conn.execute(
                    "SELECT * FROM route_state WHERE peer_id = ?",
                    (str(peer_id),),
                ).fetchone()
                return dict(row)
            self._conn.execute(
                """
                INSERT INTO route_state(peer_id, selected_path, switched_ms, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    selected_path = excluded.selected_path,
                    switched_ms = excluded.switched_ms,
                    reason = excluded.reason
                """,
                (str(peer_id), str(selected_path), current, str(reason)[:500]),
            )
            row = self._conn.execute(
                "SELECT * FROM route_state WHERE peer_id = ?",
                (str(peer_id),),
            ).fetchone()
        return dict(row)

    def route(self, peer_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM route_state WHERE peer_id = ?",
                (str(peer_id),),
            ).fetchone()
        return dict(row) if row else None

    def routes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM route_state ORDER BY peer_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._lock:
            packets = self._conn.execute(
                "SELECT COUNT(*) AS n FROM packets"
            ).fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM packets WHERE delivered = 0 AND expires_ms > ?",
                (now_ms(),),
            ).fetchone()["n"]
            inbox = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inbox WHERE visible = 1"
            ).fetchone()["n"]
            transient = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inbox WHERE visible = 0"
            ).fetchone()["n"]
            unread = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inbox WHERE is_read = 0 AND visible = 1"
            ).fetchone()["n"]
            untrusted = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inbox WHERE trusted = 0 AND visible = 1"
            ).fetchone()["n"]
            receipts = self._conn.execute(
                "SELECT COUNT(*) AS n FROM receipts"
            ).fetchone()["n"]
            rejections = self._conn.execute(
                "SELECT COUNT(*) AS n FROM packet_rejections"
            ).fetchone()["n"]
            consumer_groups = self._conn.execute(
                "SELECT COUNT(*) AS n FROM consumer_groups"
            ).fetchone()["n"]
            active_claims = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM consumer_deliveries
                WHERE state = 'leased' AND lease_until_ms > ?
                """,
                (now_ms(),),
            ).fetchone()["n"]
            agent_tasks = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agent_task_executions"
            ).fetchone()["n"]
            active_agent_tasks = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM agent_task_executions
                WHERE state IN ('working', 'canceling')
                """
            ).fetchone()["n"]
            agent_task_cancellations = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agent_task_cancellations"
            ).fetchone()["n"]
            pending_agent_task_cancellations = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM agent_task_cancellations
                WHERE state = 'requested'
                """
            ).fetchone()["n"]
            companion_approval_requests = self._conn.execute(
                "SELECT COUNT(*) AS n FROM companion_approval_requests"
            ).fetchone()["n"]
            active_companion_approvals = self._conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM companion_approval_authorizations
                WHERE state = 'active' AND grant_expires_ms > ?
                """,
                (now_ms(),),
            ).fetchone()["n"]
            companion_effect_rows = self._conn.execute(
                """
                SELECT state, COUNT(*) AS n
                FROM companion_approval_effects GROUP BY state
                """
            ).fetchall()
            a2a_principals = self._conn.execute(
                "SELECT COUNT(*) AS n FROM a2a_gateway_principals"
            ).fetchone()["n"]
            a2a_tasks = self._conn.execute(
                "SELECT COUNT(*) AS n FROM a2a_gateway_tasks"
            ).fetchone()["n"]
            a2a_messages = self._conn.execute(
                "SELECT COUNT(*) AS n FROM a2a_gateway_messages"
            ).fetchone()["n"]
            a2a_events = self._conn.execute(
                "SELECT COUNT(*) AS n FROM a2a_gateway_events"
            ).fetchone()["n"]
            a2a_dispatches = self._conn.execute(
                "SELECT COUNT(*) AS n FROM a2a_gateway_dispatches"
            ).fetchone()["n"]
            pending_a2a_dispatches = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM a2a_gateway_dispatches
                WHERE state != 'dispatched'
                """
            ).fetchone()["n"]
            pending_rows = self._conn.execute(
                """
                SELECT qos, COUNT(*) AS n FROM packets
                WHERE delivered = 0 AND expires_ms > ? GROUP BY qos
                """,
                (now_ms(),),
            ).fetchall()
        return {
            "packets": int(packets),
            "pending": int(pending),
            "inbox": int(inbox),
            "unread": int(unread),
            "untrusted": int(untrusted),
            "receipts": int(receipts),
            "rejections": int(rejections),
            "transient": int(transient),
            "consumer_groups": int(consumer_groups),
            "active_claims": int(active_claims),
            "agent_tasks": int(agent_tasks),
            "active_agent_tasks": int(active_agent_tasks),
            "agent_task_cancellations": int(agent_task_cancellations),
            "pending_agent_task_cancellations": int(
                pending_agent_task_cancellations
            ),
            "companion_approvals": {
                "requests": int(companion_approval_requests),
                "active": int(active_companion_approvals),
                "effects": {
                    str(row["state"]): int(row["n"])
                    for row in companion_effect_rows
                },
            },
            "a2a_gateway": {
                "principals": int(a2a_principals),
                "tasks": int(a2a_tasks),
                "messages": int(a2a_messages),
                "events": int(a2a_events),
                "dispatches": int(a2a_dispatches),
                "pending_dispatches": int(pending_a2a_dispatches),
            },
            "pending_by_qos": {str(row["qos"]): int(row["n"]) for row in pending_rows},
        }
