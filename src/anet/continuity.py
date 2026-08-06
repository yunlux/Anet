"""Two-phase continuity evidence for supervised Anet deployments."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import NodeConfig
from .encoding import b64d, b64e, canonical_pack
from .identity import Identity, derive_node_id
from .supervisor_health import inspect_supervisor_health


CONTINUITY_CHALLENGE_KIND = "anet.continuity.challenge"
CONTINUITY_PREPARED_KIND = "anet.continuity.prepared"
CONTINUITY_RECEIPT_KIND = "anet.continuity.receipt"
CONTINUITY_SCHEMA_VERSION = 1
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_CHALLENGE_BYTES = 1024 * 1024
_FUTURE_SKEW_MS = 5 * 60 * 1000
_PROTECTED_FILES = ("identity.json", "tls-key.pem", "tls-cert.pem")


class ContinuityError(RuntimeError):
    """Raised when a continuity challenge or receipt cannot be trusted."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _home_fingerprint(home: Path) -> str:
    normalized = os.path.normcase(str(Path(home).expanduser().resolve()))
    return _digest_bytes(normalized.encode("utf-8"))


def _protected_hashes(home: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in _PROTECTED_FILES:
        path = home / name
        if path.is_symlink() or not path.is_file():
            raise ContinuityError(f"protected node file is missing or unsafe: {name}")
        try:
            result[name] = _digest_bytes(path.read_bytes())
        except OSError as exc:
            raise ContinuityError(f"cannot read protected node file: {name}") from exc
    return result


def _load_challenge(path: Path) -> dict[str, Any]:
    challenge_path = Path(path).expanduser().resolve()
    try:
        raw = challenge_path.read_bytes()
    except OSError as exc:
        raise ContinuityError(f"cannot read continuity challenge: {challenge_path}") from exc
    if len(raw) > _MAX_CHALLENGE_BYTES:
        raise ContinuityError("continuity challenge exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContinuityError("continuity challenge is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ContinuityError("continuity challenge must be a JSON object")
    return value


def _validate_challenge(value: dict[str, Any], *, now_ms: int) -> None:
    if value.get("kind") != CONTINUITY_CHALLENGE_KIND:
        raise ContinuityError("continuity challenge kind is invalid")
    if value.get("schema_version") != CONTINUITY_SCHEMA_VERSION:
        raise ContinuityError("continuity challenge schema is unsupported")
    try:
        challenge_id = str(value["challenge_id"])
        if uuid.UUID(hex=challenge_id).hex != challenge_id:
            raise ValueError("challenge ID is not canonical")
        created_ms = int(value["created_at_ms"])
        expires_ms = int(value["expires_at_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuityError("continuity challenge fields are invalid") from exc
    try:
        sign_public = b64d(str(value["identity_sign_public"]))
        box_public = b64d(str(value["identity_box_public"]))
        signature = b64d(str(value["signature"]))
        node_id = derive_node_id(sign_public, box_public)
        if node_id != value.get("node_id"):
            raise ContinuityError("continuity challenge Node ID is invalid")
        signed = {key: item for key, item in value.items() if key != "signature"}
        Ed25519PublicKey.from_public_bytes(sign_public).verify(
            signature, canonical_pack(signed)
        )
    except (KeyError, TypeError, ValueError, binascii.Error, InvalidSignature) as exc:
        raise ContinuityError("continuity challenge signature is invalid") from exc
    if created_ms < 0 or created_ms > now_ms + _FUTURE_SKEW_MS:
        raise ContinuityError("continuity challenge was created in the future")
    if (
        expires_ms <= created_ms
        or expires_ms <= now_ms
        or expires_ms - created_ms > _MAX_TTL_SECONDS * 1000
    ):
        raise ContinuityError("continuity challenge has expired")
    if not str(value.get("home_fingerprint", "")):
        raise ContinuityError("continuity challenge home fingerprint is missing")
    instance_id = str(value.get("supervisor_instance_id", ""))
    try:
        if uuid.UUID(hex=instance_id).hex != instance_id:
            raise ValueError("supervisor instance is not canonical")
    except ValueError as exc:
        raise ContinuityError(
            "continuity challenge supervisor instance is invalid"
        ) from exc
    if not str(value.get("boot_session_id", "")):
        raise ContinuityError("continuity challenge boot session is missing")
    hashes = value.get("protected_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(_PROTECTED_FILES):
        raise ContinuityError("continuity challenge protected hashes are incomplete")
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in hashes.values()
    ):
        raise ContinuityError("continuity challenge protected hash is invalid")


def _exclusive_private_json(
    path: Path, value: dict[str, Any], *, exists_message: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ContinuityError(exists_message) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _load_node_identity(home: Path, *, phase: str) -> Identity:
    try:
        config = NodeConfig.load(home)
        identity = Identity.load(config.identity_path)
    except Exception as exc:
        raise ContinuityError(
            f"node identity cannot be validated {phase} restart"
        ) from exc
    return identity


def _validate_tls_material(home: Path, identity: Identity, *, phase: str) -> None:
    try:
        identity.ensure_tls_material(home)
    except Exception as exc:
        raise ContinuityError(f"TLS identity is invalid {phase} restart") from exc


def prepare_continuity(
    home: Path,
    *,
    output: Path | None = None,
    ttl_seconds: int = 24 * 60 * 60,
) -> dict[str, Any]:
    """Create one private challenge before a service or device restart."""

    home = Path(home).expanduser().resolve()
    identity = _load_node_identity(home, phase="before")
    health = inspect_supervisor_health(home)
    if health.get("ok") is not True:
        raise ContinuityError(
            f"supervisor is not healthy before continuity prepare: "
            f"{health.get('reason', health.get('state', 'unknown'))}"
        )
    ttl_seconds = int(ttl_seconds)
    if not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
        raise ContinuityError(
            f"continuity TTL must be between {_MIN_TTL_SECONDS} and "
            f"{_MAX_TTL_SECONDS} seconds"
        )
    now_ms = _now_ms()
    challenge_id = uuid.uuid4().hex
    challenge_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else home / "continuity" / "challenges" / f"{challenge_id}.json"
    )
    protected_hashes = _protected_hashes(home)
    _validate_tls_material(home, identity, phase="before")
    challenge_payload = {
        "kind": CONTINUITY_CHALLENGE_KIND,
        "schema_version": CONTINUITY_SCHEMA_VERSION,
        "challenge_id": challenge_id,
        "created_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_seconds * 1000,
        "home_fingerprint": _home_fingerprint(home),
        "node_id": identity.node_id,
        "identity_sign_public": b64e(identity.sign_public),
        "identity_box_public": b64e(identity.box_public),
        "protected_hashes": protected_hashes,
        "supervisor_instance_id": str(health["instance_id"]),
        "boot_session_id": str(health["boot_session_id"]),
    }
    challenge = {
        **challenge_payload,
        "signature": b64e(identity.sign(canonical_pack(challenge_payload))),
    }
    _exclusive_private_json(
        challenge_path,
        challenge,
        exists_message=f"continuity challenge already exists: {challenge_path}",
    )
    return {
        "kind": CONTINUITY_PREPARED_KIND,
        "schema_version": CONTINUITY_SCHEMA_VERSION,
        "ok": True,
        "challenge_id": challenge_id,
        "challenge_path": str(challenge_path),
        "created_at_ms": now_ms,
        "expires_at_ms": challenge["expires_at_ms"],
        "node_id": identity.node_id,
        "supervisor_instance_id": health["instance_id"],
        "boot_session_id": health["boot_session_id"],
    }


def verify_continuity(
    home: Path,
    challenge_path: Path,
    *,
    require_boot_change: bool = False,
) -> dict[str, Any]:
    """Consume one challenge after restart and return a private receipt."""

    home = Path(home).expanduser().resolve()
    now_ms = _now_ms()
    challenge = _load_challenge(challenge_path)
    _validate_challenge(challenge, now_ms=now_ms)
    challenge_id = str(challenge["challenge_id"])
    receipt_path = home / "continuity" / "receipts" / f"{challenge_id}.json"
    if receipt_path.exists():
        raise ContinuityError("continuity challenge has already been consumed")
    if challenge["home_fingerprint"] != _home_fingerprint(home):
        raise ContinuityError("continuity challenge belongs to another node home")

    identity = _load_node_identity(home, phase="after")
    if challenge["node_id"] != identity.node_id:
        raise ContinuityError("node identity changed after restart")
    protected_hashes = _protected_hashes(home)
    if challenge["protected_hashes"] != protected_hashes:
        raise ContinuityError("protected node identity material changed after restart")
    _validate_tls_material(home, identity, phase="after")

    health = inspect_supervisor_health(home)
    if health.get("ok") is not True:
        raise ContinuityError(
            f"supervisor is not healthy after restart: "
            f"{health.get('reason', health.get('state', 'unknown'))}"
        )
    previous_instance = str(challenge["supervisor_instance_id"])
    current_instance = str(health["instance_id"])
    if current_instance == previous_instance:
        raise ContinuityError("supervisor instance did not change after prepare")
    if int(health["last_sync_at_ms"]) < int(challenge["created_at_ms"]):
        raise ContinuityError("supervisor has not synchronized after prepare")

    previous_boot = str(challenge["boot_session_id"])
    current_boot = str(health["boot_session_id"])
    boot_changed = (
        previous_boot != "unknown"
        and current_boot != "unknown"
        and previous_boot != current_boot
    )
    if require_boot_change and not boot_changed:
        raise ContinuityError("operating-system boot session did not change")

    receipt: dict[str, Any] = {
        "kind": CONTINUITY_RECEIPT_KIND,
        "schema_version": CONTINUITY_SCHEMA_VERSION,
        "ok": True,
        "mode": "device-restart" if boot_changed else "supervisor-restart",
        "challenge_id": challenge_id,
        "prepared_at_ms": int(challenge["created_at_ms"]),
        "verified_at_ms": now_ms,
        "node_id": identity.node_id,
        "identity_preserved": True,
        "protected_files": list(_PROTECTED_FILES),
        "supervisor_restarted": True,
        "previous_supervisor_instance_id": previous_instance,
        "current_supervisor_instance_id": current_instance,
        "previous_boot_session_id": previous_boot,
        "current_boot_session_id": current_boot,
        "boot_session_changed": boot_changed,
        "supervisor_health": health,
        "receipt_path": str(receipt_path),
    }
    _exclusive_private_json(
        receipt_path,
        receipt,
        exists_message="continuity challenge has already been consumed",
    )
    return receipt
