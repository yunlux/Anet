from __future__ import annotations

import hashlib
import re

from .encoding import canonical_pack


_TYPED_ACTOR_ID_RE = re.compile(
    r"^act_[a-z][a-z0-9-]{0,31}_[0-9a-f]{32}$"
)
_ACTOR_KIND_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def normalize_actor_kind(value: str) -> str:
    kind = str(value).strip().lower()
    if not _ACTOR_KIND_RE.fullmatch(kind):
        raise ValueError("invalid Actor kind")
    return kind


def is_actor_id(value: str) -> bool:
    actor_id = str(value)
    return actor_id.startswith("an1") or bool(_TYPED_ACTOR_ID_RE.fullmatch(actor_id))


def validate_actor_id(value: str) -> str:
    actor_id = str(value).strip()
    if not is_actor_id(actor_id):
        raise ValueError("invalid Actor ID")
    return actor_id


def platform_actor_id(
    platform: str,
    *,
    namespace_actor_id: str,
    platform_actor_key: str,
) -> str:
    """Derive an opaque Actor ID inside one attesting Actor's namespace.

    ``platform_actor_key`` is expected to be an Adapter-owned pseudonym, never a
    raw platform account identifier. Including the attester namespace prevents
    unrelated bridges from accidentally claiming that their local pseudonyms
    identify the same Actor.
    """

    platform_name = str(platform).strip().lower()
    if not _PLATFORM_RE.fullmatch(platform_name):
        raise ValueError("invalid Actor platform")
    namespace = validate_actor_id(namespace_actor_id)
    local_key = str(platform_actor_key).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32,128}", local_key):
        raise ValueError("invalid platform Actor pseudonym")
    digest = hashlib.blake2s(
        canonical_pack(
            {
                "platform": platform_name,
                "namespace_actor_id": namespace,
                "platform_actor_key": local_key,
            }
        ),
        digest_size=16,
        person=b"anet-act",
    ).hexdigest()
    return f"act_{platform_name}_{digest}"
