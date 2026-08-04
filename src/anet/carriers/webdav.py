from __future__ import annotations

import base64
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree as ET

from ..config import WebDAVCarrierConfig
from ..identity import Identity, PeerCard
from ..packet import inspect_packet
from .base import CarrierFrame
from .directory import (
    CHANNEL_EPOCH_SECONDS,
    DIRECTORY_CARRIER_VERSION,
    LEGACY_DIRECTORY_CARRIER_VERSION,
    MAX_DROP_BYTES,
    DirectoryCarrier,
)

if TYPE_CHECKING:
    from ..node import AnetNode


MAX_LISTING_BYTES = 1024 * 1024
_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{48}|[0-9a-f]{40}\.drop)$")
_COLLECTION_RE = re.compile(r"^(?:[A-Za-z0-9_-]{27}|ch-[A-Za-z0-9_-]{27})$")
_TRANSPORT_RETRY_METHODS = frozenset(
    {"DELETE", "GET", "MKCOL", "PROPFIND"}
)
_TRANSPORT_RETRY_ATTEMPTS = 3


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@dataclass(frozen=True)
class _RemoteFrame:
    frame: CarrierFrame
    url: str


class WebDAVCarrier:
    """Outbound-only encrypted mailbox over a standards-compatible WebDAV store."""

    name = "webdav-v2"

    def __init__(
        self,
        config: WebDAVCarrierConfig,
        identity: Identity,
        *,
        codec_cache: Path,
    ) -> None:
        self.config = config
        self.identity = identity
        self.codec = DirectoryCarrier(codec_cache, identity)
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._authorization = self._authorization_header()
        self._collection_cache: set[str] | None = None

    def _authorization_header(self) -> str:
        if self.config.bearer_env:
            token = os.environ.get(self.config.bearer_env, "")
            if not token:
                raise ValueError(
                    f"WebDAV bearer token environment variable is empty: {self.config.bearer_env}"
                )
            return f"Bearer {token}"
        if self.config.username_env:
            username = os.environ.get(self.config.username_env, "")
            password = os.environ.get(self.config.password_env, "")
            if not username or not password:
                raise ValueError("WebDAV basic-auth environment variables are empty")
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode(
                "ascii"
            )
            return f"Basic {encoded}"
        return ""

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int],
        max_bytes: int = MAX_DROP_BYTES,
    ) -> tuple[int, bytes, dict[str, str]]:
        request_headers = {
            "User-Agent": "Anet-WebDAV/1",
            "Accept": "*/*",
            "Connection": "close",
            **(headers or {}),
        }
        if self._authorization:
            request_headers["Authorization"] = self._authorization
        method = str(method).upper()
        attempts = (
            _TRANSPORT_RETRY_ATTEMPTS
            if method in _TRANSPORT_RETRY_METHODS
            else 1
        )
        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=data, headers=request_headers, method=method
            )
            try:
                response = self._opener.open(
                    request, timeout=self.config.timeout
                )
            except urllib.error.HTTPError as exc:
                if exc.code not in expected:
                    raise ConnectionError(
                        f"WebDAV {method} failed with HTTP {exc.code}"
                    ) from exc
                response = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 >= attempts:
                    raise ConnectionError(
                        f"WebDAV {method} transport failed"
                    ) from exc
                time.sleep(
                    min(
                        0.25,
                        max(0.01, self.config.timeout / 10.0)
                        * (attempt + 1),
                    )
                )
                continue
            try:
                with response:
                    status = int(response.status)
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError("WebDAV response exceeds size limit")
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 >= attempts:
                    raise ConnectionError(
                        f"WebDAV {method} transport failed"
                    ) from exc
                time.sleep(
                    min(
                        0.25,
                        max(0.01, self.config.timeout / 10.0)
                        * (attempt + 1),
                    )
                )
                continue
            if status not in expected:
                raise ConnectionError(
                    f"WebDAV {method} returned unexpected HTTP {status}"
                )
            return status, body, response_headers
        raise AssertionError("unreachable WebDAV request retry state")

    def _channel_url(self, channel: Any) -> str:
        name = urllib.parse.quote(str(channel.locator), safe="")
        return f"{self.config.base_url}/{name}/"

    def _object_url(self, channel_url: str, filename: str) -> str:
        if not _OBJECT_RE.fullmatch(filename):
            raise ValueError("invalid WebDAV drop filename")
        return channel_url + urllib.parse.quote(filename, safe="")

    def _ensure_collection(self, channel_url: str) -> None:
        self._request("MKCOL", channel_url, expected={201, 405}, max_bytes=4096)

    def _put(self, channel_url: str, filename: str, wire: bytes) -> bool:
        self._ensure_collection(channel_url)
        status, _, _ = self._request(
            "PUT",
            self._object_url(channel_url, filename),
            data=wire,
            headers={"Content-Type": "application/octet-stream", "If-None-Match": "*"},
            expected={201, 204, 412},
            max_bytes=4096,
        )
        if self._collection_cache is not None:
            self._collection_cache.add(
                urllib.parse.unquote(
                    Path(urllib.parse.urlsplit(channel_url).path.rstrip("/")).name
                )
            )
        return status != 412

    def _list_collections(self) -> set[str]:
        if self._collection_cache is not None:
            return set(self._collection_cache)
        request_body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        )
        base_url = f"{self.config.base_url}/"
        _, body, _ = self._request(
            "PROPFIND",
            base_url,
            data=request_body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expected={207},
            max_bytes=MAX_LISTING_BYTES,
        )
        root = ET.fromstring(body)
        base_path = (
            urllib.parse.unquote(urllib.parse.urlsplit(base_url).path).rstrip("/") + "/"
        )
        names: set[str] = set()
        for href in root.findall(".//{DAV:}href"):
            parsed_path = urllib.parse.unquote(
                urllib.parse.urlsplit(href.text or "").path
            )
            if not parsed_path.endswith("/") or not parsed_path.startswith(base_path):
                continue
            relative = parsed_path[len(base_path) :].strip("/")
            if "/" not in relative and _COLLECTION_RE.fullmatch(relative):
                names.add(relative)
        self._collection_cache = names
        return set(names)

    def publish_packet(self, peer: PeerCard, raw: bytes, *, depth: int) -> bool:
        info = inspect_packet(raw)
        depth = int(depth)
        if depth < 1 or depth > info.max_hops:
            raise ValueError("invalid WebDAV carrier relay depth")
        channel, filename, wire = self.codec.encode_frame(
            peer,
            kind="packet",
            packet_id=info.packet_id,
            raw=raw,
            depth=depth,
            version=(
                DIRECTORY_CARRIER_VERSION
                if "webdav-carrier-v2" in peer.capabilities
                else LEGACY_DIRECTORY_CARRIER_VERSION
            ),
        )
        return self._put(self._channel_url(channel), filename, wire)

    def publish_ack(self, peer: PeerCard, packet_id: str) -> bool:
        channel, filename, wire = self.codec.encode_frame(
            peer,
            kind="ack",
            packet_id=packet_id,
            version=(
                DIRECTORY_CARRIER_VERSION
                if "webdav-carrier-v2" in peer.capabilities
                else LEGACY_DIRECTORY_CARRIER_VERSION
            ),
        )
        return self._put(self._channel_url(channel), filename, wire)

    def _list_names(self, channel_url: str, *, limit: int) -> list[str]:
        request_body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        )
        status, body, _ = self._request(
            "PROPFIND",
            channel_url,
            data=request_body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expected={207, 404},
            max_bytes=MAX_LISTING_BYTES,
        )
        if status == 404:
            return []
        root = ET.fromstring(body)
        names: set[str] = set()
        for href in root.findall(".//{DAV:}href"):
            parsed = urllib.parse.urlsplit(href.text or "")
            name = urllib.parse.unquote(Path(parsed.path.rstrip("/")).name)
            if _OBJECT_RE.fullmatch(name):
                names.add(name)
        return sorted(names)[: max(1, min(int(limit), 1024))]

    def scan(
        self, peer: PeerCard, *, limit: int = 128
    ) -> tuple[list[_RemoteFrame], list[str]]:
        limit = max(1, min(int(limit), 1024))
        frames: list[_RemoteFrame] = []
        rejected: list[str] = []
        collections = self._list_collections()
        for channel in self.codec.inbound_channels(peer):
            if channel.locator not in collections:
                continue
            remaining = limit - len(frames) - len(rejected)
            if remaining <= 0:
                break
            channel_url = self._channel_url(channel)
            for name in self._list_names(channel_url, limit=remaining):
                url = self._object_url(channel_url, name)
                try:
                    _, wire, _ = self._request("GET", url, expected={200})
                    frame = self.codec.decode_frame(
                        Path(name), wire, peer, channel=channel
                    )
                    frames.append(_RemoteFrame(frame, url))
                except Exception:
                    rejected.append(url)
        return frames, rejected

    def consume(self, url: str) -> None:
        self._request("DELETE", url, expected={204, 404}, max_bytes=4096)

    def quarantine(self, url: str) -> None:
        quarantine_url = f"{self.config.base_url}/quarantine/"
        try:
            self._ensure_collection(quarantine_url)
            destination = quarantine_url + secrets.token_hex(20) + ".drop"
            self._request(
                "MOVE",
                url,
                headers={"Destination": destination, "Overwrite": "F"},
                expected={201, 204},
                max_bytes=4096,
            )
        except Exception:
            self.consume(url)


def sync_webdav_once(
    node: AnetNode,
    config: WebDAVCarrierConfig,
    *,
    peer_ids: tuple[str, ...] | list[str] = (),
    push_peer_ids: tuple[str, ...] | list[str] | None = None,
    qos_allow: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    limit: int = 128,
    retry_after_ms: int = 300_000,
) -> dict[str, Any]:
    selected = set(str(item) for item in peer_ids if str(item))
    cards = [
        card for card in node.peers.all() if not selected or card.node_id in selected
    ]
    if selected - {card.node_id for card in cards}:
        missing = selected - {card.node_id for card in cards}
        raise KeyError(f"unknown WebDAV peer(s): {', '.join(sorted(missing))}")
    push_selected = (
        {card.node_id for card in cards}
        if push_peer_ids is None
        else {str(item) for item in push_peer_ids if str(item)}
    )
    unknown_push = push_selected - {card.node_id for card in cards}
    if unknown_push:
        raise KeyError(
            f"unknown WebDAV push peer(s): {', '.join(sorted(unknown_push))}"
        )
    carrier = WebDAVCarrier(
        config,
        node.identity,
        codec_cache=node.config.home / ".carrier-codec" / config.name,
    )
    path_id = config.path_id
    stats: dict[str, Any] = {
        "carrier": carrier.name,
        "mailbox_epoch": carrier.codec._epoch_at(int(time.time() * 1000)),
        "mailbox_epoch_seconds": CHANNEL_EPOCH_SECONDS,
        "legacy_receive": True,
        "base_url": config.base_url,
        "peers": len(cards),
        "pulled_packets": 0,
        "pulled_acks": 0,
        "pushed_packets": 0,
        "existing_packets": 0,
        "rejected": 0,
    }

    for card in cards:
        frames, rejected = carrier.scan(card, limit=limit)
        for url in rejected:
            carrier.quarantine(url)
            stats["rejected"] += 1
        for remote in frames:
            try:
                frame = remote.frame
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
                carrier.consume(remote.url)
            except Exception:
                carrier.quarantine(remote.url)
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
