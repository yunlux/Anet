from __future__ import annotations

import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .encoding import MAX_WIRE_BYTES, canonical_pack, pack, unpack
from .identity import Identity, derive_node_id


PACKET_VERSION = 3
SUPPORTED_PACKET_VERSIONS = frozenset({1, 2, 3})
INNER_VERSION = 1
MAX_PLAINTEXT_BYTES = 8 * 1024 * 1024
MAX_TTL_SECONDS = 30 * 86400
MAX_CLOCK_SKEW_MS = 15 * 60 * 1000
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
QOS_CLASSES = frozenset({"control", "interactive", "normal", "bulk"})
KEY_MODES = frozenset({"static", "opk"})


class Recipient(Protocol):
    node_id: str
    box_public: bytes


@dataclass(frozen=True)
class PublicRecipient:
    node_id: str
    box_public: bytes


@dataclass(frozen=True)
class PacketInfo:
    packet_id: str
    destination_id: str
    created_ms: int
    expires_ms: int
    max_hops: int
    qos: str
    raw: bytes
    key_mode: str = "static"
    prekey_id: str = ""
    version: int = PACKET_VERSION


@dataclass(frozen=True)
class OpenedMessage:
    packet_id: str
    sender_id: str
    sender_sign_public: bytes
    sender_box_public: bytes
    kind: str
    created_ms: int
    body: Any
    causal: tuple[str, ...]
    codec: str
    reply_to: str
    qos: str
    key_mode: str = "static"
    prekey_id: str = ""

    @property
    def sender_recipient(self) -> PublicRecipient:
        return PublicRecipient(self.sender_id, self.sender_box_public)


def now_ms() -> int:
    return int(time.time() * 1000)


def _derived_prekey_id(public_key: bytes) -> str:
    return hashlib.blake2s(
        bytes(public_key), digest_size=16, person=b"anet-opk"
    ).hexdigest()


def _key(
    shared: bytes,
    *,
    version: int,
    packet_id: str,
    recipient_box: bytes,
    ephemeral_public: bytes,
    key_mode: str = "static",
    prekey_id: str = "",
) -> bytes:
    if version <= 2:
        # Preserve the deployed v1 wire domain across the Anet brand rename.
        info = b"ainet/sealed-packet/v1\x00" + recipient_box + ephemeral_public
    else:
        info = canonical_pack(
            [
                "ainet/sealed-packet/v3",
                recipient_box,
                ephemeral_public,
                key_mode,
                prekey_id,
            ]
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(packet_id),
        info=info,
    ).derive(shared)


def _inner_fields(value: dict[str, Any]) -> list[Any]:
    return [
        int(value["v"]),
        str(value["sid"]),
        bytes(value["spk"]),
        bytes(value["bpk"]),
        str(value["kind"]),
        int(value["ts"]),
        value.get("body"),
        list(value.get("causal", [])),
        str(value.get("codec", "msgpack")),
        str(value.get("reply", "")),
    ]


def _aad_fields(value: dict[str, Any]) -> list[Any]:
    fields = [
        int(value["v"]),
        str(value["id"]),
        str(value["dst"]),
        int(value["ts"]),
        int(value["exp"]),
        int(value["hops"]),
        bytes(value["epk"]),
        bytes(value["nonce"]),
    ]
    if int(value["v"]) >= 2:
        fields.append(str(value["qos"]))
    if int(value["v"]) >= 3:
        fields.extend([str(value["km"]), str(value["kid"])])
    return fields


def _validate_outer(value: dict[str, Any], raw: bytes) -> PacketInfo:
    version = int(value.get("v", 0))
    if version not in SUPPORTED_PACKET_VERSIONS:
        raise ValueError("unsupported packet version")
    packet_id = str(value.get("id", ""))
    if len(packet_id) != 32:
        raise ValueError("invalid packet id")
    try:
        bytes.fromhex(packet_id)
    except ValueError as exc:
        raise ValueError("invalid packet id") from exc
    destination_id = str(value.get("dst", ""))
    if not destination_id.startswith("an1") or len(destination_id) < 20:
        raise ValueError("invalid packet destination")
    created_ms = int(value.get("ts", 0))
    expires_ms = int(value.get("exp", 0))
    max_hops = int(value.get("hops", 0))
    if created_ms <= 0 or expires_ms <= created_ms:
        raise ValueError("invalid packet lifetime")
    current = now_ms()
    if created_ms > current + MAX_CLOCK_SKEW_MS:
        raise ValueError("packet creation time is too far in the future")
    if expires_ms > current + MAX_TTL_SECONDS * 1000 + MAX_CLOCK_SKEW_MS:
        raise ValueError("packet expiry is too far in the future")
    if expires_ms - created_ms > MAX_TTL_SECONDS * 1000:
        raise ValueError("packet lifetime exceeds protocol limit")
    if not 1 <= max_hops <= 32:
        raise ValueError("invalid hop limit")
    if len(bytes(value.get("epk", b""))) != 32:
        raise ValueError("invalid ephemeral key")
    if len(bytes(value.get("nonce", b""))) != 12:
        raise ValueError("invalid packet nonce")
    qos = "normal" if version == 1 else str(value.get("qos", ""))
    if qos not in QOS_CLASSES:
        raise ValueError("invalid packet QoS class")
    key_mode = "static" if version <= 2 else str(value.get("km", ""))
    prekey_id = "" if version <= 2 else str(value.get("kid", ""))
    if key_mode not in KEY_MODES:
        raise ValueError("invalid packet key mode")
    if key_mode == "static" and prekey_id:
        raise ValueError("static packet cannot name a prekey")
    if key_mode == "opk":
        if not re.fullmatch(r"[0-9a-f]{32}", prekey_id):
            raise ValueError("invalid packet prekey ID")
    ciphertext = bytes(value.get("ct", b""))
    if len(ciphertext) < 32 or len(ciphertext) > MAX_PLAINTEXT_BYTES + 16:
        raise ValueError("invalid ciphertext size")
    return PacketInfo(
        packet_id,
        destination_id,
        created_ms,
        expires_ms,
        max_hops,
        qos,
        raw,
        key_mode,
        prekey_id,
        version,
    )


def inspect_packet(raw: bytes, *, allow_expired: bool = False) -> PacketInfo:
    raw = bytes(raw)
    if len(raw) > MAX_WIRE_BYTES:
        raise ValueError("packet exceeds wire size limit")
    value = unpack(raw)
    if not isinstance(value, dict):
        raise ValueError("packet must be a map")
    info = _validate_outer(value, raw)
    if not allow_expired and info.expires_ms <= now_ms():
        raise ValueError("packet expired")
    return info


def seal_packet(
    sender: Identity,
    recipient: Recipient,
    *,
    kind: str,
    body: Any,
    ttl_seconds: int = 86400,
    max_hops: int = 8,
    causal: list[str] | tuple[str, ...] = (),
    codec: str = "msgpack",
    reply_to: str = "",
    padding_min: int = 512,
    qos: str = "normal",
    recipient_prekey_public: bytes | None = None,
    recipient_prekey_id: str = "",
    packet_version: int | None = None,
) -> bytes:
    kind = str(kind).strip().lower()
    if not _KIND_RE.match(kind):
        raise ValueError("invalid message kind")
    ttl_seconds = max(30, min(int(ttl_seconds), MAX_TTL_SECONDS))
    max_hops = max(1, min(int(max_hops), 32))
    qos = str(qos).strip().lower()
    if qos not in QOS_CLASSES:
        raise ValueError("invalid packet QoS class")
    packet_version = PACKET_VERSION if packet_version is None else int(packet_version)
    if packet_version not in SUPPORTED_PACKET_VERSIONS:
        raise ValueError("unsupported packet version")
    if recipient_prekey_public is None:
        if recipient_prekey_id:
            raise ValueError("prekey ID requires a prekey public key")
        key_mode = "static"
        recipient_key_public = bytes(recipient.box_public)
    else:
        if packet_version < 3:
            raise ValueError("one-time prekeys require packet version 3")
        key_mode = "opk"
        recipient_key_public = bytes(recipient_prekey_public)
        if len(recipient_key_public) != 32:
            raise ValueError("prekey public key must be 32 bytes")
        recipient_prekey_id = str(recipient_prekey_id).strip().lower()
        if recipient_prekey_id != _derived_prekey_id(recipient_key_public):
            raise ValueError("prekey ID does not match public key")
    created_ms = now_ms()
    packet_id = os.urandom(16).hex()
    inner: dict[str, Any] = {
        "v": INNER_VERSION,
        "sid": sender.node_id,
        "spk": sender.sign_public,
        "bpk": sender.box_public,
        "kind": kind,
        "ts": created_ms,
        "body": body,
        "causal": list(causal),
        "codec": str(codec or "msgpack"),
        "reply": str(reply_to or ""),
    }
    inner["sig"] = sender.sign(canonical_pack(_inner_fields(inner)))
    encoded_inner = pack(inner)
    needed = 4 + len(encoded_inner)
    bucket = max(256, int(padding_min))
    if bucket & (bucket - 1):
        bucket = 1 << (bucket - 1).bit_length()
    if needed > bucket:
        bucket = 1 << (needed - 1).bit_length()
    if bucket > MAX_PLAINTEXT_BYTES:
        raise ValueError("message is too large")
    plaintext = len(encoded_inner).to_bytes(4, "big") + encoded_inner + os.urandom(bucket - needed)

    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    recipient_public = X25519PublicKey.from_public_bytes(recipient_key_public)
    shared = ephemeral.exchange(recipient_public)
    nonce = os.urandom(12)
    outer: dict[str, Any] = {
        "v": packet_version,
        "id": packet_id,
        "dst": str(recipient.node_id),
        "ts": created_ms,
        "exp": created_ms + ttl_seconds * 1000,
        "hops": max_hops,
        "epk": ephemeral_public,
        "nonce": nonce,
        "qos": qos,
    }
    if packet_version >= 3:
        outer["km"] = key_mode
        outer["kid"] = recipient_prekey_id
    key = _key(
        shared,
        version=packet_version,
        packet_id=packet_id,
        recipient_box=recipient_key_public,
        ephemeral_public=ephemeral_public,
        key_mode=key_mode,
        prekey_id=recipient_prekey_id,
    )
    outer["ct"] = ChaCha20Poly1305(key).encrypt(nonce, plaintext, canonical_pack(_aad_fields(outer)))
    raw = pack(outer)
    inspect_packet(raw)
    return raw


def open_packet(
    identity: Identity,
    raw: bytes,
    *,
    recipient_prekey_private: bytes | None = None,
) -> OpenedMessage:
    info = inspect_packet(raw)
    if info.destination_id != identity.node_id:
        raise ValueError("packet is addressed to another node")
    outer = unpack(raw)
    ephemeral_public = bytes(outer["epk"])
    if info.key_mode == "opk":
        if recipient_prekey_private is None:
            raise ValueError("packet requires a one-time prekey")
        prekey_private_raw = bytes(recipient_prekey_private)
        if len(prekey_private_raw) != 32:
            raise ValueError("prekey private key must be 32 bytes")
        recipient_private = X25519PrivateKey.from_private_bytes(prekey_private_raw)
        recipient_public = recipient_private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if _derived_prekey_id(recipient_public) != info.prekey_id:
            raise ValueError("packet prekey does not match private key")
    else:
        if recipient_prekey_private is not None:
            raise ValueError("static packet does not use a one-time prekey")
        recipient_private = identity.box_private
        recipient_public = identity.box_public
    shared = recipient_private.exchange(
        X25519PublicKey.from_public_bytes(ephemeral_public)
    )
    key = _key(
        shared,
        version=int(outer["v"]),
        packet_id=info.packet_id,
        recipient_box=recipient_public,
        ephemeral_public=ephemeral_public,
        key_mode=info.key_mode,
        prekey_id=info.prekey_id,
    )
    plaintext = ChaCha20Poly1305(key).decrypt(
        bytes(outer["nonce"]),
        bytes(outer["ct"]),
        canonical_pack(_aad_fields(outer)),
    )
    if len(plaintext) < 4:
        raise ValueError("decrypted packet is truncated")
    inner_size = int.from_bytes(plaintext[:4], "big")
    if inner_size <= 0 or inner_size > len(plaintext) - 4:
        raise ValueError("invalid inner packet length")
    inner = unpack(plaintext[4 : 4 + inner_size])
    if not isinstance(inner, dict) or int(inner.get("v", 0)) != INNER_VERSION:
        raise ValueError("unsupported inner packet")
    sign_public = bytes(inner.get("spk", b""))
    box_public = bytes(inner.get("bpk", b""))
    sender_id = str(inner.get("sid", ""))
    if len(sign_public) != 32 or len(box_public) != 32:
        raise ValueError("invalid sender keys")
    if derive_node_id(sign_public, box_public) != sender_id:
        raise ValueError("sender id does not match sender keys")
    signature = bytes(inner.get("sig", b""))
    Ed25519PublicKey.from_public_bytes(sign_public).verify(
        signature,
        canonical_pack(_inner_fields(inner)),
    )
    kind = str(inner.get("kind", ""))
    if not _KIND_RE.match(kind):
        raise ValueError("invalid inner message kind")
    causal = tuple(str(item) for item in inner.get("causal", []))
    return OpenedMessage(
        packet_id=info.packet_id,
        sender_id=sender_id,
        sender_sign_public=sign_public,
        sender_box_public=box_public,
        kind=kind,
        created_ms=int(inner.get("ts", 0)),
        body=inner.get("body"),
        causal=causal,
        codec=str(inner.get("codec", "msgpack")),
        reply_to=str(inner.get("reply", "")),
        qos=info.qos,
        key_mode=info.key_mode,
        prekey_id=info.prekey_id,
    )
