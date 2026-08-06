from __future__ import annotations

import asyncio

import pytest

from anet.ahub_transport import (
    bridge_ahub_relay_to_tcp,
    open_ahub_relay_tls_connection,
)


class _ClosedRelayClient:
    """Fake relay client that fails to open, isolating pre-validation."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def open_relay(self, reservation_id: str):
        raise self._error


@pytest.mark.asyncio
async def test_relay_bridge_rejects_invalid_target_port() -> None:
    with pytest.raises(ValueError, match="target port is invalid"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(RuntimeError("unreachable")),
            "res-1",
            target_host="127.0.0.1",
            target_port=0,
        )
    with pytest.raises(ValueError, match="target port is invalid"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(RuntimeError("unreachable")),
            "res-1",
            target_host="127.0.0.1",
            target_port=70000,
        )


@pytest.mark.asyncio
async def test_relay_bridge_rejects_non_loopback_target_by_default() -> None:
    with pytest.raises(ValueError, match="must be loopback"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(RuntimeError("unreachable")),
            "res-1",
            target_host="192.168.1.20",
            target_port=43101,
        )
    with pytest.raises(ValueError, match="must be loopback"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(RuntimeError("unreachable")),
            "res-1",
            target_host="anet.example",
            target_port=43101,
        )


@pytest.mark.asyncio
async def test_relay_bridge_allows_loopback_target() -> None:
    error = ConnectionError("relay not reachable")
    with pytest.raises(ConnectionError, match="relay not reachable"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(error),
            "res-1",
            target_host="127.0.0.1",
            target_port=43101,
        )


@pytest.mark.asyncio
async def test_relay_bridge_allows_localhost_alias() -> None:
    error = ConnectionError("relay not reachable")
    with pytest.raises(ConnectionError, match="relay not reachable"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(error),
            "res-1",
            target_host="localhost",
            target_port=43101,
        )


@pytest.mark.asyncio
async def test_relay_bridge_allows_non_loopback_when_explicitly_authorized() -> None:
    error = ConnectionError("relay not reachable")
    with pytest.raises(ConnectionError, match="relay not reachable"):
        await bridge_ahub_relay_to_tcp(
            _ClosedRelayClient(error),
            "res-1",
            target_host="10.0.0.5",
            target_port=43101,
            allow_non_loopback_target=True,
        )


@pytest.mark.asyncio
async def test_relay_bridge_checks_validation_before_opening_relay() -> None:
    opened = asyncio.Event()

    class _ProbingClient:
        async def open_relay(self, reservation_id: str):
            opened.set()
            raise ConnectionError("should not be reached")

    with pytest.raises(ValueError, match="must be loopback"):
        await bridge_ahub_relay_to_tcp(
            _ProbingClient(),
            "res-1",
            target_host="8.8.8.8",
            target_port=43101,
        )
    assert not opened.is_set()


@pytest.mark.asyncio
async def test_relay_tls_connection_propagates_relay_open_failure(
    monkeypatch,
) -> None:
    import ssl as ssl_module

    class _FailingClient:
        async def open_relay(self, reservation_id: str):
            raise ConnectionError("reservation gone")

    context = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_CLIENT)
    with pytest.raises(ConnectionError, match="reservation gone"):
        await open_ahub_relay_tls_connection(
            _FailingClient(),
            "res-1",
            context,
        )
