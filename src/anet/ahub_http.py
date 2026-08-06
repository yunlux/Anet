from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature

from .ahub import (
    DEFAULT_MAX_RELAY_BYTES_EACH_DIRECTION,
    DEFAULT_MAX_RELAY_DURATION_MS,
    DEFAULT_RELAY_RESERVATION_TTL_MS,
    MAX_CLAIM_BATCH,
    MAX_CLAIM_LEASE_MS,
    AhubRequest,
    AhubService,
    ClaimedPacket,
    CustodyReceipt,
    DestinationSettlement,
    RelayReservation,
    issue_ahub_request,
    issue_destination_settlement,
)
from .ahub_relay import RelayCoordinator
from .control_plane import NodeDescriptor, ReachabilityRecord
from .encoding import MAX_WIRE_BYTES, b64d, b64e
from .identity import Identity


MAX_AHUB_JSON_BYTES = 256 * 1024
MAX_AHUB_HTTP_BODY_BYTES = MAX_WIRE_BYTES + 1
MAX_AHUB_HTTP_RESPONSE_BYTES = 24 * 1024 * 1024
LOGGER = logging.getLogger(__name__)
_NODE_PATH_RE = re.compile(r"^/v1/nodes/(an1[a-z2-7]{32})$")
_DESCRIPTOR_PATH_RE = re.compile(r"^/v1/descriptors/(an1[a-z2-7]{32})$")
_REACHABILITY_PATH_RE = re.compile(
    r"^/v1/reachability/(an1[a-z2-7]{32})$"
)
_SETTLE_PATH_RE = re.compile(r"^/v1/mailbox/([0-9a-f]{32})/settle$")
_SETTLEMENT_ACK_PATH_RE = re.compile(
    r"^/v1/mailbox/settlements/([0-9a-f]{32})/ack$"
)
_RELAY_PATH_RE = re.compile(r"^/v1/relay/([0-9a-f]{32})$")
_RELAY_DISCOVERY_PATH_RE = re.compile(
    r"^/v1/relay/reservations/(an1[a-z2-7]{32})$"
)


class AhubHTTPError(ConnectionError):
    def __init__(self, category: str, *, status: int = 0) -> None:
        self.category = str(category)
        self.status = int(status)
        suffix = f" status={self.status}" if self.status else ""
        super().__init__(f"ahub_{self.category}{suffix}")


@dataclass
class AhubRelayConnection:
    websocket: Any
    peer_node_id: str
    max_duration_ms: int
    max_bytes_each_direction: int
    max_frame_bytes: int

    async def send(self, raw: bytes) -> None:
        frame = bytes(raw)
        if len(frame) > self.max_frame_bytes:
            raise ValueError("Relay frame exceeds the negotiated limit")
        await self.websocket.send(frame)

    async def receive(self) -> bytes:
        value = await self.websocket.recv()
        if not isinstance(value, bytes):
            await self.close(code=1003)
            raise ValueError("Relay emitted a non-binary data frame")
        return value

    async def close(self, *, code: int = 1000) -> None:
        await self.websocket.close(code=code)


def ahub_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_object(raw: bytes, *, max_bytes: int = MAX_AHUB_JSON_BYTES) -> dict[str, Any]:
    if len(raw) > max_bytes:
        raise ValueError("Ahub JSON body is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Ahub JSON body") from exc
    if not isinstance(value, dict):
        raise ValueError("Ahub JSON body must be an object")
    return value


def _bounded_fields(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    if not required <= set(value) or set(value) - required - optional:
        raise ValueError("Ahub JSON body has missing or unknown fields")


DEFAULT_AHUB_REQUESTS_PER_MINUTE = 600
DEFAULT_AHUB_REQUEST_BURST = 120
MAX_AHUB_RATE_LIMIT_BUCKETS = 10_000


@dataclass(frozen=True)
class AhubRateLimit:
    """Bounded in-process request rate limit for one Ahub worker."""

    requests_per_minute: int = DEFAULT_AHUB_REQUESTS_PER_MINUTE
    burst: int = DEFAULT_AHUB_REQUEST_BURST
    max_buckets: int = MAX_AHUB_RATE_LIMIT_BUCKETS

    def __post_init__(self) -> None:
        values = (
            self.requests_per_minute,
            self.burst,
            self.max_buckets,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("Ahub rate limits must be positive integers")
        if self.max_buckets > MAX_AHUB_RATE_LIMIT_BUCKETS:
            raise ValueError("Ahub rate-limit bucket count is too large")


@dataclass
class _RateBucket:
    tokens: float
    updated: float


class AhubRateLimiter:
    """Token bucket limiter keyed by the ASGI peer address.

    This is intentionally a bounded per-worker guard. A reverse proxy or
    shared edge limiter is still required for a multi-worker/public service.
    """

    def __init__(
        self,
        config: AhubRateLimit | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = AhubRateLimit() if config is None else config
        self._clock = clock
        self._rate_per_second = self.config.requests_per_minute / 60.0
        self._buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._lock = threading.Lock()
        self._limited_requests = 0

    def _prune(self, now: float) -> None:
        stale_after = max(
            60.0,
            (self.config.burst / self._rate_per_second) * 2.0,
        )
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            if now - bucket.updated <= stale_after:
                break
            self._buckets.pop(key)

    def allow(self, key: str) -> tuple[bool, int]:
        """Consume one token and return ``(allowed, retry_after_seconds)``."""

        normalized = str(key)[:512] or "unknown"
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(normalized)
            if bucket is None:
                while len(self._buckets) >= self.config.max_buckets:
                    self._buckets.popitem(last=False)
                bucket = _RateBucket(
                    tokens=float(self.config.burst),
                    updated=now,
                )
            else:
                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(
                    float(self.config.burst),
                    bucket.tokens + elapsed * self._rate_per_second,
                )
                bucket.updated = now
            if bucket.tokens < 1.0:
                retry_after = max(
                    1,
                    math.ceil((1.0 - bucket.tokens) / self._rate_per_second),
                )
                self._limited_requests += 1
                self._buckets[normalized] = bucket
                self._buckets.move_to_end(normalized)
                return False, retry_after
            bucket.tokens -= 1.0
            self._buckets[normalized] = bucket
            self._buckets.move_to_end(normalized)
            return True, 0

    def status(self) -> dict[str, int]:
        """Return bounded, non-identifying in-process limiter counters."""

        with self._lock:
            return {
                "active_buckets": len(self._buckets),
                "limited_requests": self._limited_requests,
            }


class AhubASGI:
    """Small framework-free HTTP adapter; TLS is a deployment responsibility."""

    def __init__(
        self,
        service: AhubService,
        *,
        rate_limit: AhubRateLimit | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.relay = RelayCoordinator(service.ahub.limits)
        self.rate_limit = AhubRateLimit() if rate_limit is None else rate_limit
        self.rate_limiter = AhubRateLimiter(
            self.rate_limit,
            clock=clock,
        )

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            return
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        headers = {
            bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        allowed, retry_after = self.rate_limiter.allow(
            self._rate_limit_key(scope)
        )
        if not allowed:
            if scope_type == "websocket":
                await self.relay._close(send, 4429, "rate_limited")
                LOGGER.info(
                    "ahub_websocket route=%s status=rate_limited",
                    self._route_name(method, path),
                )
                return
            await self._respond(
                send,
                429,
                {
                    "error": "rate_limited",
                    "detail": "request rate limit exceeded",
                },
                headers={"retry-after": str(retry_after)},
            )
            LOGGER.info(
                "ahub_request method=%s route=%s status=429 body_bytes=0",
                method,
                self._route_name(method, path),
            )
            return
        if scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        started = time.monotonic()
        body_size = 0
        try:
            body = await self._read_body(receive)
            body_size = len(body)
            status, result = self._dispatch(method, path, headers, body)
        except PermissionError as exc:
            status, result = 403, {"error": "forbidden", "detail": str(exc)}
        except InvalidSignature:
            status, result = 403, {"error": "invalid_signature"}
        except LookupError as exc:
            status, result = 404, {"error": "not_found", "detail": str(exc)}
        except OverflowError as exc:
            status, result = 429, {"error": "quota_exceeded", "detail": str(exc)}
        except ValueError as exc:
            status, result = 400, {"error": "invalid_request", "detail": str(exc)}
        except Exception:
            status, result = 500, {"error": "internal_error"}
        LOGGER.info(
            "ahub_request method=%s route=%s status=%d body_bytes=%d elapsed_ms=%.1f",
            method,
            self._route_name(method, path),
            status,
            body_size,
            (time.monotonic() - started) * 1000,
        )
        await self._respond(send, status, result)

    @staticmethod
    def _rate_limit_key(scope: dict[str, Any]) -> str:
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            return f"peer:{str(client[0])[:255]}"
        return "peer:unknown"

    @staticmethod
    def _route_name(method: str, path: str) -> str:
        if method == "GET" and path == "/healthz":
            return "health"
        if _DESCRIPTOR_PATH_RE.fullmatch(path):
            return "descriptor"
        if _REACHABILITY_PATH_RE.fullmatch(path):
            return "reachability"
        if _NODE_PATH_RE.fullmatch(path):
            return "node_lookup"
        if method == "POST" and path == "/v1/mailbox":
            return "mailbox_submit"
        if method == "POST" and path == "/v1/mailbox/claims":
            return "mailbox_claim"
        if method == "POST" and path == "/v1/relay/reservations":
            return "relay_reservation"
        if method == "GET" and _RELAY_DISCOVERY_PATH_RE.fullmatch(path):
            return "relay_discovery"
        if method == "GET" and _RELAY_PATH_RE.fullmatch(path):
            return "relay_stream"
        if method == "POST" and path == "/v1/mailbox/settlements":
            return "mailbox_settlements"
        if _SETTLEMENT_ACK_PATH_RE.fullmatch(path):
            return "mailbox_settlement_ack"
        if _SETTLE_PATH_RE.fullmatch(path):
            return "mailbox_settle"
        return "unknown"

    async def _handle_websocket(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        path = str(scope.get("path", ""))
        started = time.monotonic()
        status = "rejected"
        try:
            event = await receive()
            if event.get("type") != "websocket.connect":
                raise ValueError("invalid WebSocket opening event")
            match = _RELAY_PATH_RE.fullmatch(path)
            if match is None or scope.get("query_string", b""):
                raise LookupError("unknown Relay route")
            headers = {
                bytes(key).decode("latin-1"): bytes(value).decode("latin-1")
                for key, value in scope.get("headers", ())
            }
            auth = self._auth(
                headers,
                method="GET",
                path=path,
                body=b"",
            )
            reservation = self.service.authorize_relay(
                auth,
                b"",
                reservation_id=match.group(1),
            )
            await send(
                {
                    "type": "websocket.accept",
                    "headers": [
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            status = "accepted"
            await self.relay.handle(
                reservation,
                auth.node_id,
                receive,
                send,
            )
        except PermissionError:
            status = "forbidden"
            await self.relay._close(send, 4403, "forbidden")
        except InvalidSignature:
            status = "invalid_signature"
            await self.relay._close(send, 4403, "forbidden")
        except LookupError:
            status = "not_found"
            await self.relay._close(send, 4404, "not_found")
        except (OverflowError, ValueError):
            status = "invalid"
            await self.relay._close(send, 4400, "invalid_request")
        except Exception:
            status = "error"
            await self.relay._close(send, 1011, "internal_error")
        LOGGER.info(
            "ahub_websocket route=relay_stream status=%s elapsed_ms=%.1f",
            status,
            (time.monotonic() - started) * 1000,
        )

    @staticmethod
    async def _read_body(
        receive: Callable[[], Awaitable[dict[str, Any]]],
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                raise ValueError("request disconnected")
            if event.get("type") != "http.request":
                continue
            chunk = bytes(event.get("body", b""))
            size += len(chunk)
            if size > MAX_AHUB_HTTP_BODY_BYTES:
                raise ValueError("Ahub request body is too large")
            chunks.append(chunk)
            if not event.get("more_body", False):
                return b"".join(chunks)

    def _auth(
        self,
        headers: dict[str, str],
        *,
        method: str,
        path: str,
        body: bytes,
    ) -> AhubRequest:
        return AhubRequest.from_headers(
            headers,
            method=method,
            path=path,
            body=body,
        )

    def _dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/healthz":
            if body:
                raise ValueError("health request body must be empty")
            healthy = self.service.health()
            return (
                200 if healthy else 503,
                {
                    "status": "ok" if healthy else "unhealthy",
                    "service": "anet-ahub",
                    "protocol": 1,
                },
            )

        match = _DESCRIPTOR_PATH_RE.fullmatch(path)
        if method == "PUT" and match:
            descriptor = NodeDescriptor.from_dict(_json_object(body))
            if descriptor.node_id != match.group(1):
                raise ValueError("descriptor path does not match Node ID")
            changed = self.service.publish_descriptor(descriptor)
            return 200, {"accepted": changed, "digest": b64e(descriptor.digest)}

        match = _REACHABILITY_PATH_RE.fullmatch(path)
        if method == "PUT" and match:
            descriptor = self.service.control.current_descriptor(match.group(1))
            if descriptor is None:
                raise LookupError("node has no current descriptor")
            record = ReachabilityRecord.from_dict(
                _json_object(body), descriptor
            )
            if record.node_id != match.group(1):
                raise ValueError("reachability path does not match Node ID")
            changed = self.service.publish_reachability(record)
            return 200, {"accepted": changed, "digest": b64e(record.digest)}

        match = _NODE_PATH_RE.fullmatch(path)
        if method == "GET" and match:
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            descriptor, reachability = self.service.lookup(
                auth, body, match.group(1)
            )
            return 200, {
                "descriptor": descriptor.to_dict(),
                "reachability": (
                    None if reachability is None else reachability.to_dict()
                ),
            }

        if method == "POST" and path == "/v1/relay/reservations":
            value = _json_object(body)
            _bounded_fields(
                value,
                required={"allowed_peer_id"},
                optional={
                    "ttl_ms",
                    "max_duration_ms",
                    "max_bytes_each_direction",
                },
            )
            if not isinstance(value["allowed_peer_id"], str):
                raise ValueError("Relay peer must be a Node ID string")
            limits = {
                "ttl_ms": value.get(
                    "ttl_ms", DEFAULT_RELAY_RESERVATION_TTL_MS
                ),
                "max_duration_ms": value.get(
                    "max_duration_ms", DEFAULT_MAX_RELAY_DURATION_MS
                ),
                "max_bytes_each_direction": value.get(
                    "max_bytes_each_direction",
                    DEFAULT_MAX_RELAY_BYTES_EACH_DIRECTION,
                ),
            }
            if any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in limits.values()
            ):
                raise ValueError("Relay limits must be integers")
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            reservation = self.service.reserve_relay(
                auth,
                body,
                allowed_peer_id=value["allowed_peer_id"],
                ttl_ms=limits["ttl_ms"],
                max_duration_ms=limits["max_duration_ms"],
                max_bytes_each_direction=limits[
                    "max_bytes_each_direction"
                ],
            )
            return 200, {
                "reservation": reservation.to_dict(),
                "max_frame_bytes": (
                    self.service.ahub.limits.max_relay_frame_bytes
                ),
            }

        match = _RELAY_DISCOVERY_PATH_RE.fullmatch(path)
        if method == "GET" and match:
            if body:
                raise ValueError("Relay discovery body must be empty")
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            reservation = self.service.find_relay_reservation(
                auth,
                body,
                owner_id=match.group(1),
            )
            return 200, {"reservation": reservation.to_dict()}

        if method == "POST" and path == "/v1/mailbox":
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            receipt = self.service.submit(auth, body)
            return 200, {
                "packet_id": receipt.packet_id,
                "destination_id": receipt.destination_id,
                "stored": receipt.stored,
                "expires_ms": receipt.expires_ms,
                "meaning": "ahub_custody_only",
            }

        if method == "POST" and path == "/v1/mailbox/claims":
            value = _json_object(body)
            _bounded_fields(
                value,
                required={"limit", "lease_ms"},
                optional={"uploader_id"},
            )
            limit = value["limit"]
            lease_ms = value["lease_ms"]
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("claim limit must be an integer")
            if isinstance(lease_ms, bool) or not isinstance(lease_ms, int):
                raise ValueError("claim lease must be an integer")
            uploader_id = value.get("uploader_id")
            if uploader_id is not None and not isinstance(uploader_id, str):
                raise ValueError("claim uploader must be a Node ID string")
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            claimed = self.service.claim(
                auth,
                body,
                limit=limit,
                lease_ms=lease_ms,
                max_bytes=MAX_WIRE_BYTES,
                uploader_id=uploader_id,
            )
            return 200, {
                "packets": [
                    {
                        "packet_id": item.packet_id,
                        "raw": b64e(item.raw),
                        "depth": item.depth,
                        "claim_token": item.claim_token,
                        "claim_until_ms": item.claim_until_ms,
                        "uploader_id": item.uploader_id,
                        "expires_ms": item.expires_ms,
                    }
                    for item in claimed
                ]
            }

        if method == "POST" and path == "/v1/mailbox/settlements":
            value = _json_object(body)
            _bounded_fields(
                value,
                required={"limit"},
                optional={"destination_id"},
            )
            limit = value["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("settlement limit must be an integer")
            destination_id = value.get("destination_id")
            if destination_id is not None and not isinstance(
                destination_id, str
            ):
                raise ValueError("settlement destination must be a Node ID string")
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            settlements = self.service.settlements(
                auth,
                body,
                limit=limit,
                destination_id=destination_id,
            )
            return 200, {
                "meaning": "destination_settlement_only",
                "settlements": [
                    item.to_dict()
                    for item in settlements
                ],
            }

        match = _SETTLEMENT_ACK_PATH_RE.fullmatch(path)
        if method == "POST" and match:
            if body:
                raise ValueError("settlement acknowledgement body must be empty")
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            acknowledged = self.service.acknowledge_settlement(
                auth,
                body,
                packet_id=match.group(1),
            )
            return 200, {"acknowledged": acknowledged}

        match = _SETTLE_PATH_RE.fullmatch(path)
        if method == "POST" and match:
            value = _json_object(body)
            _bounded_fields(value, required={"claim_token", "proof"})
            if not isinstance(value["claim_token"], str):
                raise ValueError("claim token must be a string")
            if not isinstance(value["proof"], dict):
                raise ValueError("settlement proof must be an object")
            proof = DestinationSettlement.from_dict(value["proof"])
            auth = self._auth(
                headers, method=method, path=path, body=body
            )
            settled = self.service.settle(
                auth,
                body,
                packet_id=match.group(1),
                claim_token=value["claim_token"],
                proof=proof,
            )
            return 200, {"settled": settled}

        raise LookupError("unknown Ahub route")

    @staticmethod
    async def _respond(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        value: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = ahub_json(value)
        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        for key, item in (headers or {}).items():
            response_headers.append(
                (str(key).lower().encode("latin-1"), str(item).encode("latin-1"))
            )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": raw})


@dataclass(frozen=True)
class AhubHTTPClient:
    base_url: str
    identity: Identity
    timeout_seconds: float = 30.0
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("Ahub base URL must be absolute HTTP(S)")
        if parsed.scheme != "https" and not self.allow_insecure_http:
            raise ValueError("Ahub HTTP client requires TLS")
        if parsed.query or parsed.fragment:
            raise ValueError("Ahub base URL cannot contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Ahub base URL cannot contain a path")

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        authenticated: bool = True,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        headers = {"Content-Type": content_type}
        if authenticated:
            headers.update(
                issue_ahub_request(
                    self.identity,
                    method=method,
                    path=path,
                    body=body,
                ).to_headers()
            )
        request = urllib.request.Request(
            self._url(path),
            data=body if method != "GET" else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(MAX_AHUB_HTTP_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.read(MAX_AHUB_JSON_BYTES)
            category = (
                "auth"
                if exc.code in {401, 403}
                else (
                    "quota"
                    if exc.code == 429
                    else (
                        "not_found"
                        if exc.code == 404
                        else (
                            "invalid"
                            if 400 <= exc.code < 500
                            else "server"
                        )
                    )
                )
            )
            raise AhubHTTPError(category, status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AhubHTTPError("transport") from exc
        if len(raw) > MAX_AHUB_HTTP_RESPONSE_BYTES:
            raise ValueError("Ahub response body is too large")
        return _json_object(raw, max_bytes=MAX_AHUB_HTTP_RESPONSE_BYTES)

    def publish_descriptor(self, descriptor: NodeDescriptor) -> bool:
        value = self._request(
            "PUT",
            f"/v1/descriptors/{descriptor.node_id}",
            ahub_json(descriptor.to_dict()),
            authenticated=False,
        )
        return bool(value["accepted"])

    def publish_reachability(self, record: ReachabilityRecord) -> bool:
        value = self._request(
            "PUT",
            f"/v1/reachability/{record.node_id}",
            ahub_json(record.to_dict()),
            authenticated=False,
        )
        return bool(value["accepted"])

    def lookup(
        self, node_id: str
    ) -> tuple[NodeDescriptor, ReachabilityRecord | None]:
        value = self._request("GET", f"/v1/nodes/{node_id}")
        descriptor_raw = value.get("descriptor")
        if not isinstance(descriptor_raw, dict):
            raise ValueError("Ahub returned an invalid descriptor")
        descriptor = NodeDescriptor.from_dict(descriptor_raw)
        reachability_raw = value.get("reachability")
        reachability = (
            None
            if reachability_raw is None
            else ReachabilityRecord.from_dict(reachability_raw, descriptor)
        )
        return descriptor, reachability

    def reserve_relay(
        self,
        allowed_peer_id: str,
        *,
        ttl_ms: int = DEFAULT_RELAY_RESERVATION_TTL_MS,
        max_duration_ms: int = DEFAULT_MAX_RELAY_DURATION_MS,
        max_bytes_each_direction: int = (
            DEFAULT_MAX_RELAY_BYTES_EACH_DIRECTION
        ),
    ) -> RelayReservation:
        body = ahub_json(
            {
                "allowed_peer_id": str(allowed_peer_id),
                "ttl_ms": ttl_ms,
                "max_duration_ms": max_duration_ms,
                "max_bytes_each_direction": max_bytes_each_direction,
            }
        )
        value = self._request(
            "POST", "/v1/relay/reservations", body
        )
        raw = value.get("reservation")
        if not isinstance(raw, dict):
            raise ValueError("Ahub returned an invalid Relay reservation")
        expected = {
            "reservation_id",
            "owner_id",
            "allowed_peer_id",
            "created_ms",
            "expires_ms",
            "max_duration_ms",
            "max_bytes_each_direction",
        }
        if set(raw) != expected:
            raise ValueError("Ahub returned invalid Relay reservation fields")
        reservation = RelayReservation(
            reservation_id=str(raw["reservation_id"]),
            owner_id=str(raw["owner_id"]),
            allowed_peer_id=str(raw["allowed_peer_id"]),
            created_ms=int(raw["created_ms"]),
            expires_ms=int(raw["expires_ms"]),
            max_duration_ms=int(raw["max_duration_ms"]),
            max_bytes_each_direction=int(
                raw["max_bytes_each_direction"]
            ),
        )
        if (
            reservation.owner_id != self.identity.node_id
            or reservation.allowed_peer_id != str(allowed_peer_id)
        ):
            raise ValueError("Ahub returned a mismatched Relay reservation")
        return reservation

    @staticmethod
    def _parse_relay_reservation(value: Any) -> RelayReservation:
        if not isinstance(value, dict):
            raise ValueError("Ahub returned an invalid Relay reservation")
        expected = {
            "reservation_id",
            "owner_id",
            "allowed_peer_id",
            "created_ms",
            "expires_ms",
            "max_duration_ms",
            "max_bytes_each_direction",
        }
        if set(value) != expected:
            raise ValueError("Ahub returned invalid Relay reservation fields")
        return RelayReservation(
            reservation_id=str(value["reservation_id"]),
            owner_id=str(value["owner_id"]),
            allowed_peer_id=str(value["allowed_peer_id"]),
            created_ms=int(value["created_ms"]),
            expires_ms=int(value["expires_ms"]),
            max_duration_ms=int(value["max_duration_ms"]),
            max_bytes_each_direction=int(
                value["max_bytes_each_direction"]
            ),
        )

    def relay_reservation(self, owner_node_id: str) -> RelayReservation:
        owner = str(owner_node_id)
        value = self._request(
            "GET", f"/v1/relay/reservations/{owner}"
        )
        reservation = self._parse_relay_reservation(
            value.get("reservation")
        )
        if (
            reservation.owner_id != owner
            or reservation.allowed_peer_id != self.identity.node_id
        ):
            raise ValueError("Ahub returned a mismatched Relay reservation")
        return reservation

    async def open_relay(
        self,
        reservation_id: str,
    ) -> AhubRelayConnection:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise RuntimeError(
                "Relay client requires the optional 'ahub' dependency"
            ) from exc
        path = f"/v1/relay/{reservation_id}"
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        uri = f"{scheme}://{parsed.netloc}{path}"
        headers = issue_ahub_request(
            self.identity,
            method="GET",
            path=path,
        ).to_headers()
        websocket = None
        try:
            websocket = await connect(
                uri,
                additional_headers=headers,
                compression=None,
                max_size=MAX_WIRE_BYTES,
                proxy=None,
                user_agent_header=None,
                open_timeout=self.timeout_seconds,
                close_timeout=min(self.timeout_seconds, 10.0),
            )
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await websocket.recv()
                    if not isinstance(message, str):
                        raise ValueError(
                            "Relay sent data before the ready control"
                        )
                    control = json.loads(message)
                    if not isinstance(control, dict):
                        raise ValueError("Relay returned invalid control data")
                    if control.get("type") == "relay.waiting":
                        continue
                    if control.get("type") != "relay.ready":
                        raise ValueError("Relay returned unknown control data")
                    required = {
                        "type",
                        "peer_node_id",
                        "max_duration_ms",
                        "max_bytes_each_direction",
                        "max_frame_bytes",
                    }
                    if set(control) != required:
                        raise ValueError(
                            "Relay returned invalid ready fields"
                        )
                    integer_values = (
                        control["max_duration_ms"],
                        control["max_bytes_each_direction"],
                        control["max_frame_bytes"],
                    )
                    if any(
                        isinstance(item, bool)
                        or not isinstance(item, int)
                        or item <= 0
                        for item in integer_values
                    ):
                        raise ValueError(
                            "Relay returned invalid negotiated limits"
                        )
                    if control["max_frame_bytes"] > MAX_WIRE_BYTES:
                        raise ValueError(
                            "Relay frame limit exceeds Anet wire limit"
                        )
                    return AhubRelayConnection(
                        websocket=websocket,
                        peer_node_id=str(control["peer_node_id"]),
                        max_duration_ms=control["max_duration_ms"],
                        max_bytes_each_direction=control[
                            "max_bytes_each_direction"
                        ],
                        max_frame_bytes=control["max_frame_bytes"],
                    )
        except Exception as exc:
            if websocket is not None:
                await websocket.close()
            if isinstance(exc, (ValueError, RuntimeError)):
                raise
            response = getattr(exc, "response", None)
            status = int(getattr(response, "status_code", 0) or 0)
            category = (
                "auth"
                if status in {401, 403}
                else ("not_found" if status == 404 else "transport")
            )
            raise AhubHTTPError(category, status=status) from exc

    def submit(self, raw: bytes) -> CustodyReceipt:
        value = self._request(
            "POST",
            "/v1/mailbox",
            bytes(raw),
            content_type="application/octet-stream",
        )
        if value.get("meaning") != "ahub_custody_only":
            raise ValueError("Ahub returned an ambiguous custody receipt")
        return CustodyReceipt(
            packet_id=str(value["packet_id"]),
            destination_id=str(value["destination_id"]),
            stored=bool(value["stored"]),
            expires_ms=int(value["expires_ms"]),
        )

    def claim(
        self,
        *,
        limit: int = MAX_CLAIM_BATCH,
        lease_ms: int = MAX_CLAIM_LEASE_MS,
        uploader_id: str | None = None,
    ) -> tuple[ClaimedPacket, ...]:
        request_body: dict[str, Any] = {
            "lease_ms": lease_ms,
            "limit": limit,
        }
        if uploader_id is not None:
            request_body["uploader_id"] = uploader_id
        body = ahub_json(request_body)
        value = self._request(
            "POST", "/v1/mailbox/claims", body
        )
        packets = value.get("packets")
        if not isinstance(packets, list):
            raise ValueError("Ahub returned invalid mailbox claims")
        claimed: list[ClaimedPacket] = []
        for item in packets:
            if not isinstance(item, dict):
                raise ValueError("Ahub returned invalid mailbox claim")
            claimed.append(
                ClaimedPacket(
                    packet_id=str(item["packet_id"]),
                    raw=b64d(str(item["raw"])),
                    depth=int(item["depth"]),
                    claim_token=str(item["claim_token"]),
                    claim_until_ms=int(item["claim_until_ms"]),
                    uploader_id=str(item["uploader_id"]),
                    expires_ms=int(item["expires_ms"]),
                )
            )
        return tuple(claimed)

    def settle(
        self,
        packet_id: str,
        claim_token: str,
        proof: DestinationSettlement,
    ) -> bool:
        path = f"/v1/mailbox/{packet_id}/settle"
        body = ahub_json(
            {"claim_token": claim_token, "proof": proof.to_dict()}
        )
        value = self._request("POST", path, body)
        return bool(value["settled"])

    def settle_claim(self, claim: ClaimedPacket) -> bool:
        proof = issue_destination_settlement(
            self.identity,
            packet_id=claim.packet_id,
            raw=claim.raw,
            uploader_id=claim.uploader_id,
            expires_ms=claim.expires_ms,
        )
        return self.settle(claim.packet_id, claim.claim_token, proof)

    def settlements(
        self,
        *,
        limit: int = MAX_CLAIM_BATCH,
        destination_id: str | None = None,
    ) -> tuple[DestinationSettlement, ...]:
        request_body: dict[str, Any] = {"limit": limit}
        if destination_id is not None:
            request_body["destination_id"] = destination_id
        body = ahub_json(request_body)
        value = self._request(
            "POST", "/v1/mailbox/settlements", body
        )
        if value.get("meaning") != "destination_settlement_only":
            raise ValueError("Ahub returned ambiguous settlement state")
        items = value.get("settlements")
        if not isinstance(items, list):
            raise ValueError("Ahub returned invalid settlements")
        if any(not isinstance(item, dict) for item in items):
            raise ValueError("Ahub returned invalid settlement")
        return tuple(
            DestinationSettlement.from_dict(item)
            for item in items
        )

    def acknowledge_settlement(self, packet_id: str) -> bool:
        path = f"/v1/mailbox/settlements/{packet_id}/ack"
        value = self._request("POST", path, b"")
        return bool(value["acknowledged"])
