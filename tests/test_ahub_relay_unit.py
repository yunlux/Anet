from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from anet.ahub import AhubLimits, RelayReservation
from anet.ahub_relay import RelayCoordinator


def reservation(
    *,
    rid: str = "a" * 32,
    owner: str = "an1" + "o" * 32,
    peer: str = "an1" + "p" * 32,
    created_ms: int | None = None,
    expires_ms: int | None = None,
    duration_ms: int = 30_000,
) -> RelayReservation:
    now = int(time.time() * 1000)
    return RelayReservation(
        reservation_id=rid,
        owner_id=owner,
        allowed_peer_id=peer,
        created_ms=created_ms or now,
        expires_ms=expires_ms or (now + 60_000),
        max_duration_ms=duration_ms,
        max_bytes_each_direction=4096,
    )


class FakeASGI:
    """Captures sent control frames and queues inbound websocket events."""

    def __init__(self, idle_sleep: float = 5.0) -> None:
        self.sent: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._idle_sleep = idle_sleep

    async def receive(self) -> dict[str, Any]:
        if self._events:
            return self._events.pop(0)
        await asyncio.sleep(self._idle_sleep)

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)

    def queue(self, event: dict[str, Any]) -> None:
        self._events.append(event)


@pytest.mark.asyncio
async def test_relay_forbidden_for_unlisted_node() -> None:
    coordinator = RelayCoordinator(AhubLimits())
    outsider = FakeASGI()
    await coordinator.handle(
        reservation(),
        node_id="an1" + "x" * 32,
        receive=outsider.receive,
        send=outsider.send,
    )
    assert outsider.sent[-1]["type"] == "websocket.close"
    assert outsider.sent[-1]["code"] == 4403


@pytest.mark.asyncio
async def test_relay_owner_busy_when_reservation_already_waiting() -> None:
    coordinator = RelayCoordinator(AhubLimits())
    first = FakeASGI()
    first.queue({"type": "websocket.disconnect"})
    owner_task = asyncio.create_task(
        coordinator.handle(
            reservation(),
            node_id="an1" + "o" * 32,
            receive=first.receive,
            send=first.send,
        )
    )
    await asyncio.sleep(0)
    second = FakeASGI()
    await coordinator.handle(
        reservation(),
        node_id="an1" + "o" * 32,
        receive=second.receive,
        send=second.send,
    )
    assert second.sent[-1]["type"] == "websocket.close"
    assert second.sent[-1]["code"] == 4429
    first.queue({"type": "websocket.disconnect"})
    await owner_task


@pytest.mark.asyncio
async def test_relay_rejects_peer_when_owner_not_waiting() -> None:
    coordinator = RelayCoordinator(AhubLimits())
    peer = FakeASGI()
    await coordinator.handle(
        reservation(),
        node_id="an1" + "p" * 32,
        receive=peer.receive,
        send=peer.send,
    )
    assert peer.sent[-1]["type"] == "websocket.close"
    assert peer.sent[-1]["code"] == 4404


@pytest.mark.asyncio
async def test_relay_node_capacity_blocks_second_active_session() -> None:
    limits = AhubLimits(max_relay_connections_per_node=1)
    coordinator = RelayCoordinator(limits)
    peer_node = "an1" + "p" * 32
    # First reservation pairs owner + peer; both nodes now consume one slot.
    owner1 = FakeASGI(idle_sleep=60.0)
    peer1 = FakeASGI(idle_sleep=60.0)
    first = reservation(rid="b" * 32)
    owner1_task = asyncio.create_task(
        coordinator.handle(
            first,
            node_id="an1" + "o" * 32,
            receive=owner1.receive,
            send=owner1.send,
        )
    )
    await asyncio.sleep(0)
    peer1_task = asyncio.create_task(
        coordinator.handle(
            first,
            node_id=peer_node,
            receive=peer1.receive,
            send=peer1.send,
        )
    )
    # Wait until the first session is actually paired (relay.ready flows to
    # the peer) before attempting the second join.
    for _ in range(100):
        if any(
            e.get("type") == "websocket.send"
            and "relay.ready" in str(e.get("text", ""))
            for e in peer1.sent
        ):
            break
        await asyncio.sleep(0.01)
    # A second reservation with a different owner but the same peer cannot
    # join: the peer node has already consumed its single connection slot.
    second = reservation(rid="c" * 32, owner="an1" + "q" * 32)
    owner2 = FakeASGI(idle_sleep=60.0)
    owner2_task = asyncio.create_task(
        coordinator.handle(
            second,
            node_id="an1" + "q" * 32,
            receive=owner2.receive,
            send=owner2.send,
        )
    )
    await asyncio.sleep(0)
    peer2 = FakeASGI(idle_sleep=60.0)
    await coordinator.handle(
        second,
        node_id=peer_node,
        receive=peer2.receive,
        send=peer2.send,
    )
    assert peer2.sent[-1]["type"] == "websocket.close"
    assert peer2.sent[-1]["code"] == 4429
    # Cancel all lingering sessions to tear down cleanly.
    for task in (owner1_task, peer1_task, owner2_task):
        task.cancel()
    await asyncio.gather(*(
        task for task in (owner1_task, peer1_task, owner2_task)
    ), return_exceptions=True)


@pytest.mark.asyncio
async def test_relay_pairing_sends_ready_to_both_sides() -> None:
    coordinator = RelayCoordinator(AhubLimits())
    owner = FakeASGI()
    owner.queue({"type": "websocket.disconnect"})
    owner_task = asyncio.create_task(
        coordinator.handle(
            reservation(),
            node_id="an1" + "o" * 32,
            receive=owner.receive,
            send=owner.send,
        )
    )
    await asyncio.sleep(0)
    peer = FakeASGI()
    peer.queue({"type": "websocket.disconnect"})
    peer_task = asyncio.create_task(
        coordinator.handle(
            reservation(),
            node_id="an1" + "p" * 32,
            receive=peer.receive,
            send=peer.send,
        )
    )
    await asyncio.sleep(0)
    peer.queue({"type": "websocket.disconnect"})
    await peer_task
    owner.queue({"type": "websocket.disconnect"})
    await owner_task

    def ready_controls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "websocket.send":
                continue
            text = event.get("text", "")
            if isinstance(text, str):
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if payload.get("type") == "relay.ready":
                    result.append(payload)
        return result

    ready_to_owner = ready_controls(owner.sent)
    ready_to_peer = ready_controls(peer.sent)
    assert ready_to_owner, owner.sent
    assert ready_to_peer, peer.sent
    assert ready_to_owner[0]["peer_node_id"] == "an1" + "p" * 32
    assert ready_to_peer[0]["peer_node_id"] == "an1" + "o" * 32


@pytest.mark.asyncio
async def test_relay_expired_reservation_closes_owner() -> None:
    coordinator = RelayCoordinator(AhubLimits())
    owner = FakeASGI()
    expired = reservation(
        created_ms=int(time.time() * 1000) - 60_000,
        expires_ms=int(time.time() * 1000) - 5000,
    )
    owner_task = asyncio.create_task(
        coordinator.handle(
            expired,
            node_id="an1" + "o" * 32,
            receive=owner.receive,
            send=owner.send,
        )
    )
    await asyncio.wait_for(owner_task, timeout=2)
    closes = [e for e in owner.sent if e.get("type") == "websocket.close"]
    assert closes, owner.sent
    assert closes[0]["code"] == 4008
