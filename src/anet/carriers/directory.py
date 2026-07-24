from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..encoding import atomic_write, b64e, canonical_pack, pack, unpack
from ..identity import Identity, PeerCard
from ..packet import MAX_CLOCK_SKEW_MS, inspect_packet
from .base import CarrierFrame, CarrierScan

if TYPE_CHECKING:
    from ..node import AnetNode


LEGACY_DIRECTORY_CARRIER_VERSION = 1
DIRECTORY_CARRIER_VERSION = 2
CHANNEL_EPOCH_SECONDS = 7 * 24 * 60 * 60
CHANNEL_EPOCH_MS = CHANNEL_EPOCH_SECONDS * 1000
# Current plus five previous weekly epochs covers the full 31-day frame
# acceptance window even immediately after an epoch boundary.
CHANNEL_HISTORY_EPOCHS = 5
MAX_FRAME_AGE_MS = 31 * 86400 * 1000
MAX_DROP_BYTES = 12 * 1024 * 1024
CARRIER_FRAME_MIN_BYTES = 4096
CARRIER_WIRE_RESERVE_BYTES = 256


@dataclass(frozen=True)
class _Channel:
    version: int
    epoch: int
    token: str
    encryption_key: bytes
    naming_key: bytes

    @property
    def locator(self) -> str:
        # v1 used an identifying static prefix. v2 is an opaque, rotating
        # collection name. This does not hide access timing from the store.
        if self.version == LEGACY_DIRECTORY_CARRIER_VERSION:
            return f"ch-{self.token}"
        return self.token


def _packet_id(value: str) -> str:
    value = str(value)
    if len(value) != 32:
        raise ValueError("invalid carrier packet id")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("invalid carrier packet id") from exc
    return value


def _pad_frame_plaintext(raw: bytes) -> bytes:
    raw = bytes(raw)
    required = 4 + len(raw)
    target = max(CARRIER_FRAME_MIN_BYTES, 1 << (required - 1).bit_length())
    if target > MAX_DROP_BYTES - CARRIER_WIRE_RESERVE_BYTES:
        target = required
    if target > MAX_DROP_BYTES - CARRIER_WIRE_RESERVE_BYTES:
        raise ValueError("directory carrier plaintext is too large")
    return len(raw).to_bytes(4, "big") + raw + os.urandom(target - required)


def _unpad_frame_plaintext(raw: bytes) -> bytes:
    raw = bytes(raw)
    if len(raw) < 4:
        raise ValueError("invalid padded carrier plaintext")
    size = int.from_bytes(raw[:4], "big")
    if size < 1 or size > len(raw) - 4:
        raise ValueError("invalid padded carrier plaintext length")
    return raw[4 : 4 + size]


class DirectoryCarrier:
    """Encrypted, signed store-and-forward frames in an untrusted directory.

    The directory can be replicated by any external mechanism.  Anet does
    not trust that mechanism and does not expose a listening socket here.
    """

    name = "directory-v2"

    def __init__(self, root: Path, identity: Identity) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = identity

    @staticmethod
    def _epoch_at(created_ms: int) -> int:
        created_ms = int(created_ms)
        if created_ms < 0:
            raise ValueError("carrier timestamp cannot be negative")
        return created_ms // CHANNEL_EPOCH_MS

    def _channel(
        self,
        peer: PeerCard,
        *,
        sender_id: str,
        recipient_id: str,
        version: int = DIRECTORY_CARRIER_VERSION,
        epoch: int | None = None,
    ) -> _Channel:
        if {sender_id, recipient_id} != {self.identity.node_id, peer.node_id}:
            raise ValueError("carrier channel participants do not match keys")
        version = int(version)
        if version not in {LEGACY_DIRECTORY_CARRIER_VERSION, DIRECTORY_CARRIER_VERSION}:
            raise ValueError("unsupported carrier channel version")
        if version == LEGACY_DIRECTORY_CARRIER_VERSION:
            epoch = 0
        elif epoch is None:
            epoch = self._epoch_at(int(time.time() * 1000))
        epoch = int(epoch)
        if epoch < 0:
            raise ValueError("invalid carrier channel epoch")
        shared = self.identity.box_private.exchange(
            X25519PublicKey.from_public_bytes(peer.box_public)
        )
        participants = sorted((self.identity.node_id, peer.node_id))
        # Keep the deployed v1 derivation domain so existing peer channels and
        # queued carrier frames remain readable after the brand rename.
        context: list[Any] = ["ainet/directory-carrier", version, participants]
        direction: list[Any] = ["direction", sender_id, recipient_id]
        if version >= DIRECTORY_CARRIER_VERSION:
            context.append(epoch)
            direction.append(epoch)
        salt = hashlib.sha256(canonical_pack(context)).digest()
        material = HKDF(
            algorithm=hashes.SHA256(),
            length=84,
            salt=salt,
            info=canonical_pack(direction),
        ).derive(shared)
        return _Channel(
            version=version,
            epoch=epoch,
            token=b64e(material[:20]),
            encryption_key=material[20:52],
            naming_key=material[52:84],
        )

    def _channel_path(self, channel: _Channel, *, create: bool = True) -> Path:
        path = self.root / channel.locator
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def outbound_channel(
        self,
        peer: PeerCard,
        *,
        epoch: int | None = None,
        version: int = DIRECTORY_CARRIER_VERSION,
    ) -> _Channel:
        return self._channel(
            peer,
            sender_id=self.identity.node_id,
            recipient_id=peer.node_id,
            version=version,
            epoch=epoch,
        )

    def inbound_channel(
        self,
        peer: PeerCard,
        *,
        epoch: int | None = None,
        version: int = DIRECTORY_CARRIER_VERSION,
    ) -> _Channel:
        return self._channel(
            peer,
            sender_id=peer.node_id,
            recipient_id=self.identity.node_id,
            version=version,
            epoch=epoch,
        )

    def inbound_channels(
        self,
        peer: PeerCard,
        *,
        now_ms: int | None = None,
        include_legacy: bool = True,
    ) -> tuple[_Channel, ...]:
        current = self._epoch_at(int(time.time() * 1000) if now_ms is None else now_ms)
        epochs = [current - offset for offset in range(CHANNEL_HISTORY_EPOCHS + 1)]
        # A sender a few minutes ahead can enter the next epoch first.
        epochs.append(current + 1)
        channels = [
            self.inbound_channel(peer, epoch=epoch, version=DIRECTORY_CARRIER_VERSION)
            for epoch in epochs
            if epoch >= 0
        ]
        if include_legacy:
            channels.append(
                self.inbound_channel(peer, version=LEGACY_DIRECTORY_CARRIER_VERSION)
            )
        return tuple(channels)

    @staticmethod
    def _fields(value: dict[str, Any]) -> list[Any]:
        version = int(value["v"])
        fields: list[Any] = [
            version,
            str(value["t"]),
            str(value["sid"]),
            str(value["rid"]),
        ]
        if version >= DIRECTORY_CARRIER_VERSION:
            fields.append(int(value["e"]))
        fields.extend(
            [
                int(value["ts"]),
                bytes(value["salt"]),
                str(value["pid"]),
                int(value.get("depth", 0)),
                bytes(value.get("raw", b"")),
            ]
        )
        return fields

    @staticmethod
    def _filename(channel: _Channel, kind: str, packet_id: str) -> str:
        digest = hmac.new(
            channel.naming_key,
            canonical_pack([str(kind), str(packet_id)]),
            hashlib.sha256,
        ).hexdigest()
        if channel.version == LEGACY_DIRECTORY_CARRIER_VERSION:
            return f"{digest[:40]}.drop"
        return digest[:48]

    @staticmethod
    def _aad(channel: _Channel) -> bytes:
        fields: list[Any] = [
            "ainet/directory-carrier",
            channel.version,
            channel.token,
        ]
        if channel.version >= DIRECTORY_CARRIER_VERSION:
            fields.append(channel.epoch)
        return canonical_pack(fields)

    def _publish(
        self,
        peer: PeerCard,
        *,
        kind: str,
        packet_id: str,
        raw: bytes = b"",
        depth: int = 0,
    ) -> bool:
        channel, filename, wire = self.encode_frame(
            peer,
            kind=kind,
            packet_id=packet_id,
            raw=raw,
            depth=depth,
        )
        path = self._channel_path(channel) / filename
        if path.exists():
            return False
        atomic_write(path, wire, private=True)
        return True

    def encode_frame(
        self,
        peer: PeerCard,
        *,
        kind: str,
        packet_id: str,
        raw: bytes = b"",
        depth: int = 0,
        created_ms: int | None = None,
        version: int | None = None,
    ) -> tuple[_Channel, str, bytes]:
        packet_id = _packet_id(packet_id)
        if kind not in {"packet", "ack"}:
            raise ValueError("invalid carrier frame kind")
        created_ms = int(time.time() * 1000) if created_ms is None else int(created_ms)
        if version is None:
            version = (
                DIRECTORY_CARRIER_VERSION
                if "directory-carrier-v2" in peer.capabilities
                else LEGACY_DIRECTORY_CARRIER_VERSION
            )
        version = int(version)
        epoch = (
            self._epoch_at(created_ms) if version >= DIRECTORY_CARRIER_VERSION else 0
        )
        channel = self.outbound_channel(peer, epoch=epoch, version=version)
        body: dict[str, Any] = {
            "v": version,
            "t": kind,
            "sid": self.identity.node_id,
            "rid": peer.node_id,
            "ts": created_ms,
            "salt": os.urandom(16),
            "pid": packet_id,
            "depth": int(depth),
            "raw": bytes(raw),
        }
        if version >= DIRECTORY_CARRIER_VERSION:
            body["e"] = epoch
        body["sig"] = self.identity.sign(canonical_pack(self._fields(body)))
        nonce = os.urandom(12)
        plaintext = pack(body)
        if version >= DIRECTORY_CARRIER_VERSION:
            plaintext = _pad_frame_plaintext(plaintext)
        ciphertext = ChaCha20Poly1305(channel.encryption_key).encrypt(
            nonce,
            plaintext,
            self._aad(channel),
        )
        wire = pack({"v": version, "n": nonce, "ct": ciphertext})
        if len(wire) > MAX_DROP_BYTES:
            raise ValueError("directory carrier frame is too large")
        return channel, self._filename(channel, kind, packet_id), wire

    def publish_packet(self, peer: PeerCard, raw: bytes, *, depth: int) -> bool:
        info = inspect_packet(raw)
        depth = int(depth)
        if depth < 1 or depth > info.max_hops:
            raise ValueError("invalid directory carrier relay depth")
        return self._publish(
            peer,
            kind="packet",
            packet_id=info.packet_id,
            raw=raw,
            depth=depth,
        )

    def publish_ack(self, peer: PeerCard, packet_id: str) -> bool:
        return self._publish(peer, kind="ack", packet_id=packet_id)

    def _decode(self, path: Path, peer: PeerCard, channel: _Channel) -> CarrierFrame:
        raw_wire = path.read_bytes()
        return self.decode_frame(path, raw_wire, peer, channel=channel)

    def decode_frame(
        self,
        locator: Path,
        raw_wire: bytes,
        peer: PeerCard,
        *,
        channel: _Channel | None = None,
    ) -> CarrierFrame:
        locator = Path(locator)
        channel = channel or self.inbound_channel(peer)
        if len(raw_wire) > MAX_DROP_BYTES:
            raise ValueError("directory carrier frame is too large")
        wire = unpack(raw_wire)
        if not isinstance(wire, dict):
            raise ValueError("unsupported directory carrier frame")
        wire_version = int(wire.get("v", 0))
        if wire_version not in {
            LEGACY_DIRECTORY_CARRIER_VERSION,
            DIRECTORY_CARRIER_VERSION,
        }:
            raise ValueError("unsupported directory carrier frame")
        if wire_version != channel.version:
            raise ValueError("directory carrier channel version mismatch")
        nonce = bytes(wire.get("n", b""))
        if len(nonce) != 12:
            raise ValueError("invalid directory carrier nonce")
        plaintext = ChaCha20Poly1305(channel.encryption_key).decrypt(
            nonce,
            bytes(wire.get("ct", b"")),
            self._aad(channel),
        )
        if wire_version >= DIRECTORY_CARRIER_VERSION:
            plaintext = _unpad_frame_plaintext(plaintext)
        body = unpack(plaintext)
        if not isinstance(body, dict) or int(body.get("v", 0)) != wire_version:
            raise ValueError("invalid directory carrier body")
        if (
            wire_version >= DIRECTORY_CARRIER_VERSION
            and int(body.get("e", -1)) != channel.epoch
        ):
            raise ValueError("directory carrier epoch mismatch")
        if body.get("sid") != peer.node_id or body.get("rid") != self.identity.node_id:
            raise ValueError("directory carrier frame has wrong participants")
        created_ms = int(body.get("ts", 0))
        current_ms = int(time.time() * 1000)
        if (
            created_ms > current_ms + MAX_CLOCK_SKEW_MS
            or created_ms < current_ms - MAX_FRAME_AGE_MS
        ):
            raise ValueError(
                "directory carrier frame timestamp is outside the acceptance window"
            )
        if (
            wire_version >= DIRECTORY_CARRIER_VERSION
            and self._epoch_at(created_ms) != channel.epoch
        ):
            raise ValueError("directory carrier timestamp does not match its epoch")
        if len(bytes(body.get("salt", b""))) != 16:
            raise ValueError("invalid directory carrier frame salt")
        Ed25519PublicKey.from_public_bytes(peer.sign_public).verify(
            bytes(body.get("sig", b"")),
            canonical_pack(self._fields(body)),
        )
        kind = str(body.get("t", ""))
        if kind not in {"packet", "ack"}:
            raise ValueError("invalid directory carrier frame kind")
        packet_id = _packet_id(str(body.get("pid", "")))
        expected_name = self._filename(channel, kind, packet_id)
        if locator.name != expected_name:
            raise ValueError(
                "directory carrier filename does not match its authenticated body"
            )
        if kind == "ack":
            if bytes(body.get("raw", b"")) or int(body.get("depth", 0)) != 0:
                raise ValueError("invalid directory carrier acknowledgement")
            return CarrierFrame("ack", peer.node_id, packet_id, locator)
        packet_raw = bytes(body.get("raw", b""))
        info = inspect_packet(packet_raw)
        depth = int(body.get("depth", 0))
        if info.packet_id != packet_id or depth < 1 or depth > info.max_hops:
            raise ValueError("invalid carried packet metadata")
        return CarrierFrame(
            "packet", peer.node_id, packet_id, locator, packet_raw, depth
        )

    def scan(self, peer: PeerCard, *, limit: int = 128) -> CarrierScan:
        limit = max(1, min(int(limit), 1024))
        frames: list[CarrierFrame] = []
        rejected: list[Path] = []
        for channel in self.inbound_channels(peer):
            remaining = limit - len(frames) - len(rejected)
            if remaining <= 0:
                break
            directory = self._channel_path(channel, create=False)
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir())[:remaining]:
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(
                            "directory carrier entries must be regular files"
                        )
                    frames.append(self._decode(path, peer, channel))
                except Exception:
                    rejected.append(path)
        return CarrierScan(tuple(frames), tuple(rejected))

    def _require_local_path(self, path: Path) -> Path:
        path = Path(path)
        resolved_parent = path.parent.resolve()
        if resolved_parent != self.root and self.root not in resolved_parent.parents:
            raise ValueError("carrier path is outside the configured root")
        return path

    def consume(self, path: Path) -> None:
        path = self._require_local_path(path)
        path.unlink(missing_ok=True)

    def quarantine(self, path: Path) -> Path:
        path = self._require_local_path(path)
        quarantine = self.root / ".quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        # Do not retain the rotating mailbox token or deterministic frame
        # name in the shared quarantine namespace.
        destination = quarantine / f"{os.urandom(24).hex()}.bad"
        os.replace(path, destination)
        return destination


def sync_directory_once(
    node: AnetNode,
    root: Path,
    *,
    peer_ids: tuple[str, ...] | list[str] = (),
    push_peer_ids: tuple[str, ...] | list[str] | None = None,
    qos_allow: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    limit: int = 128,
    retry_after_ms: int = 300_000,
    path_id: str = "directory",
) -> dict[str, Any]:
    """Pull then push one pass through an encrypted directory carrier."""
    selected = set(str(item) for item in peer_ids if str(item))
    cards = [
        card for card in node.peers.all() if not selected or card.node_id in selected
    ]
    if selected - {card.node_id for card in cards}:
        missing = sorted(selected - {card.node_id for card in cards})
        raise KeyError(f"unknown carrier peer(s): {', '.join(missing)}")
    push_selected = (
        {card.node_id for card in cards}
        if push_peer_ids is None
        else {str(item) for item in push_peer_ids if str(item)}
    )
    unknown_push = push_selected - {card.node_id for card in cards}
    if unknown_push:
        raise KeyError(f"unknown push peer(s): {', '.join(sorted(unknown_push))}")
    carrier = DirectoryCarrier(root, node.identity)
    stats: dict[str, Any] = {
        "carrier": carrier.name,
        "mailbox_epoch": carrier._epoch_at(int(time.time() * 1000)),
        "mailbox_epoch_seconds": CHANNEL_EPOCH_SECONDS,
        "legacy_receive": True,
        "root": str(carrier.root),
        "peers": len(cards),
        "pulled_packets": 0,
        "pulled_acks": 0,
        "pushed_packets": 0,
        "existing_packets": 0,
        "rejected": 0,
    }

    for card in cards:
        scan = carrier.scan(card, limit=limit)
        for path in scan.rejected:
            carrier.quarantine(path)
            stats["rejected"] += 1
        for frame in scan.frames:
            try:
                if frame.kind == "ack":
                    if node.store.has_packet(frame.packet_id):
                        node.store.mark_acked(
                            [frame.packet_id],
                            card.node_id,
                            path_id=path_id,
                        )
                    stats["pulled_acks"] += 1
                else:
                    accepted = node.accept_carrier_packet(
                        frame.raw,
                        depth=frame.depth,
                        peer_id=card.node_id,
                    )
                    carrier.publish_ack(card, accepted)
                    stats["pulled_packets"] += 1
                carrier.consume(frame.path)
            except Exception:
                carrier.quarantine(frame.path)
                stats["rejected"] += 1

    for card in cards:
        if card.node_id not in push_selected:
            continue
        pending = node.store.pending_for_peer(
            card.node_id,
            limit=limit,
            retry_after_ms=max(0, int(retry_after_ms)),
            qos_allow=qos_allow,
        )
        for item in pending:
            written = carrier.publish_packet(
                card,
                item["raw"],
                depth=int(item["depth"]) + 1,
            )
            if written:
                node.store.mark_attempt(
                    [item["packet_id"]],
                    card.node_id,
                    path_id=path_id,
                )
                stats["pushed_packets"] += 1
            else:
                stats["existing_packets"] += 1

    stats["store"] = node.store.status()
    return stats
