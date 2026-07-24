from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import socket
import ssl
from typing import Any

from .ahub_http import AhubHTTPClient, AhubRelayConnection


class _RelayBridgeSession:
    def __init__(
        self,
        relay: AhubRelayConnection,
        bridge_writer: asyncio.StreamWriter,
        tasks: tuple[asyncio.Task[None], ...],
    ) -> None:
        self.relay = relay
        self.bridge_writer = bridge_writer
        self.tasks = tasks
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.bridge_writer.close()
            with suppress(Exception):
                await self.bridge_writer.wait_closed()
            with suppress(Exception):
                await self.relay.close()
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)


class RelayTLSWriter:
    """StreamWriter facade owning a Relay WebSocket and socket bridge."""

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        session: _RelayBridgeSession,
    ) -> None:
        self._writer = writer
        self._session = session

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        with suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
        await self._session.close()

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._writer.get_extra_info(name, default)


async def _pump_stream_to_relay(
    reader: asyncio.StreamReader,
    relay: AhubRelayConnection,
) -> None:
    try:
        while True:
            chunk = await reader.read(relay.max_frame_bytes)
            if not chunk:
                break
            await relay.send(chunk)
    except Exception:
        pass
    finally:
        with suppress(Exception):
            await relay.close()


async def _pump_relay_to_stream(
    relay: AhubRelayConnection,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            writer.write(await relay.receive())
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def open_ahub_relay_tls_connection(
    client: AhubHTTPClient,
    reservation_id: str,
    tls_context: ssl.SSLContext,
    *,
    timeout: float = 10.0,
) -> tuple[asyncio.StreamReader, RelayTLSWriter]:
    """Carry existing Anet client-side TLS over an authenticated Relay stream."""

    relay = await client.open_relay(reservation_id)
    client_socket, bridge_socket = socket.socketpair()
    client_socket.setblocking(False)
    bridge_socket.setblocking(False)
    bridge_writer: asyncio.StreamWriter | None = None
    session: _RelayBridgeSession | None = None
    try:
        bridge_reader, bridge_writer = await asyncio.open_connection(
            sock=bridge_socket
        )
        tasks = (
            asyncio.create_task(
                _pump_stream_to_relay(bridge_reader, relay),
                name="anet-relay-tls-out",
            ),
            asyncio.create_task(
                _pump_relay_to_stream(relay, bridge_writer),
                name="anet-relay-tls-in",
            ),
        )
        session = _RelayBridgeSession(relay, bridge_writer, tasks)
        try:
            reader, tls_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    sock=client_socket,
                    ssl=tls_context,
                    server_hostname="",
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
            raise ConnectionError(
                "Relay did not provide an Anet TLS byte stream"
            ) from exc
        return reader, RelayTLSWriter(tls_writer, session)
    except asyncio.CancelledError:
        if session is not None:
            await session.close()
        else:
            client_socket.close()
            bridge_socket.close()
            with suppress(Exception):
                await relay.close()
        raise
    except Exception:
        if session is not None:
            await session.close()
        else:
            client_socket.close()
            bridge_socket.close()
            if bridge_writer is not None:
                bridge_writer.close()
            with suppress(Exception):
                await relay.close()
        raise


async def bridge_ahub_relay_to_tcp(
    client: AhubHTTPClient,
    reservation_id: str,
    *,
    target_host: str,
    target_port: int,
    allow_non_loopback_target: bool = False,
) -> None:
    """Bridge the reservation owner to its existing local Anet TLS listener."""

    host = str(target_host).strip()
    if not 1 <= int(target_port) <= 65535:
        raise ValueError("Relay bridge target port is invalid")
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback and not allow_non_loopback_target:
        raise ValueError(
            "Relay bridge target must be loopback unless explicitly allowed"
        )
    relay = await client.open_relay(reservation_id)
    writer: asyncio.StreamWriter | None = None
    tasks: set[asyncio.Task[None]] = set()
    try:
        reader, writer = await asyncio.open_connection(host, int(target_port))
        tasks = {
            asyncio.create_task(
                _pump_stream_to_relay(reader, relay),
                name="anet-relay-listener-out",
            ),
            asyncio.create_task(
                _pump_relay_to_stream(relay, writer),
                name="anet-relay-listener-in",
            ),
        }
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        with suppress(Exception):
            await relay.close()
