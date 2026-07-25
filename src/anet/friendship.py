from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .encoding import atomic_write, b64d, b64e, canonical_pack
from .identity import Identity, PeerCard
from .pairing import PairOffer, PairResponse


FRIENDSHIP_VERSION = 1
FRIEND_INVITE_TYPE = "anet.friend.invite.v1"
FRIEND_ACCEPTANCE_TYPE = "anet.friend.acceptance.v1"
FRIEND_RELATIONSHIP = "friend"
FRIEND_QR_SCHEME = "anet"
FRIEND_QR_HOST = "friend"
MAX_QR_TEXT_BYTES = 4096
MAX_QR_OBJECT_BYTES = 32 * 1024


@dataclass(frozen=True)
class FriendInvite:
    offer: PairOffer
    signature: bytes
    relationship: str = FRIEND_RELATIONSHIP
    version: int = FRIENDSHIP_VERSION
    object_type: str = FRIEND_INVITE_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.relationship,
            self.offer.to_dict(),
        ]

    def verify(self, *, now: int | None = None) -> None:
        if (
            self.version != FRIENDSHIP_VERSION
            or self.object_type != FRIEND_INVITE_TYPE
            or self.relationship != FRIEND_RELATIONSHIP
        ):
            raise ValueError("unsupported friend invite")
        self.offer.verify(now=now)
        Ed25519PublicKey.from_public_bytes(self.offer.card.sign_public).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(
            canonical_pack([self.signing_fields(), self.signature])
        ).digest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "relationship": self.relationship,
            "offer": self.offer.to_dict(),
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        identity: Identity,
        card: PeerCard,
        *,
        ttl_seconds: int = 600,
        now: int | None = None,
    ) -> "FriendInvite":
        offer = PairOffer.create(
            identity,
            card,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        unsigned = cls(offer=offer, signature=b"")
        return cls(
            offer=offer,
            signature=identity.sign(canonical_pack(unsigned.signing_fields())),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FriendInvite":
        if set(value) != {
            "version",
            "type",
            "relationship",
            "offer",
            "signature",
        }:
            raise ValueError("friend invite has unexpected fields")
        return cls(
            version=int(value.get("version", 0)),
            object_type=str(value.get("type", "")),
            relationship=str(value.get("relationship", "")),
            offer=PairOffer.from_dict(dict(value["offer"])),
            signature=b64d(str(value.get("signature", ""))),
        )


@dataclass(frozen=True)
class FriendAcceptance:
    invite: FriendInvite
    response: PairResponse
    invite_digest: bytes
    signature: bytes
    relationship: str = FRIEND_RELATIONSHIP
    version: int = FRIENDSHIP_VERSION
    object_type: str = FRIEND_ACCEPTANCE_TYPE

    def signing_fields(self) -> list[Any]:
        return [
            self.version,
            self.object_type,
            self.relationship,
            self.invite_digest,
            self.response.to_dict(),
        ]

    def verify(self, *, now: int | None = None) -> None:
        if (
            self.version != FRIENDSHIP_VERSION
            or self.object_type != FRIEND_ACCEPTANCE_TYPE
            or self.relationship != FRIEND_RELATIONSHIP
        ):
            raise ValueError("unsupported friend acceptance")
        self.invite.verify(now=now)
        if self.invite_digest != self.invite.digest:
            raise ValueError("friend acceptance is not bound to this invite")
        self.response.verify(self.invite.offer, now=now)
        Ed25519PublicKey.from_public_bytes(
            self.response.card.sign_public
        ).verify(
            self.signature,
            canonical_pack(self.signing_fields()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.object_type,
            "relationship": self.relationship,
            "invite": self.invite.to_dict(),
            "invite_digest": b64e(self.invite_digest),
            "response": self.response.to_dict(),
            "signature": b64e(self.signature),
        }

    @classmethod
    def create(
        cls,
        invite: FriendInvite,
        identity: Identity,
        card: PeerCard,
        *,
        now: int | None = None,
    ) -> "FriendAcceptance":
        invite.verify(now=now)
        response = PairResponse.create(
            invite.offer,
            identity,
            card,
            now=now,
        )
        unsigned = cls(
            invite=invite,
            response=response,
            invite_digest=invite.digest,
            signature=b"",
        )
        return cls(
            invite=invite,
            response=response,
            invite_digest=invite.digest,
            signature=identity.sign(canonical_pack(unsigned.signing_fields())),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FriendAcceptance":
        if set(value) != {
            "version",
            "type",
            "relationship",
            "invite",
            "invite_digest",
            "response",
            "signature",
        }:
            raise ValueError("friend acceptance has unexpected fields")
        return cls(
            version=int(value.get("version", 0)),
            object_type=str(value.get("type", "")),
            relationship=str(value.get("relationship", "")),
            invite=FriendInvite.from_dict(dict(value["invite"])),
            invite_digest=b64d(str(value.get("invite_digest", ""))),
            response=PairResponse.from_dict(dict(value["response"])),
            signature=b64d(str(value.get("signature", ""))),
        )


FriendCode = FriendInvite | FriendAcceptance


def encode_friend_code(value: FriendCode) -> str:
    kind = "invite" if isinstance(value, FriendInvite) else "acceptance"
    packed = json.dumps(
        value.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    compressed = zlib.compress(packed, level=9)
    payload = b64e(compressed)
    text = f"{FRIEND_QR_SCHEME}://{FRIEND_QR_HOST}/v1/{kind}/{payload}"
    if len(text.encode("utf-8")) > MAX_QR_TEXT_BYTES:
        raise ValueError("friend code exceeds the QR payload limit")
    return text


def decode_friend_code(text: str, *, now: int | None = None) -> FriendCode:
    raw_text = str(text).strip()
    if len(raw_text.encode("utf-8")) > MAX_QR_TEXT_BYTES:
        raise ValueError("friend code exceeds the QR payload limit")
    parsed = urlsplit(raw_text)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != FRIEND_QR_SCHEME
        or parsed.netloc != FRIEND_QR_HOST
        or len(parts) != 3
        or parts[0] != "v1"
        or parts[1] not in {"invite", "acceptance"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Anet friend code")
    try:
        compressed = b64d(parts[2])
        decompressor = zlib.decompressobj()
        unpacked = decompressor.decompress(
            compressed,
            MAX_QR_OBJECT_BYTES + 1,
        )
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("invalid compressed friend code stream")
    except Exception as exc:
        raise ValueError("invalid compressed friend code") from exc
    if len(unpacked) > MAX_QR_OBJECT_BYTES:
        raise ValueError("friend code object exceeds the decoded limit")
    try:
        value = json.loads(unpacked.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid friend code object") from exc
    if not isinstance(value, dict):
        raise ValueError("friend code object must be a JSON object")
    if parts[1] == "invite":
        result: FriendCode = FriendInvite.from_dict(value)
    else:
        result = FriendAcceptance.from_dict(value)
    result.verify(now=now)
    return result


def write_friend_code(text: str, path: Path) -> None:
    target = Path(path)
    if target.suffix.lower() in {".txt", ".anetqr"}:
        atomic_write(target, (text + "\n").encode("utf-8"), private=True)
        return
    if target.suffix.lower() != ".png":
        raise ValueError("friend code output must use .png, .anetqr, or .txt")
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError(
            "QR image support is not installed; install anet-fabric[qr]"
        ) from exc
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    qr.make_image(fill_color="black", back_color="white").save(target)


def read_friend_code(source: str | Path) -> str:
    candidate = Path(source)
    try:
        exists = candidate.exists()
    except OSError:
        exists = False
    if exists:
        if candidate.suffix.lower() in {".txt", ".anetqr"}:
            return candidate.read_text(encoding="utf-8").strip()
        try:
            import zxingcpp
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "QR image scanning is not installed; install anet-fabric[qr]"
            ) from exc
        with Image.open(candidate) as image:
            barcode = zxingcpp.read_barcode(
                image,
                formats=zxingcpp.BarcodeFormat.QRCode,
            )
        if barcode is None:
            raise ValueError("no QR code found in the image")
        return str(barcode.text).strip()
    return str(source).strip()
