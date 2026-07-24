from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .encoding import b64d, b64e, canonical_pack
from .identity import Identity, derive_node_id
from .locator import parse_locator


NODE_DESCRIPTOR_VERSION = 2
REACHABILITY_VERSION = 1
HUMAN_DEVICE_GRANT_VERSION = 1
HUMAN_DEVICE_REVOCATION_VERSION = 1

NODE_DESCRIPTOR_TYPE = "anet.node-descriptor.v2"
REACHABILITY_TYPE = "anet.reachability.v1"
HUMAN_DEVICE_GRANT_TYPE = "anet.human-device-grant.v1"
HUMAN_DEVICE_REVOCATION_TYPE = "anet.human-device-revocation.v1"

GENESIS_DIGEST = bytes(32)
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_DESCRIPTOR_TTL_MS = 366 * 24 * 60 * 60 * 1000
MAX_REACHABILITY_TTL_MS = 15 * 60 * 1000
MAX_HUMAN_GRANT_TTL_MS = 90 * 24 * 60 * 60 * 1000
MAX_CAPABILITIES = 64
MAX_CANDIDATES = 16
MAX_PROTOCOL_VERSIONS = 16

_NODE_ID_RE = re.compile(r"^an1[a-z2-7]{32}$")
_HUMAN_ID_RE = re.compile(r"^hu1[a-z2-7]{32}$")
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,95}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _require_exact_fields(
    value: dict[str, Any], *, required: set[str], optional: set[str] = frozenset()
) -> None:
    if not isinstance(value, dict):
        raise ValueError("control-plane object must be a map")
    missing = required - set(value)
    if missing:
        raise ValueError(
            f"control-plane object is missing fields: {', '.join(sorted(missing))}"
        )
    unknown = set(value) - required - optional
    if unknown:
        raise ValueError(
            f"control-plane object has unknown fields: {', '.join(sorted(unknown))}"
        )


def _validate_bytes(raw: bytes, *, size: int, name: str) -> bytes:
    value = bytes(raw)
    if len(value) != size:
        raise ValueError(f"{name} must be {size} bytes")
    return value


def _decode_bytes(text: Any, *, size: int, name: str) -> bytes:
    try:
        raw = b64d(str(text))
    except Exception as exc:
        raise ValueError(f"invalid {name}") from exc
    return _validate_bytes(raw, size=size, name=name)


def _parse_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _validate_revision(sequence: int, previous_digest: bytes) -> None:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    _validate_bytes(previous_digest, size=32, name="previous digest")
    if sequence == 1 and previous_digest != GENESIS_DIGEST:
        raise ValueError("genesis revision must use the genesis digest")
    if sequence > 1 and previous_digest == GENESIS_DIGEST:
        raise ValueError("non-genesis revision must name its predecessor")


def _validate_window(
    issued_ms: int,
    expires_ms: int,
    *,
    now: int,
    max_ttl_ms: int,
    object_name: str,
) -> None:
    if (
        isinstance(issued_ms, bool)
        or isinstance(expires_ms, bool)
        or not isinstance(issued_ms, int)
        or not isinstance(expires_ms, int)
        or issued_ms <= 0
        or expires_ms <= issued_ms
    ):
        raise ValueError(f"invalid {object_name} validity window")
    if expires_ms - issued_ms > max_ttl_ms:
        raise ValueError(f"{object_name} validity window is too long")
    if issued_ms > now + MAX_CLOCK_SKEW_MS:
        raise ValueError(f"{object_name} was issued too far in the future")
    if expires_ms <= now:
        raise ValueError(f"{object_name} has expired")


def _validate_node_id(node_id: str) -> str:
    value = str(node_id)
    if not _NODE_ID_RE.fullmatch(value):
        raise ValueError("invalid Anet node id")
    return value


def _validate_human_id(human_id: str) -> str:
    value = str(human_id)
    if not _HUMAN_ID_RE.fullmatch(value):
        raise ValueError("invalid Anet human id")
    return value


def _validate_label(label: str) -> str:
    value = str(label)
    if len(value) > 64 or any(ord(character) < 32 for character in value):
        raise ValueError("invalid node label")
    return value


def _validate_capabilities(
    capabilities: tuple[str, ...], *, allow_empty: bool = False
) -> tuple[str, ...]:
    values = tuple(str(item) for item in capabilities)
    if (not values and not allow_empty) or len(values) > MAX_CAPABILITIES:
        raise ValueError("capability set has an invalid size")
    if values != tuple(sorted(set(values))):
        raise ValueError("capabilities must be unique and sorted")
    if any(not _CAPABILITY_RE.fullmatch(item) for item in values):
        raise ValueError("invalid capability token")
    return values


def _validate_protocol_versions(protocol_versions: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(str(item) for item in protocol_versions)
    if not values or len(values) > MAX_PROTOCOL_VERSIONS:
        raise ValueError("protocol version set must be non-empty and bounded")
    if values != tuple(sorted(set(values))):
        raise ValueError("protocol versions must be unique and sorted")
    if any(not _TOKEN_RE.fullmatch(item) for item in values):
        raise ValueError("invalid protocol version")
    return values


def _validate_candidates(candidates: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(str(item) for item in candidates)
    if len(values) > MAX_CANDIDATES:
        raise ValueError("too many reachability candidates")
    if values != tuple(sorted(set(values))):
        raise ValueError("reachability candidates must be unique and sorted")
    for candidate in values:
        parse_locator(candidate)
    return values


def _object_digest(signing_fields: list[Any], signature: bytes) -> bytes:
    return hashlib.sha256(canonical_pack([*signing_fields, signature])).digest()


def derive_human_id(sign_public: bytes) -> str:
    raw = _validate_bytes(sign_public, size=32, name="human signing public key")
    digest = hashlib.blake2s(raw, digest_size=20, person=b"anet-hum").digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"hu1{token}"


@dataclass(frozen=True)
class NodeDescriptor:
    node_id: str
    sign_public: bytes
    box_public: bytes
    label: str
    capabilities: tuple[str, ...]
    sequence: int
    previous_digest: bytes
    issued_ms: int
    expires_ms: int
    signature: bytes
    version: int = NODE_DESCRIPTOR_VERSION
    object_type: str = NODE_DESCRIPTOR_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.node_id,
            self.sign_public,
            self.box_public,
            self.label,
            list(self.capabilities),
            self.sequence,
            self.previous_digest,
            self.issued_ms,
            self.expires_ms,
        ]

    @property
    def digest(self) -> bytes:
        return _object_digest(self.signing_fields(), self.signature)

    def verify(self, *, now: int | None = None) -> None:
        if (
            self.version != NODE_DESCRIPTOR_VERSION
            or self.object_type != NODE_DESCRIPTOR_TYPE
        ):
            raise ValueError("unsupported node descriptor")
        _validate_node_id(self.node_id)
        _validate_bytes(self.sign_public, size=32, name="node signing public key")
        _validate_bytes(self.box_public, size=32, name="node box public key")
        if self.node_id != derive_node_id(self.sign_public, self.box_public):
            raise ValueError("node descriptor id does not match public keys")
        _validate_label(self.label)
        _validate_capabilities(self.capabilities, allow_empty=True)
        _validate_revision(self.sequence, self.previous_digest)
        _validate_window(
            self.issued_ms,
            self.expires_ms,
            now=_now_ms() if now is None else now,
            max_ttl_ms=MAX_DESCRIPTOR_TTL_MS,
            object_name="node descriptor",
        )
        _validate_bytes(self.signature, size=64, name="node descriptor signature")
        Ed25519PublicKey.from_public_bytes(self.sign_public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "object_type": self.object_type,
            "node_id": self.node_id,
            "sign_public": b64e(self.sign_public),
            "box_public": b64e(self.box_public),
            "label": self.label,
            "capabilities": list(self.capabilities),
            "sequence": self.sequence,
            "previous_digest": b64e(self.previous_digest),
            "issued_ms": self.issued_ms,
            "expires_ms": self.expires_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, now: int | None = None
    ) -> "NodeDescriptor":
        fields = {
            "version",
            "object_type",
            "node_id",
            "sign_public",
            "box_public",
            "label",
            "capabilities",
            "sequence",
            "previous_digest",
            "issued_ms",
            "expires_ms",
            "signature",
        }
        _require_exact_fields(value, required=fields)
        capabilities = value["capabilities"]
        if not isinstance(capabilities, list):
            raise ValueError("node descriptor capabilities must be a list")
        descriptor = cls(
            version=_parse_int(value["version"], name="node descriptor version"),
            object_type=str(value["object_type"]),
            node_id=str(value["node_id"]),
            sign_public=_decode_bytes(
                value["sign_public"], size=32, name="node signing public key"
            ),
            box_public=_decode_bytes(
                value["box_public"], size=32, name="node box public key"
            ),
            label=str(value["label"]),
            capabilities=tuple(str(item) for item in capabilities),
            sequence=_parse_int(value["sequence"], name="node descriptor sequence"),
            previous_digest=_decode_bytes(
                value["previous_digest"], size=32, name="previous digest"
            ),
            issued_ms=_parse_int(
                value["issued_ms"], name="node descriptor issued time"
            ),
            expires_ms=_parse_int(
                value["expires_ms"], name="node descriptor expiry time"
            ),
            signature=_decode_bytes(
                value["signature"], size=64, name="node descriptor signature"
            ),
        )
        descriptor.verify(now=now)
        return descriptor


def issue_node_descriptor(
    identity: Identity,
    *,
    capabilities: tuple[str, ...] | list[str],
    sequence: int = 1,
    previous_digest: bytes = GENESIS_DIGEST,
    issued_ms: int | None = None,
    ttl_ms: int = 30 * 24 * 60 * 60 * 1000,
) -> NodeDescriptor:
    issued = _now_ms() if issued_ms is None else issued_ms
    unsigned = NodeDescriptor(
        node_id=identity.node_id,
        sign_public=identity.sign_public,
        box_public=identity.box_public,
        label=identity.label,
        capabilities=tuple(sorted(set(str(item) for item in capabilities))),
        sequence=sequence,
        previous_digest=bytes(previous_digest),
        issued_ms=issued,
        expires_ms=issued + ttl_ms,
        signature=b"",
    )
    signature = identity.sign(canonical_pack(unsigned.signing_fields()))
    descriptor = NodeDescriptor(
        **{**unsigned.__dict__, "signature": signature}
    )
    descriptor.verify(now=issued)
    return descriptor


@dataclass(frozen=True)
class ReachabilityRecord:
    node_id: str
    descriptor_digest: bytes
    session_id: str
    protocol_versions: tuple[str, ...]
    candidates: tuple[str, ...]
    relay_reservation: str
    capability_digest: bytes
    sequence: int
    previous_digest: bytes
    issued_ms: int
    expires_ms: int
    signature: bytes
    version: int = REACHABILITY_VERSION
    object_type: str = REACHABILITY_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.node_id,
            self.descriptor_digest,
            self.session_id,
            list(self.protocol_versions),
            list(self.candidates),
            self.relay_reservation,
            self.capability_digest,
            self.sequence,
            self.previous_digest,
            self.issued_ms,
            self.expires_ms,
        ]

    @property
    def digest(self) -> bytes:
        return _object_digest(self.signing_fields(), self.signature)

    def verify(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> None:
        current = _now_ms() if now is None else now
        if (
            self.version != REACHABILITY_VERSION
            or self.object_type != REACHABILITY_TYPE
        ):
            raise ValueError("unsupported reachability record")
        descriptor.verify(now=current)
        _validate_node_id(self.node_id)
        if self.node_id != descriptor.node_id:
            raise ValueError("reachability record belongs to another node")
        _validate_bytes(
            self.descriptor_digest, size=32, name="descriptor digest"
        )
        if self.descriptor_digest != descriptor.digest:
            raise ValueError("reachability record uses a stale descriptor")
        if not _SESSION_ID_RE.fullmatch(self.session_id):
            raise ValueError("invalid reachability session id")
        _validate_protocol_versions(self.protocol_versions)
        _validate_candidates(self.candidates)
        if self.relay_reservation and not _OPAQUE_RE.fullmatch(
            self.relay_reservation
        ):
            raise ValueError("invalid relay reservation")
        _validate_bytes(
            self.capability_digest, size=32, name="capability digest"
        )
        _validate_revision(self.sequence, self.previous_digest)
        _validate_window(
            self.issued_ms,
            self.expires_ms,
            now=current,
            max_ttl_ms=MAX_REACHABILITY_TTL_MS,
            object_name="reachability record",
        )
        _validate_bytes(self.signature, size=64, name="reachability signature")
        Ed25519PublicKey.from_public_bytes(descriptor.sign_public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "object_type": self.object_type,
            "node_id": self.node_id,
            "descriptor_digest": b64e(self.descriptor_digest),
            "session_id": self.session_id,
            "protocol_versions": list(self.protocol_versions),
            "candidates": list(self.candidates),
            "relay_reservation": self.relay_reservation,
            "capability_digest": b64e(self.capability_digest),
            "sequence": self.sequence,
            "previous_digest": b64e(self.previous_digest),
            "issued_ms": self.issued_ms,
            "expires_ms": self.expires_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> "ReachabilityRecord":
        fields = {
            "version",
            "object_type",
            "node_id",
            "descriptor_digest",
            "session_id",
            "protocol_versions",
            "candidates",
            "relay_reservation",
            "capability_digest",
            "sequence",
            "previous_digest",
            "issued_ms",
            "expires_ms",
            "signature",
        }
        _require_exact_fields(value, required=fields)
        protocols = value["protocol_versions"]
        candidates = value["candidates"]
        if not isinstance(protocols, list) or not isinstance(candidates, list):
            raise ValueError("reachability protocols and candidates must be lists")
        record = cls(
            version=_parse_int(value["version"], name="reachability version"),
            object_type=str(value["object_type"]),
            node_id=str(value["node_id"]),
            descriptor_digest=_decode_bytes(
                value["descriptor_digest"], size=32, name="descriptor digest"
            ),
            session_id=str(value["session_id"]),
            protocol_versions=tuple(str(item) for item in protocols),
            candidates=tuple(str(item) for item in candidates),
            relay_reservation=str(value["relay_reservation"]),
            capability_digest=_decode_bytes(
                value["capability_digest"], size=32, name="capability digest"
            ),
            sequence=_parse_int(value["sequence"], name="reachability sequence"),
            previous_digest=_decode_bytes(
                value["previous_digest"], size=32, name="previous digest"
            ),
            issued_ms=_parse_int(
                value["issued_ms"], name="reachability issued time"
            ),
            expires_ms=_parse_int(
                value["expires_ms"], name="reachability expiry time"
            ),
            signature=_decode_bytes(
                value["signature"], size=64, name="reachability signature"
            ),
        )
        record.verify(descriptor, now=now)
        return record


def issue_reachability_record(
    identity: Identity,
    descriptor: NodeDescriptor,
    *,
    protocol_versions: tuple[str, ...] | list[str],
    candidates: tuple[str, ...] | list[str] = (),
    relay_reservation: str = "",
    capability_digest: bytes,
    sequence: int = 1,
    previous_digest: bytes = GENESIS_DIGEST,
    session_id: str | None = None,
    issued_ms: int | None = None,
    ttl_ms: int = 5 * 60 * 1000,
) -> ReachabilityRecord:
    issued = _now_ms() if issued_ms is None else issued_ms
    if identity.node_id != descriptor.node_id:
        raise ValueError("identity does not own the node descriptor")
    unsigned = ReachabilityRecord(
        node_id=identity.node_id,
        descriptor_digest=descriptor.digest,
        session_id=secrets.token_hex(16) if session_id is None else session_id,
        protocol_versions=tuple(
            sorted(set(str(item) for item in protocol_versions))
        ),
        candidates=tuple(sorted(set(str(item) for item in candidates))),
        relay_reservation=str(relay_reservation),
        capability_digest=bytes(capability_digest),
        sequence=sequence,
        previous_digest=bytes(previous_digest),
        issued_ms=issued,
        expires_ms=issued + ttl_ms,
        signature=b"",
    )
    signature = identity.sign(canonical_pack(unsigned.signing_fields()))
    record = ReachabilityRecord(
        **{**unsigned.__dict__, "signature": signature}
    )
    record.verify(descriptor, now=issued)
    return record


class HumanPrincipalIdentity:
    def __init__(self, sign_private: Ed25519PrivateKey) -> None:
        self.sign_private = sign_private

    @classmethod
    def generate(cls) -> "HumanPrincipalIdentity":
        return cls(Ed25519PrivateKey.generate())

    @property
    def sign_public(self) -> bytes:
        return self.sign_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def human_id(self) -> str:
        return derive_human_id(self.sign_public)

    def sign(self, raw: bytes) -> bytes:
        return self.sign_private.sign(raw)


@dataclass(frozen=True)
class HumanDeviceGrant:
    human_id: str
    human_sign_public: bytes
    device_node_id: str
    descriptor_digest: bytes
    capabilities: tuple[str, ...]
    sequence: int
    previous_digest: bytes
    issued_ms: int
    expires_ms: int
    signature: bytes
    version: int = HUMAN_DEVICE_GRANT_VERSION
    object_type: str = HUMAN_DEVICE_GRANT_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.human_id,
            self.human_sign_public,
            self.device_node_id,
            self.descriptor_digest,
            list(self.capabilities),
            self.sequence,
            self.previous_digest,
            self.issued_ms,
            self.expires_ms,
        ]

    @property
    def digest(self) -> bytes:
        return _object_digest(self.signing_fields(), self.signature)

    def verify(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> None:
        current = _now_ms() if now is None else now
        if (
            self.version != HUMAN_DEVICE_GRANT_VERSION
            or self.object_type != HUMAN_DEVICE_GRANT_TYPE
        ):
            raise ValueError("unsupported human device grant")
        descriptor.verify(now=current)
        _validate_human_id(self.human_id)
        _validate_bytes(
            self.human_sign_public, size=32, name="human signing public key"
        )
        if self.human_id != derive_human_id(self.human_sign_public):
            raise ValueError("human id does not match its public key")
        if self.human_sign_public == descriptor.sign_public:
            raise ValueError("human and device signing identities must be separate")
        _validate_node_id(self.device_node_id)
        if self.device_node_id != descriptor.node_id:
            raise ValueError("human grant belongs to another device")
        _validate_bytes(
            self.descriptor_digest, size=32, name="descriptor digest"
        )
        if self.descriptor_digest != descriptor.digest:
            raise ValueError("human grant uses a stale device descriptor")
        _validate_capabilities(self.capabilities)
        _validate_revision(self.sequence, self.previous_digest)
        _validate_window(
            self.issued_ms,
            self.expires_ms,
            now=current,
            max_ttl_ms=MAX_HUMAN_GRANT_TTL_MS,
            object_name="human device grant",
        )
        _validate_bytes(
            self.signature, size=64, name="human device grant signature"
        )
        Ed25519PublicKey.from_public_bytes(self.human_sign_public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "object_type": self.object_type,
            "human_id": self.human_id,
            "human_sign_public": b64e(self.human_sign_public),
            "device_node_id": self.device_node_id,
            "descriptor_digest": b64e(self.descriptor_digest),
            "capabilities": list(self.capabilities),
            "sequence": self.sequence,
            "previous_digest": b64e(self.previous_digest),
            "issued_ms": self.issued_ms,
            "expires_ms": self.expires_ms,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> "HumanDeviceGrant":
        fields = {
            "version",
            "object_type",
            "human_id",
            "human_sign_public",
            "device_node_id",
            "descriptor_digest",
            "capabilities",
            "sequence",
            "previous_digest",
            "issued_ms",
            "expires_ms",
            "signature",
        }
        _require_exact_fields(value, required=fields)
        capabilities = value["capabilities"]
        if not isinstance(capabilities, list):
            raise ValueError("human device capabilities must be a list")
        grant = cls(
            version=_parse_int(value["version"], name="human device grant version"),
            object_type=str(value["object_type"]),
            human_id=str(value["human_id"]),
            human_sign_public=_decode_bytes(
                value["human_sign_public"],
                size=32,
                name="human signing public key",
            ),
            device_node_id=str(value["device_node_id"]),
            descriptor_digest=_decode_bytes(
                value["descriptor_digest"], size=32, name="descriptor digest"
            ),
            capabilities=tuple(str(item) for item in capabilities),
            sequence=_parse_int(value["sequence"], name="human device grant sequence"),
            previous_digest=_decode_bytes(
                value["previous_digest"], size=32, name="previous digest"
            ),
            issued_ms=_parse_int(
                value["issued_ms"], name="human device grant issued time"
            ),
            expires_ms=_parse_int(
                value["expires_ms"], name="human device grant expiry time"
            ),
            signature=_decode_bytes(
                value["signature"], size=64, name="human device grant signature"
            ),
        )
        grant.verify(descriptor, now=now)
        return grant


def issue_human_device_grant(
    human: HumanPrincipalIdentity,
    descriptor: NodeDescriptor,
    *,
    capabilities: tuple[str, ...] | list[str],
    sequence: int = 1,
    previous_digest: bytes = GENESIS_DIGEST,
    issued_ms: int | None = None,
    ttl_ms: int = 30 * 24 * 60 * 60 * 1000,
) -> HumanDeviceGrant:
    issued = _now_ms() if issued_ms is None else issued_ms
    unsigned = HumanDeviceGrant(
        human_id=human.human_id,
        human_sign_public=human.sign_public,
        device_node_id=descriptor.node_id,
        descriptor_digest=descriptor.digest,
        capabilities=tuple(
            sorted(set(str(item) for item in capabilities))
        ),
        sequence=sequence,
        previous_digest=bytes(previous_digest),
        issued_ms=issued,
        expires_ms=issued + ttl_ms,
        signature=b"",
    )
    signature = human.sign(canonical_pack(unsigned.signing_fields()))
    grant = HumanDeviceGrant(
        **{**unsigned.__dict__, "signature": signature}
    )
    grant.verify(descriptor, now=issued)
    return grant


@dataclass(frozen=True)
class HumanDeviceRevocation:
    human_id: str
    human_sign_public: bytes
    device_node_id: str
    descriptor_digest: bytes
    sequence: int
    previous_digest: bytes
    revoked_ms: int
    reason_code: str
    signature: bytes
    version: int = HUMAN_DEVICE_REVOCATION_VERSION
    object_type: str = HUMAN_DEVICE_REVOCATION_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.human_id,
            self.human_sign_public,
            self.device_node_id,
            self.descriptor_digest,
            self.sequence,
            self.previous_digest,
            self.revoked_ms,
            self.reason_code,
        ]

    @property
    def digest(self) -> bytes:
        return _object_digest(self.signing_fields(), self.signature)

    def verify(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> None:
        current = _now_ms() if now is None else now
        if (
            self.version != HUMAN_DEVICE_REVOCATION_VERSION
            or self.object_type != HUMAN_DEVICE_REVOCATION_TYPE
        ):
            raise ValueError("unsupported human device revocation")
        descriptor.verify(now=current)
        _validate_human_id(self.human_id)
        _validate_bytes(
            self.human_sign_public, size=32, name="human signing public key"
        )
        if self.human_id != derive_human_id(self.human_sign_public):
            raise ValueError("human id does not match its public key")
        if self.human_sign_public == descriptor.sign_public:
            raise ValueError("human and device signing identities must be separate")
        if self.device_node_id != descriptor.node_id:
            raise ValueError("human revocation belongs to another device")
        _validate_bytes(
            self.descriptor_digest, size=32, name="descriptor digest"
        )
        if self.descriptor_digest != descriptor.digest:
            raise ValueError("human revocation uses a stale device descriptor")
        _validate_revision(self.sequence, self.previous_digest)
        if (
            isinstance(self.revoked_ms, bool)
            or not isinstance(self.revoked_ms, int)
            or self.revoked_ms <= 0
            or self.revoked_ms > current + MAX_CLOCK_SKEW_MS
        ):
            raise ValueError("invalid human device revocation time")
        if not _TOKEN_RE.fullmatch(self.reason_code):
            raise ValueError("invalid human device revocation reason")
        _validate_bytes(
            self.signature, size=64, name="human device revocation signature"
        )
        Ed25519PublicKey.from_public_bytes(self.human_sign_public).verify(
            self.signature, canonical_pack(self.signing_fields())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "object_type": self.object_type,
            "human_id": self.human_id,
            "human_sign_public": b64e(self.human_sign_public),
            "device_node_id": self.device_node_id,
            "descriptor_digest": b64e(self.descriptor_digest),
            "sequence": self.sequence,
            "previous_digest": b64e(self.previous_digest),
            "revoked_ms": self.revoked_ms,
            "reason_code": self.reason_code,
            "signature": b64e(self.signature),
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> "HumanDeviceRevocation":
        fields = {
            "version",
            "object_type",
            "human_id",
            "human_sign_public",
            "device_node_id",
            "descriptor_digest",
            "sequence",
            "previous_digest",
            "revoked_ms",
            "reason_code",
            "signature",
        }
        _require_exact_fields(value, required=fields)
        revocation = cls(
            version=_parse_int(
                value["version"], name="human device revocation version"
            ),
            object_type=str(value["object_type"]),
            human_id=str(value["human_id"]),
            human_sign_public=_decode_bytes(
                value["human_sign_public"],
                size=32,
                name="human signing public key",
            ),
            device_node_id=str(value["device_node_id"]),
            descriptor_digest=_decode_bytes(
                value["descriptor_digest"], size=32, name="descriptor digest"
            ),
            sequence=_parse_int(
                value["sequence"], name="human device revocation sequence"
            ),
            previous_digest=_decode_bytes(
                value["previous_digest"], size=32, name="previous digest"
            ),
            revoked_ms=_parse_int(
                value["revoked_ms"], name="human device revocation time"
            ),
            reason_code=str(value["reason_code"]),
            signature=_decode_bytes(
                value["signature"],
                size=64,
                name="human device revocation signature",
            ),
        )
        revocation.verify(descriptor, now=now)
        return revocation


def issue_human_device_revocation(
    human: HumanPrincipalIdentity,
    descriptor: NodeDescriptor,
    *,
    sequence: int,
    previous_digest: bytes,
    reason_code: str = "device-revoked",
    revoked_ms: int | None = None,
) -> HumanDeviceRevocation:
    revoked = _now_ms() if revoked_ms is None else revoked_ms
    unsigned = HumanDeviceRevocation(
        human_id=human.human_id,
        human_sign_public=human.sign_public,
        device_node_id=descriptor.node_id,
        descriptor_digest=descriptor.digest,
        sequence=sequence,
        previous_digest=bytes(previous_digest),
        revoked_ms=revoked,
        reason_code=str(reason_code),
        signature=b"",
    )
    signature = human.sign(canonical_pack(unsigned.signing_fields()))
    event = HumanDeviceRevocation(
        **{**unsigned.__dict__, "signature": signature}
    )
    event.verify(descriptor, now=revoked)
    return event


@dataclass(frozen=True)
class _RevisionCheckpoint:
    sequence: int
    digest: bytes
    terminal: bool = False


class ControlPlaneRevisionTracker:
    """Fail-closed revision state used by tests and future durable stores."""

    def __init__(self) -> None:
        self._descriptors: dict[str, _RevisionCheckpoint] = {}
        self._reachability: dict[str, _RevisionCheckpoint] = {}
        self._human_devices: dict[
            tuple[str, str], _RevisionCheckpoint
        ] = {}

    @staticmethod
    def _advance(
        checkpoints: dict[Any, _RevisionCheckpoint],
        key: Any,
        *,
        sequence: int,
        previous_digest: bytes,
        digest: bytes,
        terminal: bool = False,
        forbid_after_terminal: bool = False,
    ) -> bool:
        current = checkpoints.get(key)
        if current is None:
            if sequence != 1 or previous_digest != GENESIS_DIGEST:
                raise ValueError("revision chain must start at genesis")
        else:
            if sequence == current.sequence:
                if digest == current.digest:
                    return False
                raise ValueError("revision fork at the current sequence")
            if sequence < current.sequence:
                raise ValueError("stale revision")
            if forbid_after_terminal and current.terminal:
                raise ValueError("revision chain is permanently revoked")
            if sequence != current.sequence + 1:
                raise ValueError("revision sequence gap")
            if previous_digest != current.digest:
                raise ValueError("revision predecessor mismatch")
        checkpoints[key] = _RevisionCheckpoint(
            sequence=sequence, digest=digest, terminal=terminal
        )
        return True

    def accept_descriptor(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> bool:
        descriptor.verify(now=now)
        return self._advance(
            self._descriptors,
            descriptor.node_id,
            sequence=descriptor.sequence,
            previous_digest=descriptor.previous_digest,
            digest=descriptor.digest,
        )

    def _require_current_descriptor(self, descriptor: NodeDescriptor) -> None:
        current = self._descriptors.get(descriptor.node_id)
        if current is None or current.digest != descriptor.digest:
            raise ValueError("object is not bound to the current node descriptor")

    def accept_reachability(
        self,
        record: ReachabilityRecord,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        record.verify(descriptor, now=now)
        self._require_current_descriptor(descriptor)
        return self._advance(
            self._reachability,
            record.node_id,
            sequence=record.sequence,
            previous_digest=record.previous_digest,
            digest=record.digest,
        )

    def accept_human_grant(
        self,
        grant: HumanDeviceGrant,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        grant.verify(descriptor, now=now)
        self._require_current_descriptor(descriptor)
        return self._advance(
            self._human_devices,
            (grant.human_id, grant.device_node_id),
            sequence=grant.sequence,
            previous_digest=grant.previous_digest,
            digest=grant.digest,
            forbid_after_terminal=True,
        )

    def accept_human_revocation(
        self,
        revocation: HumanDeviceRevocation,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        revocation.verify(descriptor, now=now)
        self._require_current_descriptor(descriptor)
        key = (revocation.human_id, revocation.device_node_id)
        if key not in self._human_devices:
            raise ValueError("cannot revoke a device without an accepted grant")
        return self._advance(
            self._human_devices,
            key,
            sequence=revocation.sequence,
            previous_digest=revocation.previous_digest,
            digest=revocation.digest,
            terminal=True,
            forbid_after_terminal=True,
        )


CONTROL_PLANE_DB_VERSION = 1


class ControlPlaneStore:
    """Durable public control-plane checkpoints with transactional fencing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
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

    def _initialize_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, CONTROL_PLANE_DB_VERSION}:
            raise ValueError(
                f"unsupported control-plane database version: {version}"
            )
        if version == CONTROL_PLANE_DB_VERSION:
            return
        self.connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE control_node_descriptors (
                node_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                digest BLOB NOT NULL CHECK(length(digest) = 32),
                object_json TEXT NOT NULL
            );
            CREATE TABLE control_reachability (
                node_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                digest BLOB NOT NULL CHECK(length(digest) = 32),
                descriptor_digest BLOB NOT NULL
                    CHECK(length(descriptor_digest) = 32),
                expires_ms INTEGER NOT NULL,
                object_json TEXT NOT NULL,
                FOREIGN KEY(node_id)
                    REFERENCES control_node_descriptors(node_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE control_human_devices (
                human_id TEXT NOT NULL,
                device_node_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                digest BLOB NOT NULL CHECK(length(digest) = 32),
                descriptor_digest BLOB NOT NULL
                    CHECK(length(descriptor_digest) = 32),
                terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                event_type TEXT NOT NULL
                    CHECK(event_type IN ('grant', 'revocation')),
                object_json TEXT NOT NULL,
                PRIMARY KEY(human_id, device_node_id),
                FOREIGN KEY(device_node_id)
                    REFERENCES control_node_descriptors(node_id)
                    ON DELETE RESTRICT
            );
            PRAGMA user_version = 1;
            COMMIT;
            """
        )

    @staticmethod
    def _json(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decoded_json(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("corrupt control-plane object JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("corrupt control-plane object JSON")
        return value

    @staticmethod
    def _check_revision(
        current: sqlite3.Row | None,
        *,
        sequence: int,
        previous_digest: bytes,
        digest: bytes,
        forbid_after_terminal: bool = False,
    ) -> bool:
        if current is None:
            if sequence != 1 or previous_digest != GENESIS_DIGEST:
                raise ValueError("revision chain must start at genesis")
            return True
        current_sequence = int(current["sequence"])
        current_digest = bytes(current["digest"])
        if sequence == current_sequence:
            if digest == current_digest:
                return False
            raise ValueError("revision fork at the current sequence")
        if sequence < current_sequence:
            raise ValueError("stale revision")
        if forbid_after_terminal and bool(current["terminal"]):
            raise ValueError("revision chain is permanently revoked")
        if sequence != current_sequence + 1:
            raise ValueError("revision sequence gap")
        if previous_digest != current_digest:
            raise ValueError("revision predecessor mismatch")
        return True

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.execute("COMMIT")
        self._restrict_permissions()

    def _rollback(self) -> None:
        self.connection.execute("ROLLBACK")

    def accept_descriptor(
        self, descriptor: NodeDescriptor, *, now: int | None = None
    ) -> bool:
        descriptor.verify(now=now)
        self._begin()
        try:
            current = self.connection.execute(
                """
                SELECT sequence, digest
                FROM control_node_descriptors
                WHERE node_id = ?
                """,
                (descriptor.node_id,),
            ).fetchone()
            changed = self._check_revision(
                current,
                sequence=descriptor.sequence,
                previous_digest=descriptor.previous_digest,
                digest=descriptor.digest,
            )
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO control_node_descriptors(
                        node_id, sequence, digest, object_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        sequence = excluded.sequence,
                        digest = excluded.digest,
                        object_json = excluded.object_json
                    """,
                    (
                        descriptor.node_id,
                        descriptor.sequence,
                        descriptor.digest,
                        self._json(descriptor.to_dict()),
                    ),
                )
            self._commit()
            return changed
        except Exception:
            self._rollback()
            raise

    def _require_current_descriptor(
        self, descriptor: NodeDescriptor
    ) -> None:
        current = self.connection.execute(
            """
            SELECT digest
            FROM control_node_descriptors
            WHERE node_id = ?
            """,
            (descriptor.node_id,),
        ).fetchone()
        if current is None or bytes(current["digest"]) != descriptor.digest:
            raise ValueError("object is not bound to the current node descriptor")

    def accept_reachability(
        self,
        record: ReachabilityRecord,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        record.verify(descriptor, now=now)
        self._begin()
        try:
            self._require_current_descriptor(descriptor)
            current = self.connection.execute(
                """
                SELECT sequence, digest
                FROM control_reachability
                WHERE node_id = ?
                """,
                (record.node_id,),
            ).fetchone()
            changed = self._check_revision(
                current,
                sequence=record.sequence,
                previous_digest=record.previous_digest,
                digest=record.digest,
            )
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO control_reachability(
                        node_id, sequence, digest, descriptor_digest,
                        expires_ms, object_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        sequence = excluded.sequence,
                        digest = excluded.digest,
                        descriptor_digest = excluded.descriptor_digest,
                        expires_ms = excluded.expires_ms,
                        object_json = excluded.object_json
                    """,
                    (
                        record.node_id,
                        record.sequence,
                        record.digest,
                        record.descriptor_digest,
                        record.expires_ms,
                        self._json(record.to_dict()),
                    ),
                )
            self._commit()
            return changed
        except Exception:
            self._rollback()
            raise

    def accept_human_grant(
        self,
        grant: HumanDeviceGrant,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        grant.verify(descriptor, now=now)
        self._begin()
        try:
            self._require_current_descriptor(descriptor)
            current = self.connection.execute(
                """
                SELECT sequence, digest, terminal
                FROM control_human_devices
                WHERE human_id = ? AND device_node_id = ?
                """,
                (grant.human_id, grant.device_node_id),
            ).fetchone()
            changed = self._check_revision(
                current,
                sequence=grant.sequence,
                previous_digest=grant.previous_digest,
                digest=grant.digest,
                forbid_after_terminal=True,
            )
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO control_human_devices(
                        human_id, device_node_id, sequence, digest,
                        descriptor_digest, terminal, event_type, object_json
                    ) VALUES (?, ?, ?, ?, ?, 0, 'grant', ?)
                    ON CONFLICT(human_id, device_node_id) DO UPDATE SET
                        sequence = excluded.sequence,
                        digest = excluded.digest,
                        descriptor_digest = excluded.descriptor_digest,
                        terminal = 0,
                        event_type = 'grant',
                        object_json = excluded.object_json
                    """,
                    (
                        grant.human_id,
                        grant.device_node_id,
                        grant.sequence,
                        grant.digest,
                        grant.descriptor_digest,
                        self._json(grant.to_dict()),
                    ),
                )
            self._commit()
            return changed
        except Exception:
            self._rollback()
            raise

    def accept_human_revocation(
        self,
        revocation: HumanDeviceRevocation,
        descriptor: NodeDescriptor,
        *,
        now: int | None = None,
    ) -> bool:
        revocation.verify(descriptor, now=now)
        self._begin()
        try:
            self._require_current_descriptor(descriptor)
            current = self.connection.execute(
                """
                SELECT sequence, digest, terminal
                FROM control_human_devices
                WHERE human_id = ? AND device_node_id = ?
                """,
                (revocation.human_id, revocation.device_node_id),
            ).fetchone()
            if current is None:
                raise ValueError(
                    "cannot revoke a device without an accepted grant"
                )
            changed = self._check_revision(
                current,
                sequence=revocation.sequence,
                previous_digest=revocation.previous_digest,
                digest=revocation.digest,
                forbid_after_terminal=True,
            )
            if changed:
                self.connection.execute(
                    """
                    UPDATE control_human_devices
                    SET sequence = ?,
                        digest = ?,
                        descriptor_digest = ?,
                        terminal = 1,
                        event_type = 'revocation',
                        object_json = ?
                    WHERE human_id = ? AND device_node_id = ?
                    """,
                    (
                        revocation.sequence,
                        revocation.digest,
                        revocation.descriptor_digest,
                        self._json(revocation.to_dict()),
                        revocation.human_id,
                        revocation.device_node_id,
                    ),
                )
            self._commit()
            return changed
        except Exception:
            self._rollback()
            raise

    def current_descriptor(
        self, node_id: str, *, now: int | None = None
    ) -> NodeDescriptor | None:
        row = self.connection.execute(
            """
            SELECT object_json
            FROM control_node_descriptors
            WHERE node_id = ?
            """,
            (_validate_node_id(node_id),),
        ).fetchone()
        if row is None:
            return None
        return NodeDescriptor.from_dict(
            self._decoded_json(str(row["object_json"])), now=now
        )

    def current_reachability(
        self, node_id: str, *, now: int | None = None
    ) -> ReachabilityRecord | None:
        current = _now_ms() if now is None else now
        descriptor = self.current_descriptor(node_id, now=now)
        if descriptor is None:
            return None
        row = self.connection.execute(
            """
            SELECT object_json, descriptor_digest, expires_ms
            FROM control_reachability
            WHERE node_id = ?
            """,
            (descriptor.node_id,),
        ).fetchone()
        if row is None:
            return None
        if int(row["expires_ms"]) <= current:
            return None
        if bytes(row["descriptor_digest"]) != descriptor.digest:
            return None
        return ReachabilityRecord.from_dict(
            self._decoded_json(str(row["object_json"])),
            descriptor,
            now=current,
        )

    def human_device_grant(
        self,
        human_id: str,
        device_node_id: str,
        *,
        now: int | None = None,
    ) -> HumanDeviceGrant | None:
        descriptor = self.current_descriptor(device_node_id, now=now)
        if descriptor is None:
            return None
        row = self.connection.execute(
            """
            SELECT event_type, object_json, descriptor_digest
            FROM control_human_devices
            WHERE human_id = ? AND device_node_id = ?
            """,
            (
                _validate_human_id(human_id),
                descriptor.node_id,
            ),
        ).fetchone()
        if (
            row is None
            or str(row["event_type"]) != "grant"
            or bytes(row["descriptor_digest"]) != descriptor.digest
        ):
            return None
        return HumanDeviceGrant.from_dict(
            self._decoded_json(str(row["object_json"])),
            descriptor,
            now=now,
        )

    def is_human_device_revoked(
        self, human_id: str, device_node_id: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT terminal
            FROM control_human_devices
            WHERE human_id = ? AND device_node_id = ?
            """,
            (
                _validate_human_id(human_id),
                _validate_node_id(device_node_id),
            ),
        ).fetchone()
        return bool(row and row["terminal"])

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ControlPlaneStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
