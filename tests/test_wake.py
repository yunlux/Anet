from __future__ import annotations

import json
from pathlib import Path

import pytest

from anet.config import initialize_node
from anet.identity import Identity
from anet.packet import open_packet, seal_packet
from anet.store import PacketStore
from anet.wake import WakeBridge, validate_loopback_endpoint


class _Response:
    status = 202

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        pass


def _add_message(store: PacketStore, sender: Identity, recipient: Identity, body: str) -> None:
    raw = seal_packet(sender, recipient.card(), kind="message", body=body)
    message = open_packet(recipient, raw)
    assert store.add_inbox(message, trusted=True)


def test_wake_bridge_edges_growth_and_rearms(tmp_path: Path) -> None:
    home = tmp_path / "node"
    config = initialize_node(home, label="recipient")
    recipient = Identity.load(config.identity_path)
    sender = Identity.generate("sender")
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return _Response()

    store = PacketStore(config.database_path)
    try:
        store.open_consumer_group("runtime-a.inbox", start="latest")
        bridge = WakeBridge(
            home=home,
            group="runtime-a.inbox",
            endpoint="http://127.0.0.1:12345/wake",
            token="a" * 64,
            rearm_seconds=30,
            opener=opener,
        )
        assert bridge.step(store, monotonic=1)["attempted"] is False

        _add_message(store, sender, recipient, "one")
        assert bridge.step(store, monotonic=2) == {
            "available": 1,
            "attempted": True,
            "accepted": True,
        }
        assert bridge.step(store, monotonic=3)["attempted"] is False

        _add_message(store, sender, recipient, "two")
        assert bridge.step(store, monotonic=4)["accepted"] is True
        assert bridge.step(store, monotonic=35)["accepted"] is True

        request, timeout = calls[0]
        assert request.headers["X-anet-bridge-token"] == "a" * 64
        payload = json.loads(request.data)
        assert payload["schema"] == "anet-wake.v1"
        assert payload["consumerGroup"] == "runtime-a.inbox"
        assert "body" not in payload and "message" not in payload
        assert timeout == 5.0
    finally:
        store.close()


def test_wake_endpoint_is_loopback_only() -> None:
    assert validate_loopback_endpoint("http://localhost:1234/wake")
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_endpoint("https://127.0.0.1:1234/wake")
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_endpoint("http://example.com:1234/wake")
    with pytest.raises(ValueError, match="port"):
        validate_loopback_endpoint("http://127.0.0.1/wake")
