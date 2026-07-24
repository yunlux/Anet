from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .ahub import AhubLimits, RelayReservation


LOGGER = logging.getLogger(__name__)
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class RelayProtocolError(ValueError):
    pass


class RelayLimitError(OverflowError):
    pass


@dataclass
class _RelayEndpoint:
    node_id: str
    receive: ASGIReceive
    send: ASGISend
    paired: asyncio.Event
    done: asyncio.Future[None]


class RelayCoordinator:
    """In-memory pairing for durable, explicitly peer-scoped reservations."""

    def __init__(self, limits: AhubLimits) -> None:
        self.limits = limits
        self._lock = asyncio.Lock()
        self._waiting: dict[str, _RelayEndpoint] = {}
        self._active_reservations: set[str] = set()
        self._active_by_node: dict[str, int] = {}

    async def handle(
        self,
        reservation: RelayReservation,
        node_id: str,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if node_id == reservation.owner_id:
            await self._wait_as_owner(reservation, receive, send)
            return
        if node_id == reservation.allowed_peer_id:
            await self._join_as_peer(reservation, receive, send)
            return
        await self._close(send, 4403, "forbidden")

    async def _wait_as_owner(
        self,
        reservation: RelayReservation,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        loop = asyncio.get_running_loop()
        endpoint = _RelayEndpoint(
            node_id=reservation.owner_id,
            receive=receive,
            send=send,
            paired=asyncio.Event(),
            done=loop.create_future(),
        )
        async with self._lock:
            if (
                reservation.reservation_id in self._waiting
                or reservation.reservation_id in self._active_reservations
            ):
                await self._close(send, 4429, "reservation_busy")
                return
            if not self._node_has_capacity(reservation.owner_id):
                await self._close(send, 4429, "node_busy")
                return
            self._waiting[reservation.reservation_id] = endpoint
        await self._send_control(
            send,
            {
                "type": "relay.waiting",
                "expires_ms": reservation.expires_ms,
            },
        )
        paired_task = asyncio.create_task(endpoint.paired.wait())
        receive_task = asyncio.create_task(receive())
        try:
            remaining_seconds = max(
                0.0,
                (reservation.expires_ms - int(time.time() * 1000)) / 1000,
            )
            done, _pending = await asyncio.wait(
                {paired_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining_seconds,
            )
            if not done:
                await self._close(send, 4008, "reservation_expired")
                return
            if receive_task in done:
                event = receive_task.result()
                if event.get("type") != "websocket.disconnect":
                    await self._close(send, 1003, "data_before_ready")
                return
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receive_task
            await endpoint.done
        finally:
            paired_task.cancel()
            receive_task.cancel()
            async with self._lock:
                if self._waiting.get(reservation.reservation_id) is endpoint:
                    del self._waiting[reservation.reservation_id]

    async def _join_as_peer(
        self,
        reservation: RelayReservation,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        async with self._lock:
            owner = self._waiting.get(reservation.reservation_id)
            if owner is None:
                await self._close(send, 4404, "owner_not_waiting")
                return
            if reservation.reservation_id in self._active_reservations:
                await self._close(send, 4429, "reservation_busy")
                return
            if not self._node_has_capacity(reservation.allowed_peer_id):
                await self._close(send, 4429, "node_busy")
                return
            del self._waiting[reservation.reservation_id]
            self._active_reservations.add(reservation.reservation_id)
            self._increment_node(reservation.owner_id)
            self._increment_node(reservation.allowed_peer_id)
            owner.paired.set()

        counters = {"owner_to_peer": 0, "peer_to_owner": 0}
        started = time.monotonic()
        close_code = 1000
        close_reason = "relay_closed"
        try:
            ready_common = {
                "type": "relay.ready",
                "max_duration_ms": reservation.max_duration_ms,
                "max_bytes_each_direction": (
                    reservation.max_bytes_each_direction
                ),
                "max_frame_bytes": self.limits.max_relay_frame_bytes,
            }
            await self._send_control(
                owner.send,
                {**ready_common, "peer_node_id": reservation.allowed_peer_id},
            )
            await self._send_control(
                send,
                {**ready_common, "peer_node_id": reservation.owner_id},
            )
            try:
                remaining_seconds = max(
                    0.0,
                    (
                        reservation.expires_ms - int(time.time() * 1000)
                    )
                    / 1000,
                )
                session_seconds = min(
                    reservation.max_duration_ms / 1000,
                    remaining_seconds,
                )
                if session_seconds <= 0:
                    raise TimeoutError
                await asyncio.wait_for(
                    self._relay_bidirectional(
                        owner,
                        _RelayEndpoint(
                            node_id=reservation.allowed_peer_id,
                            receive=receive,
                            send=send,
                            paired=asyncio.Event(),
                            done=asyncio.get_running_loop().create_future(),
                        ),
                        reservation,
                        counters,
                    ),
                    timeout=session_seconds,
                )
            except TimeoutError:
                close_code = 4008
                close_reason = "duration_limit"
            except RelayLimitError:
                close_code = 1009
                close_reason = "byte_limit"
            except RelayProtocolError:
                close_code = 1003
                close_reason = "binary_required"
        except Exception:
            close_code = 1011
            close_reason = "relay_error"
        finally:
            await asyncio.gather(
                self._close(owner.send, close_code, close_reason),
                self._close(send, close_code, close_reason),
            )
            async with self._lock:
                self._active_reservations.discard(
                    reservation.reservation_id
                )
                self._decrement_node(reservation.owner_id)
                self._decrement_node(reservation.allowed_peer_id)
            if not owner.done.done():
                owner.done.set_result(None)
            LOGGER.info(
                "ahub_relay status=%s owner_to_peer_bytes=%d "
                "peer_to_owner_bytes=%d elapsed_ms=%.1f",
                close_reason,
                counters["owner_to_peer"],
                counters["peer_to_owner"],
                (time.monotonic() - started) * 1000,
            )

    async def _relay_bidirectional(
        self,
        owner: _RelayEndpoint,
        peer: _RelayEndpoint,
        reservation: RelayReservation,
        counters: dict[str, int],
    ) -> None:
        tasks = {
            asyncio.create_task(
                self._pump(
                    owner,
                    peer,
                    reservation,
                    counters,
                    "owner_to_peer",
                )
            ),
            asyncio.create_task(
                self._pump(
                    peer,
                    owner,
                    reservation,
                    counters,
                    "peer_to_owner",
                )
            ),
        }
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()

    async def _pump(
        self,
        source: _RelayEndpoint,
        destination: _RelayEndpoint,
        reservation: RelayReservation,
        counters: dict[str, int],
        counter_name: str,
    ) -> None:
        while True:
            event = await source.receive()
            event_type = event.get("type")
            if event_type == "websocket.disconnect":
                return
            if event_type != "websocket.receive":
                continue
            raw = event.get("bytes")
            if raw is None or event.get("text") is not None:
                raise RelayProtocolError("Relay accepts binary frames only")
            frame = bytes(raw)
            if len(frame) > self.limits.max_relay_frame_bytes:
                raise RelayLimitError("Relay frame exceeds configured limit")
            counters[counter_name] += len(frame)
            if (
                counters[counter_name]
                > reservation.max_bytes_each_direction
            ):
                raise RelayLimitError("Relay direction byte limit exceeded")
            await destination.send(
                {"type": "websocket.send", "bytes": frame}
            )

    def _node_has_capacity(self, node_id: str) -> bool:
        return (
            self._active_by_node.get(node_id, 0)
            < self.limits.max_relay_connections_per_node
        )

    def _increment_node(self, node_id: str) -> None:
        self._active_by_node[node_id] = (
            self._active_by_node.get(node_id, 0) + 1
        )

    def _decrement_node(self, node_id: str) -> None:
        remaining = self._active_by_node.get(node_id, 0) - 1
        if remaining > 0:
            self._active_by_node[node_id] = remaining
        else:
            self._active_by_node.pop(node_id, None)

    @staticmethod
    async def _send_control(send: ASGISend, value: dict[str, Any]) -> None:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        await send({"type": "websocket.send", "text": raw})

    @staticmethod
    async def _close(send: ASGISend, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await send(
                {
                    "type": "websocket.close",
                    "code": int(code),
                    "reason": str(reason)[:123],
                }
            )
