from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .control_plane import (
    ControlPlaneStore,
    NodeDescriptor,
    ReachabilityRecord,
)
from .encoding import MAX_WIRE_BYTES, b64d, b64e, canonical_pack
from .identity import Identity
from .packet import PacketInfo, inspect_packet


AHUB_REQUEST_VERSION = 1
AHUB_REQUEST_TYPE = "anet.ahub-request.v1"
AHUB_DB_VERSION = 6
AHUB_REQUEST_SKEW_MS = 5 * 60 * 1000
AHUB_NONCE_RETENTION_MS = 10 * 60 * 1000
MIN_CLAIM_LEASE_MS = 5 * 1000
MAX_CLAIM_LEASE_MS = 5 * 60 * 1000
MAX_CLAIM_BATCH = 100
DEFAULT_MAX_PACKETS = 1_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MIN_RELAY_RESERVATION_TTL_MS = 30 * 1000
DEFAULT_RELAY_RESERVATION_TTL_MS = 15 * 60 * 1000
MIN_RELAY_DURATION_MS = 1 * 1000
DEFAULT_MAX_RELAY_DURATION_MS = 5 * 60 * 1000
DEFAULT_MAX_RELAY_BYTES_EACH_DIRECTION = 64 * 1024 * 1024
DEFAULT_MAX_RELAY_RESERVATIONS_PER_NODE = 4
DEFAULT_MAX_RELAY_CONNECTIONS_PER_NODE = 4
DEFAULT_MAX_RELAY_FRAME_BYTES = 64 * 1024

_NODE_ID_RE = re.compile(r"^an1[a-z2-7]{32}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,511}$")
_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_PACKET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RELAY_RESERVATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_node_id(node_id: str) -> str:
    value = str(node_id)
    if not _NODE_ID_RE.fullmatch(value):
        raise ValueError("invalid Anet node id")
    return value


def _validate_method(method: str) -> str:
    value = str(method).upper()
    if value not in _METHODS:
        raise ValueError("unsupported Ahub request method")
    return value


def _validate_path(path: str) -> str:
    value = str(path)
    if not _PATH_RE.fullmatch(value) or "//" in value or ".." in value:
        raise ValueError("invalid Ahub request path")
    return value


@dataclass(frozen=True)
class AhubRequest:
    node_id: str
    method: str
    path: str
    issued_ms: int
    nonce: str
    body_sha256: bytes
    signature: bytes
    version: int = AHUB_REQUEST_VERSION
    object_type: str = AHUB_REQUEST_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.node_id,
            self.method,
            self.path,
            self.issued_ms,
            self.nonce,
            self.body_sha256,
        ]

    def verify(
        self,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        now: int | None = None,
    ) -> None:
        current = _now_ms() if now is None else now
        if (
            self.version != AHUB_REQUEST_VERSION
            or self.object_type != AHUB_REQUEST_TYPE
        ):
            raise ValueError("unsupported Ahub request")
        if _validate_node_id(self.node_id) != descriptor.node_id:
            raise ValueError("Ahub request descriptor mismatch")
        if self.method != _validate_method(self.method):
            raise ValueError("Ahub request method must be canonical")
        _validate_path(self.path)
        if (
            isinstance(self.issued_ms, bool)
            or not isinstance(self.issued_ms, int)
            or abs(current - self.issued_ms) > AHUB_REQUEST_SKEW_MS
        ):
            raise ValueError("Ahub request is outside the clock window")
        if not _NONCE_RE.fullmatch(self.nonce):
            raise ValueError("invalid Ahub request nonce")
        if len(self.body_sha256) != 32:
            raise ValueError("invalid Ahub request body digest")
        if not hmac.compare_digest(
            self.body_sha256, hashlib.sha256(bytes(body)).digest()
        ):
            raise ValueError("Ahub request body digest mismatch")
        if len(self.signature) != 64:
            raise ValueError("invalid Ahub request signature")
        descriptor.verify(now=current)
        Ed25519PublicKey.from_public_bytes(descriptor.sign_public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Anet-Node": self.node_id,
            "X-Anet-Issued": str(self.issued_ms),
            "X-Anet-Nonce": self.nonce,
            "X-Anet-Signature": b64e(self.signature),
        }

    @classmethod
    def from_headers(
        cls,
        headers: dict[str, str],
        *,
        method: str,
        path: str,
        body: bytes,
    ) -> "AhubRequest":
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        required = (
            "x-anet-node",
            "x-anet-issued",
            "x-anet-nonce",
            "x-anet-signature",
        )
        if any(name not in normalized for name in required):
            raise ValueError("missing Ahub authentication headers")
        try:
            issued_ms = int(normalized["x-anet-issued"])
            signature = b64d(normalized["x-anet-signature"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Ahub authentication headers") from exc
        return cls(
            node_id=normalized["x-anet-node"],
            method=_validate_method(method),
            path=_validate_path(path),
            issued_ms=issued_ms,
            nonce=normalized["x-anet-nonce"],
            body_sha256=hashlib.sha256(bytes(body)).digest(),
            signature=signature,
        )


def issue_ahub_request(
    identity: Identity,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    issued_ms: int | None = None,
    nonce: str | None = None,
) -> AhubRequest:
    unsigned = AhubRequest(
        node_id=identity.node_id,
        method=_validate_method(method),
        path=_validate_path(path),
        issued_ms=_now_ms() if issued_ms is None else issued_ms,
        nonce=secrets.token_urlsafe(24) if nonce is None else str(nonce),
        body_sha256=hashlib.sha256(bytes(body)).digest(),
        signature=b"",
    )
    if not _NONCE_RE.fullmatch(unsigned.nonce):
        raise ValueError("invalid Ahub request nonce")
    return AhubRequest(
        **{
            **unsigned.__dict__,
            "signature": identity.sign(canonical_pack(unsigned.signing_fields())),
        }
    )


@dataclass(frozen=True)
class AhubLimits:
    max_packet_bytes: int = MAX_WIRE_BYTES
    max_packets_per_destination: int = DEFAULT_MAX_PACKETS
    max_bytes_per_destination: int = DEFAULT_MAX_BYTES
    max_packets_per_uploader: int = DEFAULT_MAX_PACKETS
    max_bytes_per_uploader: int = DEFAULT_MAX_BYTES
    max_relay_reservation_ttl_ms: int = DEFAULT_RELAY_RESERVATION_TTL_MS
    max_relay_duration_ms: int = DEFAULT_MAX_RELAY_DURATION_MS
    max_relay_bytes_each_direction: int = (
        DEFAULT_MAX_RELAY_BYTES_EACH_DIRECTION
    )
    max_relay_reservations_per_node: int = (
        DEFAULT_MAX_RELAY_RESERVATIONS_PER_NODE
    )
    max_relay_connections_per_node: int = (
        DEFAULT_MAX_RELAY_CONNECTIONS_PER_NODE
    )
    max_relay_frame_bytes: int = DEFAULT_MAX_RELAY_FRAME_BYTES

    def __post_init__(self) -> None:
        values = tuple(self.__dict__.values())
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("Ahub limits must be positive integers")
        if self.max_packet_bytes > MAX_WIRE_BYTES:
            raise ValueError("Ahub packet limit exceeds the protocol limit")
        if self.max_relay_reservation_ttl_ms < MIN_RELAY_RESERVATION_TTL_MS:
            raise ValueError("Ahub Relay reservation TTL limit is too small")
        if self.max_relay_duration_ms < MIN_RELAY_DURATION_MS:
            raise ValueError("Ahub Relay duration limit is too small")


@dataclass(frozen=True)
class CustodyReceipt:
    packet_id: str
    destination_id: str
    stored: bool
    expires_ms: int


@dataclass(frozen=True)
class RelayReservation:
    reservation_id: str
    owner_id: str
    allowed_peer_id: str
    created_ms: int
    expires_ms: int
    max_duration_ms: int
    max_bytes_each_direction: int

    def __post_init__(self) -> None:
        if not _RELAY_RESERVATION_ID_RE.fullmatch(self.reservation_id):
            raise ValueError("invalid Relay reservation ID")
        _validate_node_id(self.owner_id)
        _validate_node_id(self.allowed_peer_id)
        if self.owner_id == self.allowed_peer_id:
            raise ValueError("Relay reservation peer must differ from owner")
        values = (
            self.created_ms,
            self.expires_ms,
            self.max_duration_ms,
            self.max_bytes_each_direction,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in values
        ):
            raise ValueError("invalid Relay reservation integer")
        if (
            self.created_ms <= 0
            or self.expires_ms <= self.created_ms
            or self.max_duration_ms < MIN_RELAY_DURATION_MS
            or self.max_bytes_each_direction <= 0
        ):
            raise ValueError("invalid Relay reservation limits")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "owner_id": self.owner_id,
            "allowed_peer_id": self.allowed_peer_id,
            "created_ms": self.created_ms,
            "expires_ms": self.expires_ms,
            "max_duration_ms": self.max_duration_ms,
            "max_bytes_each_direction": self.max_bytes_each_direction,
        }


@dataclass(frozen=True)
class ClaimedPacket:
    packet_id: str
    raw: bytes
    depth: int
    claim_token: str
    claim_until_ms: int
    uploader_id: str = ""
    expires_ms: int = 0


@dataclass(frozen=True)
class DestinationSettlement:
    packet_id: str
    packet_sha256: bytes
    uploader_id: str
    destination_id: str
    settled_ms: int
    expires_ms: int
    signature: bytes
    version: int = 1
    object_type: str = "anet.destination-settlement.v1"

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.packet_id,
            self.packet_sha256,
            self.uploader_id,
            self.destination_id,
            self.settled_ms,
            self.expires_ms,
        ]

    def verify(
        self,
        sign_public: bytes,
        *,
        now: int | None = None,
    ) -> None:
        current = _now_ms() if now is None else now
        if self.version != 1 or self.object_type != "anet.destination-settlement.v1":
            raise ValueError("unsupported destination settlement")
        if not _PACKET_ID_RE.fullmatch(self.packet_id):
            raise ValueError("invalid settlement packet id")
        if len(self.packet_sha256) != 32:
            raise ValueError("invalid settlement packet digest")
        _validate_node_id(self.uploader_id)
        _validate_node_id(self.destination_id)
        public = bytes(sign_public)
        if len(public) != 32 or len(self.signature) != 64:
            raise ValueError("invalid settlement signing material")
        if (
            isinstance(self.settled_ms, bool)
            or isinstance(self.expires_ms, bool)
            or not isinstance(self.settled_ms, int)
            or not isinstance(self.expires_ms, int)
            or self.settled_ms <= 0
            or self.expires_ms <= self.settled_ms
            or self.settled_ms > current + AHUB_REQUEST_SKEW_MS
            or self.expires_ms <= current
        ):
            raise ValueError("invalid destination settlement time window")
        Ed25519PublicKey.from_public_bytes(public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "object_type": self.object_type,
            "packet_id": self.packet_id,
            "packet_sha256": b64e(self.packet_sha256),
            "uploader_id": self.uploader_id,
            "destination_id": self.destination_id,
            "settled_ms": self.settled_ms,
            "expires_ms": self.expires_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DestinationSettlement":
        fields = {
            "version",
            "object_type",
            "packet_id",
            "packet_sha256",
            "uploader_id",
            "destination_id",
            "settled_ms",
            "expires_ms",
            "signature",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid destination settlement fields")
        for name in ("version", "settled_ms", "expires_ms"):
            if isinstance(value[name], bool) or not isinstance(value[name], int):
                raise ValueError("invalid destination settlement integer")
        try:
            packet_sha256 = b64d(str(value["packet_sha256"]))
            signature = b64d(str(value["signature"]))
        except ValueError as exc:
            raise ValueError("invalid destination settlement encoding") from exc
        return cls(
            version=value["version"],
            object_type=str(value["object_type"]),
            packet_id=str(value["packet_id"]),
            packet_sha256=packet_sha256,
            uploader_id=str(value["uploader_id"]),
            destination_id=str(value["destination_id"]),
            settled_ms=value["settled_ms"],
            expires_ms=value["expires_ms"],
            signature=signature,
        )


def issue_destination_settlement(
    identity: Identity,
    *,
    packet_id: str,
    raw: bytes,
    uploader_id: str,
    expires_ms: int,
    settled_ms: int | None = None,
) -> DestinationSettlement:
    settled = _now_ms() if settled_ms is None else settled_ms
    unsigned = DestinationSettlement(
        packet_id=str(packet_id),
        packet_sha256=hashlib.sha256(bytes(raw)).digest(),
        uploader_id=_validate_node_id(uploader_id),
        destination_id=identity.node_id,
        settled_ms=settled,
        expires_ms=int(expires_ms),
        signature=b"",
    )
    proof = DestinationSettlement(
        **{
            **unsigned.__dict__,
            "signature": identity.sign(
                canonical_pack(unsigned.signing_fields())
            ),
        }
    )
    proof.verify(identity.sign_public, now=settled)
    return proof


class AhubStore:
    """Durable allowlist, anti-replay state, and encrypted mailbox custody."""

    def __init__(self, path: Path, *, limits: AhubLimits | None = None) -> None:
        self.path = Path(path)
        self.limits = AhubLimits() if limits is None else limits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = self._open_connection()
        self._initialize_schema()
        self._restrict_permissions()

    def _open_connection(self) -> sqlite3.Connection:
        retryable = ("database is locked", "database is busy", "disk i/o error")
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(20):
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=10,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout = 10000")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                return connection
            except sqlite3.OperationalError as exc:
                connection.close()
                if not any(item in str(exc).lower() for item in retryable):
                    raise
                last_error = exc
                time.sleep(min(0.02 * (attempt + 1), 0.2))
        assert last_error is not None
        raise last_error

    def _initialize_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, 2, 3, 4, 5, AHUB_DB_VERSION}:
            raise ValueError(f"unsupported Ahub database version: {version}")
        if version == AHUB_DB_VERSION:
            return
        if version == 1:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE ahub_allowlist
                    ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1));
                ALTER TABLE ahub_allowlist
                    ADD COLUMN disabled_ms INTEGER;
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
            version = 2
        if version == 2:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE ahub_settlements (
                    packet_id TEXT PRIMARY KEY,
                    destination_id TEXT NOT NULL,
                    uploader_id TEXT NOT NULL,
                    raw_sha256 BLOB NOT NULL CHECK(length(raw_sha256) = 32),
                    settled_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    FOREIGN KEY(destination_id)
                        REFERENCES ahub_allowlist(node_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(uploader_id)
                        REFERENCES ahub_allowlist(node_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX ahub_settlements_uploader
                    ON ahub_settlements(
                        uploader_id, destination_id, settled_ms, packet_id
                    );
                CREATE INDEX ahub_settlements_expiry
                    ON ahub_settlements(expires_ms);
                PRAGMA user_version = 3;
                COMMIT;
                """
            )
            version = 3
        if version == 3:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE ahub_settlements
                    ADD COLUMN proof_json TEXT;
                PRAGMA user_version = 4;
                COMMIT;
                """
            )
            version = 4
        if version == 4:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE ahub_settlements
                    ADD COLUMN acknowledged_ms INTEGER NOT NULL DEFAULT 0;
                PRAGMA user_version = 5;
                COMMIT;
                """
            )
            version = 5
        if version == 5:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE ahub_relay_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    allowed_peer_id TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER NOT NULL,
                    max_duration_ms INTEGER NOT NULL,
                    max_bytes_each_direction INTEGER NOT NULL,
                    UNIQUE(owner_id, allowed_peer_id),
                    CHECK(owner_id <> allowed_peer_id),
                    CHECK(expires_ms > created_ms),
                    CHECK(max_duration_ms > 0),
                    CHECK(max_bytes_each_direction > 0),
                    FOREIGN KEY(owner_id)
                        REFERENCES ahub_allowlist(node_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(allowed_peer_id)
                        REFERENCES ahub_allowlist(node_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX ahub_relay_reservations_expiry
                    ON ahub_relay_reservations(expires_ms);
                CREATE INDEX ahub_relay_reservations_peer
                    ON ahub_relay_reservations(
                        allowed_peer_id, expires_ms
                    );
                PRAGMA user_version = 6;
                COMMIT;
                """
            )
            return
        self.connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE ahub_allowlist (
                node_id TEXT PRIMARY KEY,
                added_ms INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1)),
                disabled_ms INTEGER
            );
            CREATE TABLE ahub_nonces (
                node_id TEXT NOT NULL,
                nonce TEXT NOT NULL,
                issued_ms INTEGER NOT NULL,
                PRIMARY KEY(node_id, nonce),
                FOREIGN KEY(node_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE ahub_mailbox (
                packet_id TEXT PRIMARY KEY,
                destination_id TEXT NOT NULL,
                uploader_id TEXT NOT NULL,
                raw BLOB NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                created_ms INTEGER NOT NULL,
                expires_ms INTEGER NOT NULL,
                qos TEXT NOT NULL,
                depth INTEGER NOT NULL CHECK(depth >= 0),
                claim_token_hash BLOB,
                claim_until_ms INTEGER,
                FOREIGN KEY(destination_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(uploader_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX ahub_mailbox_destination
                ON ahub_mailbox(destination_id, created_ms, packet_id);
            CREATE INDEX ahub_mailbox_uploader
                ON ahub_mailbox(uploader_id);
            CREATE INDEX ahub_mailbox_expiry
                ON ahub_mailbox(expires_ms);
            CREATE INDEX ahub_nonce_expiry
                ON ahub_nonces(issued_ms);
            CREATE TABLE ahub_settlements (
                packet_id TEXT PRIMARY KEY,
                destination_id TEXT NOT NULL,
                uploader_id TEXT NOT NULL,
                raw_sha256 BLOB NOT NULL CHECK(length(raw_sha256) = 32),
                settled_ms INTEGER NOT NULL,
                expires_ms INTEGER NOT NULL,
                proof_json TEXT NOT NULL,
                acknowledged_ms INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(destination_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(uploader_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX ahub_settlements_uploader
                ON ahub_settlements(
                    uploader_id, destination_id, settled_ms, packet_id
                );
            CREATE INDEX ahub_settlements_expiry
                ON ahub_settlements(expires_ms);
            CREATE TABLE ahub_relay_reservations (
                reservation_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                allowed_peer_id TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                expires_ms INTEGER NOT NULL,
                max_duration_ms INTEGER NOT NULL,
                max_bytes_each_direction INTEGER NOT NULL,
                UNIQUE(owner_id, allowed_peer_id),
                CHECK(owner_id <> allowed_peer_id),
                CHECK(expires_ms > created_ms),
                CHECK(max_duration_ms > 0),
                CHECK(max_bytes_each_direction > 0),
                FOREIGN KEY(owner_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(allowed_peer_id)
                    REFERENCES ahub_allowlist(node_id)
                    ON DELETE RESTRICT
            );
            CREATE INDEX ahub_relay_reservations_expiry
                ON ahub_relay_reservations(expires_ms);
            CREATE INDEX ahub_relay_reservations_peer
                ON ahub_relay_reservations(
                    allowed_peer_id, expires_ms
                );
            PRAGMA user_version = 6;
            COMMIT;
            """
        )

    def _restrict_permissions(self) -> None:
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.execute("COMMIT")
        self._restrict_permissions()

    def _rollback(self) -> None:
        self.connection.execute("ROLLBACK")

    def allow_node(self, node_id: str, *, now: int | None = None) -> bool:
        current = _now_ms() if now is None else now
        validated = _validate_node_id(node_id)
        self._begin()
        try:
            row = self.connection.execute(
                """
                SELECT enabled
                FROM ahub_allowlist
                WHERE node_id = ?
                """,
                (validated,),
            ).fetchone()
            if row is not None and bool(row["enabled"]):
                self._commit()
                return False
            self.connection.execute(
                """
                INSERT INTO ahub_allowlist(
                    node_id, added_ms, enabled, disabled_ms
                ) VALUES (?, ?, 1, NULL)
                ON CONFLICT(node_id) DO UPDATE SET
                    enabled = 1,
                    disabled_ms = NULL
                """,
                (validated, current),
            )
            self._commit()
            return True
        except Exception:
            self._rollback()
            raise

    def disallow_node(self, node_id: str, *, now: int | None = None) -> bool:
        current = _now_ms() if now is None else now
        validated = _validate_node_id(node_id)
        self._begin()
        try:
            cursor = self.connection.execute(
                """
                UPDATE ahub_allowlist
                SET enabled = 0, disabled_ms = ?
                WHERE node_id = ? AND enabled = 1
                """,
                (current, validated),
            )
            self._commit()
            return cursor.rowcount == 1
        except Exception:
            self._rollback()
            raise

    def allowed_nodes(self, *, include_disabled: bool = False) -> tuple[dict[str, Any], ...]:
        where = "" if include_disabled else "WHERE enabled = 1"
        rows = self.connection.execute(
            f"""
            SELECT node_id, added_ms, enabled, disabled_ms
            FROM ahub_allowlist
            {where}
            ORDER BY node_id
            """
        ).fetchall()
        return tuple(
            {
                "node_id": str(row["node_id"]),
                "added_ms": int(row["added_ms"]),
                "enabled": bool(row["enabled"]),
                "disabled_ms": (
                    None
                    if row["disabled_ms"] is None
                    else int(row["disabled_ms"])
                ),
            }
            for row in rows
        )

    def is_allowed(self, node_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM ahub_allowlist
            WHERE node_id = ? AND enabled = 1
            """,
            (_validate_node_id(node_id),),
        ).fetchone()
        return row is not None

    def _require_allowed(self, node_id: str) -> None:
        if not self.is_allowed(node_id):
            raise PermissionError("node is not allowed by this Ahub")

    def _purge(self, *, now: int) -> dict[str, int]:
        expired_packets = self.connection.execute(
            "DELETE FROM ahub_mailbox WHERE expires_ms <= ?",
            (now,),
        ).rowcount
        old_nonces = self.connection.execute(
            "DELETE FROM ahub_nonces WHERE issued_ms < ?",
            (now - AHUB_NONCE_RETENTION_MS,),
        ).rowcount
        expired_claims = self.connection.execute(
            """
            UPDATE ahub_mailbox
            SET claim_token_hash = NULL, claim_until_ms = NULL
            WHERE claim_until_ms IS NOT NULL AND claim_until_ms <= ?
            """,
            (now,),
        ).rowcount
        expired_settlements = self.connection.execute(
            "DELETE FROM ahub_settlements WHERE expires_ms <= ?",
            (now,),
        ).rowcount
        expired_relay_reservations = self.connection.execute(
            "DELETE FROM ahub_relay_reservations WHERE expires_ms <= ?",
            (now,),
        ).rowcount
        return {
            "expired_packets": expired_packets,
            "old_nonces": old_nonces,
            "expired_claims": expired_claims,
            "expired_settlements": expired_settlements,
            "expired_relay_reservations": expired_relay_reservations,
        }

    def purge(self, *, now: int | None = None) -> dict[str, int]:
        current = _now_ms() if now is None else now
        self._begin()
        try:
            result = self._purge(now=current)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def status(self, *, now: int | None = None) -> dict[str, Any]:
        current = _now_ms() if now is None else now
        allowlist = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count,
                SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) AS disabled_count
            FROM ahub_allowlist
            """
        ).fetchone()
        mailbox = self.connection.execute(
            """
            SELECT
                COUNT(*) AS packet_count,
                COALESCE(SUM(size_bytes), 0) AS byte_count,
                SUM(CASE WHEN expires_ms <= ? THEN 1 ELSE 0 END) AS expired_count,
                SUM(
                    CASE
                        WHEN claim_until_ms > ? THEN 1
                        ELSE 0
                    END
                ) AS active_claim_count,
                MIN(created_ms) AS oldest_created_ms
            FROM ahub_mailbox
            """,
            (current, current),
        ).fetchone()
        nonces = self.connection.execute(
            "SELECT COUNT(*) AS nonce_count FROM ahub_nonces"
        ).fetchone()
        settlements = self.connection.execute(
            "SELECT COUNT(*) AS settlement_count FROM ahub_settlements"
        ).fetchone()
        relay = self.connection.execute(
            """
            SELECT
                COUNT(*) AS reservation_count,
                SUM(CASE WHEN expires_ms <= ? THEN 1 ELSE 0 END)
                    AS expired_count
            FROM ahub_relay_reservations
            """,
            (current,),
        ).fetchone()
        return {
            "database_version": AHUB_DB_VERSION,
            "enabled_nodes": int(allowlist["enabled_count"] or 0),
            "disabled_nodes": int(allowlist["disabled_count"] or 0),
            "mailbox_packets": int(mailbox["packet_count"]),
            "mailbox_bytes": int(mailbox["byte_count"]),
            "expired_packets": int(mailbox["expired_count"] or 0),
            "active_claims": int(mailbox["active_claim_count"] or 0),
            "oldest_packet_age_ms": (
                None
                if mailbox["oldest_created_ms"] is None
                else max(0, current - int(mailbox["oldest_created_ms"]))
            ),
            "retained_nonces": int(nonces["nonce_count"]),
            "retained_settlements": int(settlements["settlement_count"]),
            "relay_reservations": int(relay["reservation_count"]),
            "expired_relay_reservations": int(relay["expired_count"] or 0),
        }

    @staticmethod
    def _relay_reservation_from_row(row: sqlite3.Row) -> RelayReservation:
        return RelayReservation(
            reservation_id=str(row["reservation_id"]),
            owner_id=str(row["owner_id"]),
            allowed_peer_id=str(row["allowed_peer_id"]),
            created_ms=int(row["created_ms"]),
            expires_ms=int(row["expires_ms"]),
            max_duration_ms=int(row["max_duration_ms"]),
            max_bytes_each_direction=int(
                row["max_bytes_each_direction"]
            ),
        )

    def reserve_relay(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        allowed_peer_id: str,
        ttl_ms: int,
        max_duration_ms: int,
        max_bytes_each_direction: int,
        now: int | None = None,
    ) -> RelayReservation:
        current = _now_ms() if now is None else now
        peer_id = _validate_node_id(allowed_peer_id)
        values = (ttl_ms, max_duration_ms, max_bytes_each_direction)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in values
        ):
            raise ValueError("Relay reservation limits must be integers")
        if not (
            MIN_RELAY_RESERVATION_TTL_MS
            <= ttl_ms
            <= self.limits.max_relay_reservation_ttl_ms
        ):
            raise ValueError("Relay reservation TTL is outside allowed limits")
        if not (
            MIN_RELAY_DURATION_MS
            <= max_duration_ms
            <= self.limits.max_relay_duration_ms
        ):
            raise ValueError("Relay duration is outside allowed limits")
        if not (
            1
            <= max_bytes_each_direction
            <= self.limits.max_relay_bytes_each_direction
        ):
            raise ValueError("Relay byte limit is outside allowed limits")
        if peer_id == request.node_id:
            raise ValueError("Relay reservation cannot target its owner")
        expires_ms = current + ttl_ms
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            self._require_allowed(peer_id)
            existing = self.connection.execute(
                """
                SELECT *
                FROM ahub_relay_reservations
                WHERE owner_id = ? AND allowed_peer_id = ?
                """,
                (request.node_id, peer_id),
            ).fetchone()
            if existing is None:
                count = int(
                    self.connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM ahub_relay_reservations
                        WHERE owner_id = ?
                        """,
                        (request.node_id,),
                    ).fetchone()[0]
                )
                if count >= self.limits.max_relay_reservations_per_node:
                    raise OverflowError("Relay reservation quota exceeded")
                reservation_id = secrets.token_hex(16)
                created_ms = current
                self.connection.execute(
                    """
                    INSERT INTO ahub_relay_reservations(
                        reservation_id,
                        owner_id,
                        allowed_peer_id,
                        created_ms,
                        expires_ms,
                        max_duration_ms,
                        max_bytes_each_direction
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        request.node_id,
                        peer_id,
                        created_ms,
                        expires_ms,
                        max_duration_ms,
                        max_bytes_each_direction,
                    ),
                )
            else:
                reservation_id = str(existing["reservation_id"])
                created_ms = int(existing["created_ms"])
                self.connection.execute(
                    """
                    UPDATE ahub_relay_reservations
                    SET
                        expires_ms = ?,
                        max_duration_ms = ?,
                        max_bytes_each_direction = ?
                    WHERE reservation_id = ?
                    """,
                    (
                        expires_ms,
                        max_duration_ms,
                        max_bytes_each_direction,
                        reservation_id,
                    ),
                )
            row = self.connection.execute(
                """
                SELECT *
                FROM ahub_relay_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            assert row is not None
            result = self._relay_reservation_from_row(row)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def authorize_relay(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        reservation_id: str,
        now: int | None = None,
    ) -> RelayReservation:
        current = _now_ms() if now is None else now
        value = str(reservation_id)
        if not _RELAY_RESERVATION_ID_RE.fullmatch(value):
            raise ValueError("invalid Relay reservation ID")
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            row = self.connection.execute(
                """
                SELECT *
                FROM ahub_relay_reservations
                WHERE reservation_id = ?
                """,
                (value,),
            ).fetchone()
            if row is None:
                raise LookupError("Relay reservation not found")
            result = self._relay_reservation_from_row(row)
            if request.node_id not in {
                result.owner_id,
                result.allowed_peer_id,
            }:
                raise PermissionError("node is not authorized for Relay reservation")
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def find_relay_reservation(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        owner_id: str,
        now: int | None = None,
    ) -> RelayReservation:
        current = _now_ms() if now is None else now
        owner = _validate_node_id(owner_id)
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            self._require_allowed(owner)
            row = self.connection.execute(
                """
                SELECT *
                FROM ahub_relay_reservations
                WHERE owner_id = ? AND allowed_peer_id = ?
                """,
                (owner, request.node_id),
            ).fetchone()
            if row is None:
                raise LookupError("matching Relay reservation not found")
            result = self._relay_reservation_from_row(row)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def health(self) -> bool:
        row = self.connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def _consume_request(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        now: int,
    ) -> None:
        request.verify(descriptor, body, now=now)
        self._require_allowed(request.node_id)
        try:
            self.connection.execute(
                """
                INSERT INTO ahub_nonces(node_id, nonce, issued_ms)
                VALUES (?, ?, ?)
                """,
                (request.node_id, request.nonce, request.issued_ms),
            )
        except sqlite3.IntegrityError as exc:
            raise PermissionError("Ahub request nonce was already used") from exc

    def authenticate(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        now: int | None = None,
    ) -> None:
        current = _now_ms() if now is None else now
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            self._commit()
        except Exception:
            self._rollback()
            raise

    def submit(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        raw: bytes,
        *,
        now: int | None = None,
    ) -> CustodyReceipt:
        current = _now_ms() if now is None else now
        packet = inspect_packet(bytes(raw))
        if packet.expires_ms <= current:
            raise ValueError("packet expired")
        if len(packet.raw) > self.limits.max_packet_bytes:
            raise ValueError("packet exceeds this Ahub's size limit")
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, packet.raw, now=current)
            self._require_allowed(packet.destination_id)
            existing = self.connection.execute(
                """
                SELECT raw, destination_id, expires_ms
                FROM ahub_mailbox
                WHERE packet_id = ?
                """,
                (packet.packet_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["raw"]), packet.raw):
                    raise ValueError("packet id conflicts with different bytes")
                receipt = CustodyReceipt(
                    packet_id=packet.packet_id,
                    destination_id=str(existing["destination_id"]),
                    stored=False,
                    expires_ms=int(existing["expires_ms"]),
                )
                self._commit()
                return receipt
            settled = self.connection.execute(
                """
                SELECT destination_id, uploader_id, raw_sha256, expires_ms
                FROM ahub_settlements
                WHERE packet_id = ?
                """,
                (packet.packet_id,),
            ).fetchone()
            if settled is not None:
                if (
                    str(settled["destination_id"]) != packet.destination_id
                    or str(settled["uploader_id"]) != request.node_id
                    or not hmac.compare_digest(
                        bytes(settled["raw_sha256"]),
                        hashlib.sha256(packet.raw).digest(),
                    )
                ):
                    raise ValueError("settled packet id conflicts with new upload")
                receipt = CustodyReceipt(
                    packet_id=packet.packet_id,
                    destination_id=packet.destination_id,
                    stored=False,
                    expires_ms=int(settled["expires_ms"]),
                )
                self._commit()
                return receipt
            self._check_quota(packet, uploader_id=request.node_id)
            self.connection.execute(
                """
                INSERT INTO ahub_mailbox(
                    packet_id, destination_id, uploader_id, raw, size_bytes,
                    created_ms, expires_ms, qos, depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    packet.packet_id,
                    packet.destination_id,
                    request.node_id,
                    packet.raw,
                    len(packet.raw),
                    packet.created_ms,
                    packet.expires_ms,
                    packet.qos,
                ),
            )
            receipt = CustodyReceipt(
                packet_id=packet.packet_id,
                destination_id=packet.destination_id,
                stored=True,
                expires_ms=packet.expires_ms,
            )
            self._commit()
            return receipt
        except Exception:
            self._rollback()
            raise

    def _check_quota(self, packet: PacketInfo, *, uploader_id: str) -> None:
        destination = self.connection.execute(
            """
            SELECT COUNT(*) AS packet_count,
                   COALESCE(SUM(size_bytes), 0) AS byte_count
            FROM ahub_mailbox
            WHERE destination_id = ?
            """,
            (packet.destination_id,),
        ).fetchone()
        uploader = self.connection.execute(
            """
            SELECT COUNT(*) AS packet_count,
                   COALESCE(SUM(size_bytes), 0) AS byte_count
            FROM ahub_mailbox
            WHERE uploader_id = ?
            """,
            (uploader_id,),
        ).fetchone()
        if (
            int(destination["packet_count"])
            >= self.limits.max_packets_per_destination
            or int(destination["byte_count"]) + len(packet.raw)
            > self.limits.max_bytes_per_destination
        ):
            raise OverflowError("destination mailbox quota exceeded")
        if (
            int(uploader["packet_count"]) >= self.limits.max_packets_per_uploader
            or int(uploader["byte_count"]) + len(packet.raw)
            > self.limits.max_bytes_per_uploader
        ):
            raise OverflowError("uploader mailbox quota exceeded")

    def claim(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        limit: int,
        lease_ms: int,
        max_bytes: int | None = None,
        uploader_id: str | None = None,
        now: int | None = None,
    ) -> tuple[ClaimedPacket, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_CLAIM_BATCH
        ):
            raise ValueError("invalid mailbox claim limit")
        if (
            isinstance(lease_ms, bool)
            or not isinstance(lease_ms, int)
            or not MIN_CLAIM_LEASE_MS <= lease_ms <= MAX_CLAIM_LEASE_MS
        ):
            raise ValueError("invalid mailbox claim lease")
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("invalid mailbox claim byte limit")
        uploader = (
            None if uploader_id is None else _validate_node_id(uploader_id)
        )
        current = _now_ms() if now is None else now
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            if uploader is None:
                rows = self.connection.execute(
                    """
                    SELECT packet_id, uploader_id, size_bytes, depth, expires_ms
                    FROM ahub_mailbox
                    WHERE destination_id = ?
                      AND claim_token_hash IS NULL
                    ORDER BY created_ms, packet_id
                    LIMIT ?
                    """,
                    (request.node_id, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT packet_id, uploader_id, size_bytes, depth, expires_ms
                    FROM ahub_mailbox
                    WHERE destination_id = ?
                      AND uploader_id = ?
                      AND claim_token_hash IS NULL
                    ORDER BY created_ms, packet_id
                    LIMIT ?
                    """,
                    (request.node_id, uploader, limit),
                ).fetchall()
            claimed: list[ClaimedPacket] = []
            claim_until = current + lease_ms
            claimed_bytes = 0
            for row in rows:
                size_bytes = int(row["size_bytes"])
                if (
                    max_bytes is not None
                    and claimed_bytes + size_bytes > max_bytes
                ):
                    continue
                packet_row = self.connection.execute(
                    """
                    SELECT raw
                    FROM ahub_mailbox
                    WHERE packet_id = ? AND claim_token_hash IS NULL
                    """,
                    (str(row["packet_id"]),),
                ).fetchone()
                if packet_row is None:
                    continue
                token = secrets.token_urlsafe(32)
                self.connection.execute(
                    """
                    UPDATE ahub_mailbox
                    SET claim_token_hash = ?, claim_until_ms = ?
                    WHERE packet_id = ? AND claim_token_hash IS NULL
                    """,
                    (
                        hashlib.sha256(token.encode("ascii")).digest(),
                        claim_until,
                        str(row["packet_id"]),
                    ),
                )
                claimed.append(
                        ClaimedPacket(
                            packet_id=str(row["packet_id"]),
                            raw=bytes(packet_row["raw"]),
                        depth=int(row["depth"]),
                            claim_token=token,
                            claim_until_ms=claim_until,
                            uploader_id=str(row["uploader_id"]),
                            expires_ms=int(row["expires_ms"]),
                    )
                )
                claimed_bytes += size_bytes
            self._commit()
            return tuple(claimed)
        except Exception:
            self._rollback()
            raise

    def settle(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        packet_id: str,
        claim_token: str,
        proof: DestinationSettlement,
        now: int | None = None,
    ) -> bool:
        current = _now_ms() if now is None else now
        token_hash = hashlib.sha256(str(claim_token).encode("ascii")).digest()
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            row = self.connection.execute(
                """
                SELECT destination_id, uploader_id, raw, expires_ms,
                       claim_token_hash, claim_until_ms
                FROM ahub_mailbox
                WHERE packet_id = ?
                """,
                (str(packet_id),),
            ).fetchone()
            if row is None:
                self._commit()
                return False
            if str(row["destination_id"]) != request.node_id:
                raise PermissionError("only the destination can settle a packet")
            stored_hash = row["claim_token_hash"]
            if (
                stored_hash is None
                or row["claim_until_ms"] is None
                or int(row["claim_until_ms"]) <= current
                or not hmac.compare_digest(bytes(stored_hash), token_hash)
            ):
                raise PermissionError("invalid or expired mailbox claim")
            proof.verify(descriptor.sign_public, now=current)
            if (
                proof.packet_id != str(packet_id)
                or proof.destination_id != request.node_id
                or proof.uploader_id != str(row["uploader_id"])
                or proof.expires_ms != int(row["expires_ms"])
                or not hmac.compare_digest(
                    proof.packet_sha256,
                    hashlib.sha256(bytes(row["raw"])).digest(),
                )
            ):
                raise ValueError("destination settlement does not match claim")
            self.connection.execute(
                """
                INSERT INTO ahub_settlements(
                    packet_id, destination_id, uploader_id, raw_sha256,
                    settled_ms, expires_ms, proof_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(packet_id) DO NOTHING
                """,
                (
                    str(packet_id),
                    str(row["destination_id"]),
                    str(row["uploader_id"]),
                    hashlib.sha256(bytes(row["raw"])).digest(),
                    proof.settled_ms,
                    int(row["expires_ms"]),
                    json.dumps(
                        proof.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            self.connection.execute(
                "DELETE FROM ahub_mailbox WHERE packet_id = ?",
                (str(packet_id),),
            )
            self._commit()
            return True
        except Exception:
            self._rollback()
            raise

    def settlements(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        limit: int,
        destination_id: str | None = None,
        now: int | None = None,
    ) -> tuple[DestinationSettlement, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_CLAIM_BATCH
        ):
            raise ValueError("invalid settlement limit")
        destination = (
            None
            if destination_id is None
            else _validate_node_id(destination_id)
        )
        current = _now_ms() if now is None else now
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            if destination is None:
                rows = self.connection.execute(
                    """
                    SELECT proof_json
                    FROM ahub_settlements
                    WHERE uploader_id = ?
                      AND proof_json IS NOT NULL
                      AND acknowledged_ms = 0
                    ORDER BY settled_ms, packet_id
                    LIMIT ?
                    """,
                    (request.node_id, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT proof_json
                    FROM ahub_settlements
                    WHERE uploader_id = ? AND destination_id = ?
                      AND proof_json IS NOT NULL
                      AND acknowledged_ms = 0
                    ORDER BY settled_ms, packet_id
                    LIMIT ?
                    """,
                    (request.node_id, destination, limit),
                ).fetchall()
            result = tuple(
                DestinationSettlement.from_dict(
                    json.loads(str(row["proof_json"]))
                )
                for row in rows
            )
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def acknowledge_settlement(
        self,
        request: AhubRequest,
        descriptor: NodeDescriptor,
        body: bytes,
        *,
        packet_id: str,
        now: int | None = None,
    ) -> bool:
        if not _PACKET_ID_RE.fullmatch(str(packet_id)):
            raise ValueError("invalid settlement acknowledgement packet id")
        current = _now_ms() if now is None else now
        self._begin()
        try:
            self._purge(now=current)
            self._consume_request(request, descriptor, body, now=current)
            row = self.connection.execute(
                """
                SELECT uploader_id, acknowledged_ms
                FROM ahub_settlements
                WHERE packet_id = ?
                """,
                (str(packet_id),),
            ).fetchone()
            if row is None:
                self._commit()
                return False
            if str(row["uploader_id"]) != request.node_id:
                raise PermissionError(
                    "only the uploader can acknowledge a settlement"
                )
            changed = int(row["acknowledged_ms"]) == 0
            if changed:
                self.connection.execute(
                    """
                    UPDATE ahub_settlements
                    SET acknowledged_ms = ?
                    WHERE packet_id = ?
                    """,
                    (current, str(packet_id)),
                )
            self._commit()
            return changed
        except Exception:
            self._rollback()
            raise

    def mailbox_count(self, node_id: str, *, now: int | None = None) -> int:
        current = _now_ms() if now is None else now
        self._begin()
        try:
            self._purge(now=current)
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS packet_count
                FROM ahub_mailbox
                WHERE destination_id = ?
                """,
                (_validate_node_id(node_id),),
            ).fetchone()
            self._commit()
            return int(row["packet_count"])
        except Exception:
            self._rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "AhubStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class AhubService:
    """No-private-key Rendezvous and Mailbox composition root."""

    def __init__(
        self,
        root: Path,
        *,
        limits: AhubLimits | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.control = ControlPlaneStore(self.root / "control.sqlite3")
        self.ahub = AhubStore(self.root / "ahub.sqlite3", limits=limits)

    def allow_node(self, node_id: str, *, now: int | None = None) -> bool:
        return self.ahub.allow_node(node_id, now=now)

    def disallow_node(self, node_id: str, *, now: int | None = None) -> bool:
        return self.ahub.disallow_node(node_id, now=now)

    def publish_descriptor(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> bool:
        self.ahub._require_allowed(descriptor.node_id)
        return self.control.accept_descriptor(descriptor, now=now)

    def publish_reachability(
        self,
        record: ReachabilityRecord,
        *,
        now: int | None = None,
    ) -> bool:
        self.ahub._require_allowed(record.node_id)
        descriptor = self.control.current_descriptor(record.node_id, now=now)
        if descriptor is None:
            raise ValueError("reachability requires a current descriptor")
        return self.control.accept_reachability(record, descriptor, now=now)

    def authenticate(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        now: int | None = None,
    ) -> NodeDescriptor:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        self.ahub.authenticate(request, descriptor, body, now=now)
        return descriptor

    def lookup(
        self,
        request: AhubRequest,
        body: bytes,
        target_node_id: str,
        *,
        now: int | None = None,
    ) -> tuple[NodeDescriptor, ReachabilityRecord | None]:
        self.authenticate(request, body, now=now)
        self.ahub._require_allowed(target_node_id)
        descriptor = self.control.current_descriptor(target_node_id, now=now)
        if descriptor is None:
            raise LookupError("target has no current descriptor")
        return descriptor, self.control.current_reachability(
            target_node_id, now=now
        )

    def reserve_relay(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        allowed_peer_id: str,
        ttl_ms: int,
        max_duration_ms: int,
        max_bytes_each_direction: int,
        now: int | None = None,
    ) -> RelayReservation:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.reserve_relay(
            request,
            descriptor,
            body,
            allowed_peer_id=allowed_peer_id,
            ttl_ms=ttl_ms,
            max_duration_ms=max_duration_ms,
            max_bytes_each_direction=max_bytes_each_direction,
            now=now,
        )

    def authorize_relay(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        reservation_id: str,
        now: int | None = None,
    ) -> RelayReservation:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.authorize_relay(
            request,
            descriptor,
            body,
            reservation_id=reservation_id,
            now=now,
        )

    def find_relay_reservation(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        owner_id: str,
        now: int | None = None,
    ) -> RelayReservation:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.find_relay_reservation(
            request,
            descriptor,
            body,
            owner_id=owner_id,
            now=now,
        )

    def submit(
        self,
        request: AhubRequest,
        raw: bytes,
        *,
        now: int | None = None,
    ) -> CustodyReceipt:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.submit(request, descriptor, raw, now=now)

    def claim(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        limit: int,
        lease_ms: int,
        max_bytes: int | None = None,
        uploader_id: str | None = None,
        now: int | None = None,
    ) -> tuple[ClaimedPacket, ...]:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.claim(
            request,
            descriptor,
            body,
            limit=limit,
            lease_ms=lease_ms,
            max_bytes=max_bytes,
            uploader_id=uploader_id,
            now=now,
        )

    def settle(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        packet_id: str,
        claim_token: str,
        proof: DestinationSettlement,
        now: int | None = None,
    ) -> bool:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.settle(
            request,
            descriptor,
            body,
            packet_id=packet_id,
            claim_token=claim_token,
            proof=proof,
            now=now,
        )

    def settlements(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        limit: int,
        destination_id: str | None = None,
        now: int | None = None,
    ) -> tuple[DestinationSettlement, ...]:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.settlements(
            request,
            descriptor,
            body,
            limit=limit,
            destination_id=destination_id,
            now=now,
        )

    def acknowledge_settlement(
        self,
        request: AhubRequest,
        body: bytes,
        *,
        packet_id: str,
        now: int | None = None,
    ) -> bool:
        descriptor = self.control.current_descriptor(request.node_id, now=now)
        if descriptor is None:
            raise PermissionError("Ahub caller has no current descriptor")
        return self.ahub.acknowledge_settlement(
            request,
            descriptor,
            body,
            packet_id=packet_id,
            now=now,
        )

    def status(self, *, now: int | None = None) -> dict[str, Any]:
        current = _now_ms() if now is None else now
        control = self.control.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM control_node_descriptors)
                    AS descriptor_count,
                (
                    SELECT COUNT(*)
                    FROM control_reachability
                    WHERE expires_ms > ?
                ) AS live_reachability_count,
                (
                    SELECT COUNT(*)
                    FROM control_reachability
                    WHERE expires_ms <= ?
                ) AS expired_reachability_count
            """,
            (current, current),
        ).fetchone()
        return {
            **self.ahub.status(now=current),
            "descriptors": int(control["descriptor_count"]),
            "live_reachability": int(control["live_reachability_count"]),
            "expired_reachability": int(
                control["expired_reachability_count"]
            ),
        }

    def health(self) -> bool:
        control = self.control.connection.execute("PRAGMA quick_check").fetchone()
        return (
            self.ahub.health()
            and bool(control)
            and str(control[0]).lower() == "ok"
        )

    def checkpoint(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for name, connection in (
            ("ahub", self.ahub.connection),
            ("control", self.control.connection),
        ):
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result[name] = {
                "busy": int(row[0]),
                "wal_pages": int(row[1]),
                "checkpointed_pages": int(row[2]),
            }
        self.ahub._restrict_permissions()
        self.control._restrict_permissions()
        return result

    def close(self) -> None:
        self.ahub.close()
        self.control.close()

    def __enter__(self) -> "AhubService":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
