from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path


def identity_key_path(home: Path) -> Path:
    return Path(home) / "amesh-identity.key"


class LocalIdentity:
    """Private installation identity used for stable local pseudonyms.

    This is deliberately an Amesh primitive. It is not a platform account,
    Discord token, agent bearer token, or identity from another project.
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.path = identity_key_path(self.home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create()
        self.identity_id = "id_" + hashlib.sha256(self._key).hexdigest()[:32]

    def _load_or_create(self) -> bytes:
        if self.path.exists():
            value = self.path.read_bytes()
            if len(value) != 32:
                raise ValueError("Amesh identity key is invalid")
            return value
        value = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return value

    def pseudonym(self, namespace: str, value: str) -> str:
        namespace = str(namespace).strip().lower()
        value = str(value)
        if not namespace or len(namespace) > 64 or len(value) > 4096:
            raise ValueError("Amesh pseudonym input is outside limits")
        return hmac.new(
            self._key,
            f"{namespace}\0{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def platform_actor_id(
    platform: str,
    *,
    namespace_actor_id: str,
    platform_actor_key: str,
) -> str:
    seed = (
        f"amesh.actor.v1\0{str(platform).strip().lower()}\0"
        f"{namespace_actor_id}\0{platform_actor_key}"
    ).encode("utf-8")
    return "actor_" + hashlib.blake2s(seed, digest_size=20).hexdigest()
