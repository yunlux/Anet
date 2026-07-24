from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import NodeConfig
from .store import PacketStore


WAKE_TOKEN_HEADER = "x-anet-bridge-token"


def validate_loopback_endpoint(value: str) -> str:
    endpoint = str(value).strip()
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("wake endpoint must be loopback HTTP")
    if not parsed.port:
        raise ValueError("wake endpoint must include a port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("wake endpoint must not include credentials, query, or fragment")
    return endpoint


@dataclass
class WakeBridge:
    """Convert durable Inbox availability into content-free runtime wake hints.

    Message bodies never leave the Anet store.  The local runtime receives only
    an edge hint and claims authenticated messages through its own narrowly
    scoped Anet capability.
    """

    home: Path
    group: str
    endpoint: str
    token: str
    poll_seconds: float = 0.25
    rearm_seconds: float = 30.0
    startup_delay_seconds: float = 5.0
    start: str = "latest"
    timeout_seconds: float = 5.0
    opener: Callable[..., object] = urllib.request.urlopen

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser().resolve()
        self.endpoint = validate_loopback_endpoint(self.endpoint)
        self.group = str(self.group).strip()
        self.token = str(self.token)
        if not self.group:
            raise ValueError("wake consumer group is required")
        if len(self.token) < 32:
            raise ValueError("wake bridge token is missing or too short")
        self.poll_seconds = max(0.05, min(float(self.poll_seconds), 60.0))
        self.rearm_seconds = max(1.0, min(float(self.rearm_seconds), 3600.0))
        self.startup_delay_seconds = max(
            0.0, min(float(self.startup_delay_seconds), 60.0)
        )
        self.timeout_seconds = max(0.5, min(float(self.timeout_seconds), 30.0))
        self._last_available = 0
        self._last_notified_available = 0
        self._last_notified_at = 0.0

    def _notify(self, available: int) -> bool:
        payload = json.dumps(
            {
                "schema": "anet-wake.v1",
                "eventId": f"anet-wake-{uuid.uuid4()}",
                "consumerGroup": self.group,
                "available": int(available),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                WAKE_TOKEN_HEADER: self.token,
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            try:
                status = int(getattr(response, "status", response.getcode()))
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
            return status == 202
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            return False

    def step(self, store: PacketStore, *, monotonic: float | None = None) -> dict[str, object]:
        now = time.monotonic() if monotonic is None else float(monotonic)
        available = int(store.consumer_group_status(self.group)["available"])
        edge = self._last_available == 0 and available > 0
        growth = available > self._last_notified_available
        rearm = available > 0 and now - self._last_notified_at >= self.rearm_seconds
        attempted = bool(edge or growth or rearm)
        accepted = False
        if attempted:
            accepted = self._notify(available)
            if accepted:
                self._last_notified_available = available
                self._last_notified_at = now
        if available == 0:
            self._last_notified_available = 0
        self._last_available = available
        return {"available": available, "attempted": attempted, "accepted": accepted}

    def run_forever(self) -> None:
        config = NodeConfig.load(self.home)
        store = PacketStore(config.database_path)
        try:
            try:
                store.consumer_group_status(self.group)
            except KeyError:
                store.open_consumer_group(self.group, start=self.start)
            if self.startup_delay_seconds:
                time.sleep(self.startup_delay_seconds)
            failure_delay = self.poll_seconds
            while True:
                result = self.step(store)
                if result["attempted"] and not result["accepted"]:
                    failure_delay = min(max(failure_delay * 2, 0.5), 30.0)
                else:
                    failure_delay = self.poll_seconds
                time.sleep(failure_delay)
        finally:
            store.close()
