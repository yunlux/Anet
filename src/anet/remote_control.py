"""Prototype remote control plane for a self-starting Anet Windows node.

This module deliberately starts with a plain JSON control page so the runtime
and installer workflow can be exercised before the signed publication protocol
is introduced.  The page is a data source for configuration, peer cards and
optional package updates; it is never executed as a script.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pathlib import Path
from typing import Any

from .config import NodeConfig
from .encoding import atomic_json, atomic_write, b64d, b64e, canonical_pack
from .identity import Identity, PeerCard
from .locator import parse_locator, validate_locator_context
from .peers import PeerBook


LOGGER = logging.getLogger("anet.remote_control")
CONTROL_SETTINGS_NAME = "remote-control.json"
CONTROL_STATE_NAME = "remote-control-state.json"
CONTROL_VERSION = 1
CONTROL_SIGNATURE_VERSION = 1
CONTROL_SIGNATURE_FIELD = "_anet_control"
CONTROL_CLOCK_SKEW_MS = 5 * 60 * 1000
CONTROL_DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000
CONTROL_MAX_TTL_MS = 30 * 24 * 60 * 60 * 1000
MAX_CONTROL_TRUSTED_KEYS = 16
DEFAULT_POLL_SECONDS = 300.0
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_COUNT = 64
MAX_PAGE_DEPTH = 4
_NETWORK_CONFIG_KEYS = frozenset(
    {
        "listen_host",
        "listen_port",
        "listen_enabled",
        "advertise",
        "locator_contexts",
        "capabilities",
    }
)
_REPOSITORY_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONTROL_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RemoteControlError(RuntimeError):
    """Raised when a remote control page cannot be read or applied."""


class SupervisorLock:
    """Hold one OS-level lock for the supervisor owning a node home."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "supervisor.lock"
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if not self.home.is_dir():
            raise RemoteControlError(f"node home does not exist: {self.home}")
        handle = self.path.open("a+b")
        try:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RemoteControlError(
                f"another Anet supervisor already owns node home: {self.home}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            LOGGER.debug("failed to release supervisor lock", exc_info=True)
        finally:
            handle.close()

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _control_signing_fields(
    payload: dict[str, Any],
    *,
    key_id: str,
    issued_ms: int,
    expires_ms: int,
) -> list[Any]:
    return [
        CONTROL_SIGNATURE_VERSION,
        key_id,
        issued_ms,
        expires_ms,
        payload,
    ]


def _validate_control_window(
    issued_ms: Any,
    expires_ms: Any,
    *,
    now_ms: int | None = None,
) -> tuple[int, int]:
    if (
        isinstance(issued_ms, bool)
        or isinstance(expires_ms, bool)
        or not isinstance(issued_ms, int)
        or not isinstance(expires_ms, int)
    ):
        raise RemoteControlError("control signature timestamps must be integers")
    current = _now_ms() if now_ms is None else int(now_ms)
    if issued_ms > current + CONTROL_CLOCK_SKEW_MS:
        raise RemoteControlError("control signature is issued too far in the future")
    if expires_ms <= issued_ms:
        raise RemoteControlError("control signature expiry must follow issuance")
    if expires_ms <= current - CONTROL_CLOCK_SKEW_MS:
        raise RemoteControlError("control signature has expired")
    if expires_ms - issued_ms > CONTROL_MAX_TTL_MS:
        raise RemoteControlError("control signature lifetime is too long")
    return issued_ms, expires_ms


def _normalise_trusted_keys(value: Any) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise RemoteControlError("control trusted_keys must be an object")
    if len(value) > MAX_CONTROL_TRUSTED_KEYS:
        raise RemoteControlError("control trusted key set is too large")
    result: dict[str, str] = {}
    for raw_key_id, raw_public_key in value.items():
        key_id = str(raw_key_id).strip()
        if not _CONTROL_KEY_ID_PATTERN.fullmatch(key_id):
            raise RemoteControlError("control trusted key id is invalid")
        encoded = str(raw_public_key).strip()
        try:
            public_key = b64d(encoded)
        except (TypeError, ValueError) as exc:
            raise RemoteControlError(
                f"control trusted key {key_id} is not valid base64"
            ) from exc
        if len(public_key) != 32:
            raise RemoteControlError(
                f"control trusted key {key_id} must contain 32 public-key bytes"
            )
        result[key_id] = b64e(public_key)
    return result


def sign_control_page(
    payload: dict[str, Any],
    identity: Identity,
    *,
    key_id: str,
    issued_ms: int | None = None,
    expires_ms: int | None = None,
) -> dict[str, Any]:
    """Return a control page signed by an offline Ed25519 publisher identity."""

    if not isinstance(payload, dict):
        raise RemoteControlError("signed control page payload must be an object")
    clean_key_id = str(key_id).strip()
    if not _CONTROL_KEY_ID_PATTERN.fullmatch(clean_key_id):
        raise RemoteControlError("control signing key id is invalid")
    issued = _now_ms() if issued_ms is None else int(issued_ms)
    expires = (
        issued + CONTROL_DEFAULT_TTL_MS if expires_ms is None else int(expires_ms)
    )
    issued, expires = _validate_control_window(issued, expires)
    document = dict(payload)
    document.pop(CONTROL_SIGNATURE_FIELD, None)
    signature = identity.sign(
        canonical_pack(
            _control_signing_fields(
                document,
                key_id=clean_key_id,
                issued_ms=issued,
                expires_ms=expires,
            )
        )
    )
    document[CONTROL_SIGNATURE_FIELD] = {
        "version": CONTROL_SIGNATURE_VERSION,
        "algorithm": "ed25519",
        "key_id": clean_key_id,
        "issued_ms": issued,
        "expires_ms": expires,
        "signature": b64e(signature),
    }
    return document


def _verify_control_page(
    value: dict[str, Any] | list[Any],
    *,
    trusted_keys: dict[str, str],
    source_url: str,
    now_ms: int | None = None,
) -> tuple[dict[str, Any] | list[Any], dict[str, Any]]:
    """Verify and strip the optional local-policy control-page signature."""

    if not isinstance(value, dict):
        if trusted_keys:
            raise RemoteControlError(
                f"signed control page required for source: {source_url}"
            )
        return value, {
            "signed": False,
            "key_id": "",
            "issued_ms": 0,
            "expires_ms": 0,
        }
    marker = value.get(CONTROL_SIGNATURE_FIELD)
    if marker is None:
        if trusted_keys:
            raise RemoteControlError(
                f"signed control page required for source: {source_url}"
            )
        return value, {
            "signed": False,
            "key_id": "",
            "issued_ms": 0,
            "expires_ms": 0,
        }
    if not isinstance(marker, dict):
        raise RemoteControlError("control signature metadata must be an object")
    if not trusted_keys:
        raise RemoteControlError(
            "control page is signed but no local trusted key is configured"
        )
    if marker.get("version") != CONTROL_SIGNATURE_VERSION:
        raise RemoteControlError("unsupported control signature version")
    if str(marker.get("algorithm", "")).casefold() != "ed25519":
        raise RemoteControlError("unsupported control signature algorithm")
    key_id = str(marker.get("key_id", "")).strip()
    if key_id not in trusted_keys:
        raise RemoteControlError(f"control signing key is not locally trusted: {key_id}")
    issued_ms, expires_ms = _validate_control_window(
        marker.get("issued_ms"),
        marker.get("expires_ms"),
        now_ms=now_ms,
    )
    try:
        public_key = b64d(trusted_keys[key_id])
        signature = b64d(str(marker.get("signature", "")))
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            canonical_pack(
                _control_signing_fields(
                    {key: item for key, item in value.items() if key != CONTROL_SIGNATURE_FIELD},
                    key_id=key_id,
                    issued_ms=issued_ms,
                    expires_ms=expires_ms,
                )
            ),
        )
    except (TypeError, ValueError, InvalidSignature) as exc:
        raise RemoteControlError(
            f"control page signature verification failed: {source_url}"
        ) from exc
    payload = {
        key: item for key, item in value.items() if key != CONTROL_SIGNATURE_FIELD
    }
    return payload, {
        "signed": True,
        "key_id": key_id,
        "issued_ms": issued_ms,
        "expires_ms": expires_ms,
    }


def _normalize_repository_ref(value: Any) -> str:
    reference = str(value or "").strip()
    if not reference:
        return ""
    if (
        not _REPOSITORY_REF_PATTERN.fullmatch(reference)
        or ".." in reference
        or "//" in reference
        or "@{" in reference
        or reference.endswith((".", "/"))
    ):
        raise RemoteControlError("software repo_ref contains an invalid Git reference")
    return reference


def _git_source(source: str, reference: str) -> str:
    package = source if source.startswith("git+") else f"git+{source}"
    if not reference:
        return package
    base, separator, fragment = package.partition("#")
    package = f"{base}@{reference}"
    if separator:
        package += f"#{fragment}"
    return package


def _is_windows_path(value: str) -> bool:
    return len(value) >= 2 and value[1] == ":"


def _local_path_from_url(value: str) -> Path:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        path = urllib.request.url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path = f"\\\\{parsed.netloc}{path}"
        return Path(path)
    return Path(value)


def _resolve_reference(base_url: str, reference: str) -> str:
    """Resolve a page-owned URL or path for nested control data."""

    target = str(reference).strip()
    if not target:
        return target
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme and not _is_windows_path(target):
        return target
    if Path(target).is_absolute() or _is_windows_path(target):
        return str(Path(target).expanduser().resolve())

    base = urllib.parse.urlparse(base_url)
    if base.scheme in {"http", "https"}:
        return urllib.parse.urljoin(base_url, target)
    base_path = _local_path_from_url(base_url).expanduser()
    return str((base_path.parent / target).resolve())


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_loopback_host(value: str) -> bool:
    host = str(value).strip().strip("[]").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard_host(value: str) -> bool:
    return str(value).strip().strip("[]") in {"0.0.0.0", "::"}


def _network_values(config: dict[str, Any]) -> tuple[str, int, bool, list[str], list[str]]:
    """Validate and return the network fields used by a persistent node."""

    host = str(config.get("listen_host", "127.0.0.1")).strip()
    if not host:
        raise RemoteControlError("node listen_host must not be empty")
    raw_port = config.get("listen_port", 4242)
    if isinstance(raw_port, bool):
        raise RemoteControlError("node listen_port must be an integer")
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise RemoteControlError("node listen_port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise RemoteControlError("node listen_port must be between 0 and 65535")

    raw_contexts = config.get("locator_contexts", [])
    if not isinstance(raw_contexts, list):
        raise RemoteControlError("node locator_contexts must be a list")
    contexts: list[str] = []
    try:
        contexts = [validate_locator_context(str(item)) for item in raw_contexts]
    except ValueError as exc:
        raise RemoteControlError(str(exc)) from exc

    raw_advertise = config.get("advertise", [])
    if not isinstance(raw_advertise, list):
        raise RemoteControlError("node advertise must be a list")
    advertise: list[str] = []
    try:
        advertise = [parse_locator(str(item)).raw for item in raw_advertise]
    except ValueError as exc:
        raise RemoteControlError(str(exc)) from exc
    return host, port, bool(config.get("listen_enabled", True)), contexts, advertise


def _validate_network_config(
    config: dict[str, Any], *, cross_platform_windows_wsl: bool
) -> None:
    host, _, listen_enabled, contexts, advertise = _network_values(config)
    if not cross_platform_windows_wsl or not listen_enabled:
        return
    host_context = any(item.startswith("host:") for item in contexts)
    host_locators = []
    for item in advertise:
        locator = parse_locator(item)
        if locator.scope == "host":
            host_locators.append(locator)
    if not host_context and not host_locators:
        return
    if _is_loopback_host(host):
        raise RemoteControlError(
            "Windows/WSL host-scoped deployment must not listen on loopback"
        )
    if any(_is_loopback_host(locator.host) for locator in host_locators):
        raise RemoteControlError(
            "Windows/WSL host-scoped locators must not advertise loopback"
        )
    if _is_wildcard_host(host) and not host_locators:
        raise RemoteControlError(
            "a wildcard Windows/WSL listener needs an explicit host-scoped advertise"
        )


def _effective_platform_config(
    document: dict[str, Any], platform_name: str
) -> dict[str, Any] | None:
    platforms = document.get("platforms")
    if not isinstance(platforms, dict):
        return None
    overlay = platforms.get(platform_name)
    if not isinstance(overlay, dict):
        return None
    base = document.get("config", {})
    if not isinstance(base, dict):
        base = {}
    patch = overlay.get("config", {})
    if not isinstance(patch, dict):
        raise RemoteControlError(
            f"control page platforms.{platform_name}.config must be an object"
        )
    return _deep_merge(base, patch)


def _has_host_scope(config: dict[str, Any]) -> bool:
    _, _, listen_enabled, contexts, advertise = _network_values(config)
    if not listen_enabled:
        return False
    return any(item.startswith("host:") for item in contexts) or any(
        parse_locator(item).scope == "host" for item in advertise
    )


def _validate_cross_platform_ports(document: dict[str, Any]) -> None:
    """Require distinct listener ports for host-scoped Windows and WSL cards."""

    windows = _effective_platform_config(document, "windows")
    wsl = _effective_platform_config(document, "wsl")
    if windows is None or wsl is None:
        return
    windows_values = _network_values(windows)
    wsl_values = _network_values(wsl)
    if not windows_values[2] or not wsl_values[2]:
        return
    windows_host_scope = _has_host_scope(windows)
    wsl_host_scope = _has_host_scope(wsl)
    if windows_host_scope != wsl_host_scope:
        raise RemoteControlError(
            "Windows and WSL host scope must be declared on both enabled overlays"
        )
    if not windows_host_scope:
        return
    windows_port = windows_values[1]
    wsl_port = wsl_values[1]
    if not 1 <= windows_port <= 65535 or not 1 <= wsl_port <= 65535:
        raise RemoteControlError(
            "Windows/WSL host-scoped deployments require explicit listener ports"
        )
    if windows_port == wsl_port:
        raise RemoteControlError(
            "Windows and WSL host-scoped deployments must use distinct listener ports"
        )


def _read_json_url(url: str, *, timeout: float) -> dict[str, Any] | list[Any]:
    target = str(url).strip()
    if not target:
        raise RemoteControlError("control page URL is empty")
    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme or _is_windows_path(target):
        path = Path(target).expanduser().resolve()
        raw = path.read_bytes()
    elif parsed.scheme == "file":
        path = _local_path_from_url(target).resolve()
        raw = path.read_bytes()
    elif parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            target,
            headers={
                "Accept": "application/json",
                "User-Agent": "Anet-Control/0.12.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
        except Exception as exc:
            raise RemoteControlError(f"failed to fetch control page: {target}") from exc
    else:
        raise RemoteControlError(f"unsupported control page URL scheme: {parsed.scheme}")
    if len(raw) > MAX_PAGE_BYTES:
        raise RemoteControlError("control page exceeds the 8 MiB prototype limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteControlError(f"control page is not valid UTF-8 JSON: {target}") from exc
    if not isinstance(value, (dict, list)):
        raise RemoteControlError("control page must contain a JSON object or array")
    return value


def _bounded_interval(value: Any, *, label: str = "control interval") -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise RemoteControlError(f"{label} is invalid") from exc
    return max(5.0, min(interval, 86400.0))


def _empty_document(
    *, poll_seconds: float = DEFAULT_POLL_SECONDS
) -> dict[str, Any]:
    return {
        "sequence": 0,
        "network": "",
        "repo_url": "",
        "repo_ref": "",
        "poll_seconds": poll_seconds,
        "config": {},
        "software": {},
        "nodes": [],
        "sources": [],
        "cross_platform_windows_wsl": False,
        "control_signed": False,
        "control_key_id": "",
        "control_issued_ms": 0,
        "control_expires_ms": 0,
    }


def runtime_platform() -> str:
    """Return the stable control-page selector for this runtime."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "linux":
        prefix = os.environ.get("PREFIX", "")
        if os.environ.get("TERMUX_VERSION") or prefix.startswith(
            "/data/data/com.termux/"
        ):
            return "termux"
        if os.environ.get("WSL_INTEROP"):
            return "wsl"
        try:
            release = Path("/proc/sys/kernel/osrelease").read_text(
                encoding="utf-8"
            )
        except OSError:
            release = ""
        if "microsoft" in release.casefold():
            return "wsl"
        return "linux"
    return sys.platform


def _normalise_document(
    value: dict[str, Any] | list[Any],
    *,
    source_url: str,
    visited: set[str],
    depth: int,
    sources: list[str],
    trusted_keys: dict[str, str] | None = None,
    now_ms: int | None = None,
    default_poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    if depth > MAX_PAGE_DEPTH:
        raise RemoteControlError("control page nesting exceeds the prototype limit")
    if source_url in visited:
        return _empty_document(poll_seconds=default_poll_seconds)
    if len(sources) >= MAX_PAGE_COUNT:
        raise RemoteControlError("control page fan-out exceeds the prototype limit")
    value, signature = _verify_control_page(
        value,
        trusted_keys={} if trusted_keys is None else trusted_keys,
        source_url=source_url,
        now_ms=now_ms,
    )
    visited.add(source_url)
    sources.append(source_url)

    documents: list[dict[str, Any]]
    if isinstance(value, list):
        documents = [item for item in value if isinstance(item, dict)]
    else:
        documents = [value]
    if not documents:
        raise RemoteControlError("control page contains no JSON objects")

    platform_name = runtime_platform()
    expanded_documents: list[dict[str, Any]] = []
    for document in documents:
        _validate_cross_platform_ports(document)
        expanded_documents.append(document)
        platforms = document.get("platforms")
        if platforms is None:
            continue
        if not isinstance(platforms, dict):
            raise RemoteControlError("control page platforms must be an object")
        overlay = platforms.get(platform_name)
        if overlay is None:
            continue
        if not isinstance(overlay, dict):
            raise RemoteControlError(
                f"control page platforms.{platform_name} must be an object"
            )
        expanded_documents.append(overlay)
    documents = expanded_documents

    result = _empty_document(poll_seconds=default_poll_seconds)
    result.update(
        {
            "control_signed": bool(signature["signed"]),
            "control_key_id": str(signature["key_id"]),
            "control_issued_ms": int(signature["issued_ms"]),
            "control_expires_ms": int(signature["expires_ms"]),
        }
    )
    result["cross_platform_windows_wsl"] = any(
        isinstance(document.get("platforms"), dict)
        and isinstance(document["platforms"].get("windows"), dict)
        and isinstance(document["platforms"].get("wsl"), dict)
        for document in documents
    )

    def normalise_node(item: Any) -> Any:
        if isinstance(item, str):
            target = _resolve_reference(source_url, item)
            return _card_value(
                _read_json_url(target, timeout=20.0),
                base_url=target,
            )
        if not isinstance(item, dict):
            return item
        if item.get("card_url"):
            target = _resolve_reference(source_url, str(item["card_url"]))
            card = _card_value(
                _read_json_url(target, timeout=20.0),
                base_url=target,
            )
            normalised_item = dict(item)
            normalised_item["card_url"] = target
            normalised_item["card"] = card
            return normalised_item
        if isinstance(item.get("card"), str):
            target = _resolve_reference(source_url, str(item["card"]))
            normalised_item = dict(item)
            normalised_item["card"] = _card_value(
                _read_json_url(target, timeout=20.0),
                base_url=target,
            )
            return normalised_item
        return item

    for document in documents:
        sequence = document.get("sequence")
        if isinstance(sequence, int):
            result["sequence"] = max(int(result["sequence"]), sequence)
        if document.get("network"):
            result["network"] = str(document["network"])
        if document.get("repo_url"):
            result["repo_url"] = _resolve_reference(
                source_url, str(document["repo_url"])
            )
        if document.get("anet_repo"):
            result["repo_url"] = _resolve_reference(
                source_url, str(document["anet_repo"])
            )
        if document.get("repo_ref"):
            result["repo_ref"] = str(document["repo_ref"]).strip()
        if document.get("anet_repo_ref"):
            result["repo_ref"] = str(document["anet_repo_ref"]).strip()
        if "poll_seconds" in document:
            result["poll_seconds"] = _bounded_interval(
                document.get("poll_seconds"),
                label="control page poll_seconds",
            )
        config = document.get("config", document.get("default_config", {}))
        if isinstance(config, dict):
            result["config"] = _deep_merge(result["config"], config)
        software = document.get("software", {})
        if isinstance(software, dict):
            software = dict(software)
            for key in ("wheel_url", "repo_url"):
                if software.get(key):
                    software[key] = _resolve_reference(
                        source_url, str(software[key])
                    )
            result["software"] = _deep_merge(result["software"], software)
        if result["repo_url"] and "repo_url" not in result["software"]:
            result["software"]["repo_url"] = result["repo_url"]
        if result["repo_ref"] and "repo_ref" not in result["software"]:
            result["software"]["repo_ref"] = result["repo_ref"]
        for key in ("nodes", "peers"):
            values = document.get(key, [])
            if isinstance(values, list):
                for item in values:
                    result["nodes"].append(normalise_node(item))

        for page_key in ("pages", "kv"):
            raw_pages = document.get(page_key, [])
            if not isinstance(raw_pages, list):
                raise RemoteControlError(f"control page {page_key} must be a list")
            for page in raw_pages:
                if isinstance(page, str):
                    page_url = _resolve_reference(source_url, page)
                elif isinstance(page, dict) and page.get("url"):
                    page_url = _resolve_reference(source_url, str(page["url"]))
                else:
                    raise RemoteControlError("control page entries must be URLs")
                child_value = _read_json_url(page_url, timeout=20.0)
                child = _normalise_document(
                    child_value,
                    source_url=page_url,
                    visited=visited,
                    depth=depth + 1,
                    sources=sources,
                    trusted_keys=trusted_keys,
                    now_ms=now_ms,
                    default_poll_seconds=default_poll_seconds,
                )
                result["sequence"] = max(result["sequence"], child["sequence"])
                result["config"] = _deep_merge(result["config"], child["config"])
                result["software"] = _deep_merge(
                    result["software"], child["software"]
                )
                result["nodes"].extend(child["nodes"])
                if child["network"]:
                    result["network"] = child["network"]
                if child["repo_url"]:
                    result["repo_url"] = child["repo_url"]
                if child["repo_ref"]:
                    result["repo_ref"] = child["repo_ref"]
                result["poll_seconds"] = min(
                    result["poll_seconds"], child["poll_seconds"]
                )
                result["cross_platform_windows_wsl"] = bool(
                    result["cross_platform_windows_wsl"]
                    or child.get("cross_platform_windows_wsl", False)
                )
    result["sources"] = list(sources)
    return result


def _load_control_settings(home: Path, url: str | None) -> dict[str, Any]:
    path = Path(home) / CONTROL_SETTINGS_NAME
    local_value: dict[str, Any] = {}
    if path.exists():
        try:
            raw_local = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RemoteControlError(f"invalid control settings: {path}") from exc
        if not isinstance(raw_local, dict):
            raise RemoteControlError(f"control settings must be an object: {path}")
        local_value = dict(raw_local)
    trusted_keys = _normalise_trusted_keys(local_value.get("trusted_keys", {}))
    if url:
        return {
            "version": CONTROL_VERSION,
            "url": str(url),
            "interval": DEFAULT_POLL_SECONDS,
            "trusted_keys": trusted_keys,
        }
    configured = os.environ.get("ANET_CONTROL_URL", "").strip()
    if configured:
        return {
            "version": CONTROL_VERSION,
            "url": configured,
            "interval": DEFAULT_POLL_SECONDS,
            "trusted_keys": trusted_keys,
        }
    if not local_value:
        raise RemoteControlError(
            f"no control page configured; create {path} or set ANET_CONTROL_URL"
        )
    if not str(local_value.get("url", "")).strip():
        raise RemoteControlError(f"control settings require a url: {path}")
    value = local_value
    value["trusted_keys"] = trusted_keys
    value["interval"] = _bounded_interval(
        value.get("interval", DEFAULT_POLL_SECONDS),
        label=f"control settings interval: {path}",
    )
    return value


def _load_state(home: Path) -> dict[str, Any]:
    path = Path(home) / CONTROL_STATE_NAME
    if not path.exists():
        return {"version": CONTROL_VERSION, "sequence": -1, "digest": ""}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteControlError(f"invalid control state: {path}") from exc
    return value if isinstance(value, dict) else {}


def _snapshot_control_files(home: Path) -> dict[Path, bytes | None]:
    """Capture the node files changed by one remote-control sync."""

    paths = (
        Path(home) / "config.json",
        Path(home) / "card.json",
        Path(home) / "peers.json",
    )
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            snapshot[path] = path.read_bytes() if path.exists() else None
        except OSError as exc:
            raise RemoteControlError(
                f"cannot snapshot node control file: {path}"
            ) from exc
    return snapshot


def _restore_control_files(snapshot: dict[Path, bytes | None]) -> None:
    """Restore a failed sync without exposing an intermediate JSON file."""

    try:
        for path, data in snapshot.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data, private=path.name != "card.json")
    except OSError as exc:
        raise RemoteControlError("failed to roll back remote control files") from exc


def _apply_config(
    home: Path,
    patch: dict[str, Any],
    *,
    cross_platform_windows_wsl: bool = False,
) -> bool:
    if not patch:
        return False
    config_path = Path(home) / "config.json"
    try:
        original = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteControlError(f"cannot read node config: {config_path}") from exc
    if not isinstance(original, dict):
        raise RemoteControlError("node config must be a JSON object")
    updated = _deep_merge(original, patch)
    if updated == original:
        return False
    _validate_network_config(
        updated,
        cross_platform_windows_wsl=cross_platform_windows_wsl,
    )
    card_path = Path(home) / "card.json"
    old_card = card_path.read_bytes() if card_path.exists() else None
    signed_card_fields_changed = any(
        updated.get(key) != original.get(key)
        for key in _NETWORK_CONFIG_KEYS
    )
    try:
        atomic_json(config_path, updated)
        validated = NodeConfig.load(Path(home))
        # Canonicalize through NodeConfig instead of retaining arbitrary page
        # keys, and regenerate the local public Card when network or
        # capability fields change. This mirrors locator-config's signed-card
        # boundary for the remote-control path.
        validated.save()
        if signed_card_fields_changed:
            identity = Identity.load(validated.identity_path)
            identity.card(
                addresses=validated.effective_addresses(),
                capabilities=validated.capabilities,
            ).save(card_path)
    except Exception:
        atomic_json(config_path, original)
        if old_card is None:
            card_path.unlink(missing_ok=True)
        else:
            atomic_write(card_path, old_card)
        raise
    return True


def _card_value(item: Any, *, base_url: str) -> dict[str, Any]:
    if isinstance(item, str):
        target = urllib.parse.urljoin(base_url, item)
        value = _read_json_url(target, timeout=20.0)
    elif isinstance(item, dict) and isinstance(item.get("card"), dict):
        value = item["card"]
    elif isinstance(item, dict) and item.get("card_url"):
        target = urllib.parse.urljoin(base_url, str(item["card_url"]))
        value = _read_json_url(target, timeout=20.0)
    else:
        value = item
    if isinstance(value, dict) and isinstance(value.get("card"), dict):
        value = value["card"]
    if not isinstance(value, dict):
        raise RemoteControlError("remote node entry must contain a Peer Card object")
    return value


def _apply_nodes(home: Path, nodes: list[Any]) -> tuple[int, int]:
    if not nodes:
        return 0, 0
    config = NodeConfig.load(Path(home))
    identity = Identity.load(config.identity_path)
    peers = PeerBook(config.peers_path, own_node_id=identity.node_id)
    try:
        cards = [PeerCard.from_dict(_card_value(item, base_url="")) for item in nodes]
        return peers.add_many(cards)
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteControlError("remote control page contains an invalid Peer Card") from exc


def _current_version() -> str:
    try:
        return importlib.metadata.version("anet-fabric")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _installed_package_paths() -> tuple[Path, ...]:
    """Return package files that belong to the active runtime, if discoverable."""

    try:
        spec = importlib.util.find_spec("anet")
        locations = list(spec.submodule_search_locations or ()) if spec else []
        distribution = importlib.metadata.distribution("anet-fabric")
        metadata_path = Path(str(getattr(distribution, "_path", ""))).resolve()
    except (ImportError, OSError, ValueError):
        return ()
    prefix = Path(sys.prefix).resolve()
    candidates: list[Path] = []
    if locations:
        candidates.append(Path(locations[0]).resolve())
    if str(metadata_path) and metadata_path.exists():
        candidates.append(metadata_path)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.exists() or not _is_within(candidate, prefix):
            continue
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _snapshot_installed_package(home: Path) -> Path | None:
    paths = _installed_package_paths()
    if not paths:
        return None
    rollback_root = Path(home) / "control-cache" / "rollback"
    rollback_root.mkdir(parents=True, exist_ok=True)
    snapshot = rollback_root / f"snapshot-{_now_ms()}-{os.getpid()}"
    snapshot.mkdir()
    entries: list[dict[str, str]] = []
    try:
        for index, target in enumerate(paths):
            backup = snapshot / f"entry-{index}-{target.name}"
            if target.is_dir():
                shutil.copytree(target, backup, symlinks=True)
            else:
                shutil.copy2(target, backup)
            entries.append({"target": str(target), "backup": backup.name})
        atomic_json(
            snapshot / "manifest.json",
            {"version": 1, "entries": entries},
            private=True,
        )
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def _restore_package_snapshot(snapshot: Path) -> None:
    manifest_path = Path(snapshot) / "manifest.json"
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise RemoteControlError("rollback snapshot manifest is invalid")
    targets: list[tuple[Path, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RemoteControlError("rollback snapshot entry is invalid")
        raw_target = str(entry.get("target", "")).strip()
        raw_backup = str(entry.get("backup", "")).strip()
        if not raw_target or not raw_backup:
            raise RemoteControlError("rollback snapshot entry is incomplete")
        target = Path(raw_target).resolve()
        backup = (Path(snapshot) / raw_backup).resolve()
        if (
            not _is_within(target, Path(sys.prefix))
            or not _is_within(backup, Path(snapshot))
            or not backup.exists()
        ):
            raise RemoteControlError("rollback snapshot entry is outside its boundary")
        targets.append((target, backup))
    metadata_parents = {target.parent for target, _backup in targets}
    for parent in metadata_parents:
        for candidate in parent.glob("anet_fabric-*.dist-info"):
            _remove_path(candidate)
        for candidate in parent.glob("anet_fabric.egg-info"):
            _remove_path(candidate)
    for target, _backup in targets:
        _remove_path(target)
    for target, backup in targets:
        if backup.is_dir():
            shutil.copytree(backup, target, symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)


def _prune_rollback_snapshots(home: Path, *, keep: int = 2) -> None:
    root = Path(home) / "control-cache" / "rollback"
    if not root.is_dir():
        return
    snapshots = sorted(
        (
            item
            for item in root.iterdir()
            if item.is_dir() and item.name.startswith("snapshot-")
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    for old in snapshots[max(0, keep) :]:
        shutil.rmtree(old, ignore_errors=True)


def _verify_software(software: dict[str, Any]) -> None:
    expected_version = str(software.get("version", "")).strip()
    if expected_version and _current_version() != expected_version:
        raise RemoteControlError(
            "installed Anet version does not match the control page"
        )
    try:
        subprocess.run(
            [sys.executable, "-m", "anet", "--version"],
            check=True,
        )
    except Exception as exc:
        raise RemoteControlError("installed Anet CLI verification failed") from exc


def _download(url: str, destination: Path, *, timeout: float) -> None:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or _is_windows_path(url):
        source = Path(url).expanduser().resolve()
        destination.write_bytes(source.read_bytes())
        return
    if parsed.scheme == "file":
        source = _local_path_from_url(url).resolve()
        destination.write_bytes(source.read_bytes())
        return
    if parsed.scheme not in {"http", "https"}:
        raise RemoteControlError(f"unsupported software URL scheme: {parsed.scheme}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Anet-Control/0.12.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_PAGE_BYTES * 8 + 1)
    except Exception as exc:
        raise RemoteControlError(f"failed to download software: {url}") from exc
    if len(data) > MAX_PAGE_BYTES * 8:
        raise RemoteControlError("software package exceeds the prototype size limit")
    destination.write_bytes(data)


def _install_software(home: Path, software: dict[str, Any], state: dict[str, Any]) -> bool:
    source = str(software.get("wheel_url", "") or software.get("repo_url", "")).strip()
    if not source:
        return False
    source_ref = _normalize_repository_ref(software.get("repo_ref", ""))
    software_key = _json_digest(software)
    if state.get("software_key") == software_key:
        return False
    target_version = str(software.get("version", "")).strip()
    # The one-click installer has already installed the initial page's
    # version, but it does not yet have a remote-control state file. Record
    # that first observation without reinstalling. Once a state key exists,
    # a changed wheel/repository must still be applied even when the package
    # keeps the same version (for example, a rebuilt development wheel).
    if (
        target_version
        and target_version == _current_version()
        and not state.get("software_key")
    ):
        state["software_key"] = software_key
        return False

    cache = Path(home) / "control-cache"
    cache.mkdir(parents=True, exist_ok=True)
    if "wheel_url" in software or source.lower().endswith(".whl"):
        wheel = cache / "anet-update.whl"
        _download(source, wheel, timeout=120.0)
        expected = str(software.get("sha256", "")).strip().lower()
        if expected:
            observed = hashlib.sha256(wheel.read_bytes()).hexdigest().lower()
            if observed != expected:
                raise RemoteControlError("software SHA-256 does not match the control page")
        package_arguments = [
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ]
    else:
        package = _git_source(source, source_ref)
        package_arguments = [
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            package,
        ]
    uv = shutil.which("uv.exe") or shutil.which("uv")
    if uv:
        command = [uv, "pip", "--python", sys.executable, *package_arguments]
    else:
        command = [sys.executable, "-m", "pip", *package_arguments]
    LOGGER.info("installing remote Anet software from %s", source)
    snapshot = _snapshot_installed_package(home)
    try:
        subprocess.run(command, check=True)
        _verify_software(software)
    except Exception:
        if snapshot is not None:
            try:
                _restore_package_snapshot(snapshot)
                LOGGER.warning("restored Anet package after failed software update")
            except Exception as rollback_exc:
                raise RemoteControlError(
                    "software update failed and package rollback failed"
                ) from rollback_exc
        raise
    if snapshot is not None:
        state["rollback_path"] = str(snapshot)
        _prune_rollback_snapshots(home)
    state["software_key"] = software_key
    return True


def _sync_remote_control_unlocked(
    home: Path,
    *,
    url: str | None = None,
    trusted_keys: dict[str, str] | None = None,
    apply_software: bool = True,
) -> dict[str, Any]:
    """Fetch and apply one control page while the caller owns the home lock."""

    home = Path(home).expanduser().resolve()
    settings = _load_control_settings(home, url)
    page_url = str(settings["url"]).strip()
    default_poll_seconds = _bounded_interval(
        settings.get("interval", DEFAULT_POLL_SECONDS)
    )
    configured_trusted_keys = _normalise_trusted_keys(
        settings.get("trusted_keys", {})
    )
    trusted_keys = (
        configured_trusted_keys
        if trusted_keys is None
        else _normalise_trusted_keys(trusted_keys)
    )
    state = _load_state(home)
    now_ms = _now_ms()
    raw = _read_json_url(page_url, timeout=20.0)
    document = _normalise_document(
        raw,
        source_url=page_url,
        visited=set(),
        depth=0,
        sources=[],
        trusted_keys=trusted_keys,
        now_ms=now_ms,
        default_poll_seconds=default_poll_seconds,
    )
    digest = _json_digest(document)
    sequence = int(document.get("sequence", 0))
    current_sequence = int(state.get("sequence", -1))
    current_digest = str(state.get("digest", ""))
    if sequence < current_sequence:
        return {
            "ok": True,
            "changed": False,
            "stale": True,
            "sequence": sequence,
            "current_sequence": current_sequence,
            "sources": document["sources"],
            "poll_seconds": document["poll_seconds"],
        }
    if (
        document["control_signed"]
        and sequence == current_sequence
        and current_digest
        and digest != current_digest
    ):
        raise RemoteControlError(
            "signed control page reused a sequence number with different content"
        )
    if digest == current_digest:
        state["last_sync_ms"] = now_ms
        atomic_json(home / CONTROL_STATE_NAME, state, private=True)
        return {
            "ok": True,
            "changed": False,
            "sequence": sequence,
            "digest": digest,
            "control_signed": document["control_signed"],
            "control_key_id": document["control_key_id"],
            "control_expires_ms": document["control_expires_ms"],
            "sources": document["sources"],
            "poll_seconds": document["poll_seconds"],
        }

    control_snapshot = _snapshot_control_files(home)
    try:
        config_changed = _apply_config(
            home,
            document["config"],
            cross_platform_windows_wsl=bool(
                document.get("cross_platform_windows_wsl", False)
            ),
        )
        nodes_added, nodes_updated = _apply_nodes(home, document["nodes"])
        nodes_changed = nodes_added + nodes_updated
        software_updated = False
        if apply_software:
            software_updated = _install_software(home, document["software"], state)
    except Exception:
        try:
            _restore_control_files(control_snapshot)
        except Exception as rollback_exc:
            raise RemoteControlError(
                "remote control sync failed and node-file rollback failed"
            ) from rollback_exc
        raise
    state.update(
        {
            "version": CONTROL_VERSION,
            "url": page_url,
            "sequence": sequence,
            "digest": digest,
            "network": document["network"],
            "repo_url": document["repo_url"],
            "sources": document["sources"],
            "last_sync_ms": now_ms,
            "control_signed": document["control_signed"],
            "control_key_id": document["control_key_id"],
            "control_issued_ms": document["control_issued_ms"],
            "control_expires_ms": document["control_expires_ms"],
        }
    )
    atomic_json(home / CONTROL_STATE_NAME, state, private=True)
    return {
        "ok": True,
        "changed": bool(config_changed or nodes_changed or software_updated),
        "config_changed": config_changed,
        "nodes_added": nodes_added,
        "nodes_updated": nodes_updated,
        "software_updated": software_updated,
        "restart_required": bool(config_changed or nodes_changed),
        "sequence": sequence,
        "digest": digest,
        "control_signed": document["control_signed"],
        "control_key_id": document["control_key_id"],
        "control_expires_ms": document["control_expires_ms"],
        "network": document["network"],
        "repo_url": document["repo_url"],
        "sources": document["sources"],
        "poll_seconds": document["poll_seconds"],
    }


def sync_remote_control(
    home: Path,
    *,
    url: str | None = None,
    trusted_keys: dict[str, str] | None = None,
    apply_software: bool = True,
) -> dict[str, Any]:
    """Serialize one-shot control syncs with the persistent supervisor."""

    home = Path(home).expanduser().resolve()
    with SupervisorLock(home):
        return _sync_remote_control_unlocked(
            home,
            url=url,
            trusted_keys=trusted_keys,
            apply_software=apply_software,
        )


async def _wait_for_child_or_interval(
    child: asyncio.subprocess.Process | None,
    delay: float,
) -> int | None:
    """Wait for the next control poll or notice a server child exit first."""

    interval_task = asyncio.create_task(asyncio.sleep(max(5.0, delay)))
    if child is None:
        await interval_task
        return None
    child_task = asyncio.create_task(child.wait())
    try:
        done, _pending = await asyncio.wait(
            (interval_task, child_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if child_task in done:
            return child_task.result()
        return None
    finally:
        for task in (interval_task, child_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(interval_task, child_task, return_exceptions=True)


async def run_supervisor(
    home: Path,
    *,
    url: str | None = None,
    trusted_keys: dict[str, str] | None = None,
    interval: float | None = None,
    once: bool = False,
    apply_software: bool = True,
) -> dict[str, Any] | None:
    """Run the control client and keep an Anet server child alive."""

    home = Path(home).expanduser().resolve()
    supervisor_lock = SupervisorLock(home)
    supervisor_lock.acquire()
    child: asyncio.subprocess.Process | None = None
    next_interval = interval or DEFAULT_POLL_SECONDS

    async def stop_child() -> None:
        nonlocal child
        if child is None or child.returncode is not None:
            child = None
            return
        child.terminate()
        try:
            await asyncio.wait_for(child.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            child.kill()
            await child.wait()
        child = None

    async def start_child() -> None:
        nonlocal child
        if child is not None and child.returncode is None:
            return
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "anet",
            "--home",
            str(home),
            "serve",
        )
        LOGGER.info("Anet server child started: PID=%s", child.pid)

    try:
        while True:
            try:
                result = await asyncio.to_thread(
                    _sync_remote_control_unlocked,
                    home,
                    url=url,
                    trusted_keys=trusted_keys,
                    apply_software=apply_software,
                )
                next_interval = interval or float(
                    result.get("poll_seconds", DEFAULT_POLL_SECONDS)
                )
                LOGGER.info(
                    "remote control sync: changed=%s sequence=%s sources=%s",
                    result.get("changed"),
                    result.get("sequence"),
                    len(result.get("sources", [])),
                )
                if result.get("software_updated"):
                    await stop_child()
                    LOGGER.info("restarting supervisor after software update")
                    supervisor_lock.release()
                    os.execv(
                        sys.executable,
                        [sys.executable, "-m", "anet", *sys.argv[1:]],
                    )
                if once:
                    return result
                if result.get("restart_required"):
                    await stop_child()
                await start_child()
            except Exception as exc:
                if once:
                    raise
                LOGGER.warning("remote control sync failed: %s", exc)
                await start_child()
            if once:
                return None
            exit_code = await _wait_for_child_or_interval(child, next_interval)
            if exit_code is not None and child is not None:
                LOGGER.warning(
                    "Anet server child exited unexpectedly: returncode=%s; "
                    "retrying in 5 seconds",
                    exit_code,
                )
                child = None
                await asyncio.sleep(5.0)
    finally:
        try:
            await stop_child()
        finally:
            supervisor_lock.release()


def control_settings_path(home: Path) -> Path:
    return Path(home).expanduser().resolve() / CONTROL_SETTINGS_NAME


def write_control_settings(
    home: Path,
    *,
    url: str,
    interval: float = DEFAULT_POLL_SECONDS,
    trusted_keys: dict[str, str] | None = None,
) -> Path:
    path = control_settings_path(home)
    normalized_keys = _normalise_trusted_keys(trusted_keys or {})
    value: dict[str, Any] = {
        "version": CONTROL_VERSION,
        "url": str(url),
        "interval": _bounded_interval(interval),
    }
    if normalized_keys:
        value["trusted_keys"] = normalized_keys
    atomic_json(
        path,
        value,
        private=True,
    )
    return path
