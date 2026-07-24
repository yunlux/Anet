from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import os
import socket
import ssl
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import DirectProxyConfig, StdioDialerConfig
from .encoding import MAX_WIRE_BYTES, pack, unpack
from .locator import parse_locator


class StdioAdapterError(ConnectionError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def classify_connection_error(error: BaseException, *, proxied: bool) -> str:
    if isinstance(error, StdioAdapterError):
        return error.category
    message = str(error).lower()
    if isinstance(error, ssl.SSLError) or "ssl" in message or "tls" in message:
        return "tls_handshake"
    if "credential environment variable" in message:
        return "proxy_config"
    if "authentication" in message:
        return "proxy_auth"
    if "connect failed with reply" in message:
        return "proxy_target_rejected"
    if "target resolution" in message or "getaddrinfo" in message:
        return "target_dns"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "proxy_timeout" if proxied else "tcp_timeout"
    if isinstance(error, ConnectionRefusedError):
        return "proxy_unreachable" if proxied else "tcp_refused"
    if isinstance(error, OSError):
        return "proxy_unreachable" if proxied else "tcp_unreachable"
    return "proxy_protocol" if proxied else "tcp_connect"


_STDIO_BASE_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)


def _stdio_environment(
    config: StdioDialerConfig,
    *,
    host: str,
    port: int,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _STDIO_BASE_ENV
        if name in os.environ
    }
    for name in config.env:
        value = os.environ.get(name)
        if value is None:
            raise StdioAdapterError(
                "adapter_config",
                f"stdio adapter environment variable is missing: {name}",
            )
        environment[name] = value
    environment["ANET_TARGET_HOST"] = host
    environment["ANET_TARGET_PORT"] = str(port)
    return environment


async def _pump_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


class _StdioAdapterSession:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        bridge_writer: asyncio.StreamWriter,
        tasks: tuple[asyncio.Task[None], ...],
    ) -> None:
        self.process = process
        self.bridge_writer = bridge_writer
        self.tasks = tasks
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def close(self, *, grace: float = 0.5) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self.process.returncode is None and grace > 0:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.process.wait(), timeout=grace)
            self.bridge_writer.close()
            with suppress(Exception):
                await self.bridge_writer.wait_closed()
            if self.process.stdin is not None:
                self.process.stdin.close()
                with suppress(Exception):
                    await self.process.stdin.wait_closed()
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    with suppress(Exception):
                        await asyncio.wait_for(self.process.wait(), timeout=1.0)


class StdioTLSWriter:
    """StreamWriter facade that also owns the external adapter process."""

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        session: _StdioAdapterSession,
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


async def open_stdio_tls_connection(
    config: StdioDialerConfig,
    host: str,
    port: int,
    tls_context: ssl.SSLContext,
) -> tuple[asyncio.StreamReader, StdioTLSWriter]:
    """Run an adapter as argv and carry Anet TLS ciphertext over its stdio."""

    environment = _stdio_environment(config, host=host, port=port)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                str(config.executable),
                *config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                creationflags=creationflags,
            ),
            timeout=config.startup_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise StdioAdapterError(
            "adapter_timeout", "stdio adapter process startup timed out"
        ) from exc
    except (OSError, ValueError) as exc:
        raise StdioAdapterError(
            "adapter_spawn", f"stdio adapter could not be started: {exc}"
        ) from exc
    if process.stdin is None or process.stdout is None:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.wait()
        raise StdioAdapterError(
            "adapter_spawn", "stdio adapter pipes could not be created"
        )

    client_socket, bridge_socket = socket.socketpair()
    client_socket.setblocking(False)
    bridge_socket.setblocking(False)
    bridge_writer: asyncio.StreamWriter | None = None
    session: _StdioAdapterSession | None = None
    try:
        bridge_reader, bridge_writer = await asyncio.open_connection(
            sock=bridge_socket
        )
        tasks = (
            asyncio.create_task(_pump_stream(bridge_reader, process.stdin)),
            asyncio.create_task(_pump_stream(process.stdout, bridge_writer)),
        )
        session = _StdioAdapterSession(process, bridge_writer, tasks)
        try:
            reader, tls_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    sock=client_socket,
                    ssl=tls_context,
                    server_hostname="",
                ),
                timeout=config.startup_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise StdioAdapterError(
                "adapter_timeout", "stdio adapter TLS tunnel timed out"
            ) from exc
        except (ssl.SSLError, ConnectionError, OSError) as exc:
            await asyncio.sleep(0)
            if process.returncode is None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=0.05)
            if process.returncode is not None:
                raise StdioAdapterError(
                    "adapter_exit",
                    f"stdio adapter exited with code {process.returncode}",
                ) from exc
            raise StdioAdapterError(
                "adapter_protocol", "stdio adapter did not provide a TLS byte stream"
            ) from exc
        return reader, StdioTLSWriter(tls_writer, session)
    except asyncio.CancelledError:
        if session is not None:
            await session.close(grace=0)
        else:
            client_socket.close()
            if bridge_writer is not None:
                bridge_writer.close()
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
        raise
    except Exception:
        if session is not None:
            await session.close(grace=0)
        else:
            client_socket.close()
            if bridge_writer is not None:
                bridge_writer.close()
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
        raise


def parse_tls_address(address: str) -> tuple[str, int]:
    locator = parse_locator(address)
    return locator.host, locator.port


def server_tls_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(cert_path), str(key_path))
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    return context


def client_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    return context


async def _read_exact(reader: asyncio.StreamReader, size: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(size), timeout=5.0)


async def _socks_targets(host: str, port: int, *, remote_dns: bool) -> tuple[bytes, ...]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if remote_dns:
            encoded = host.encode("idna")
            if not encoded or len(encoded) > 255:
                raise ValueError("SOCKS5 target domain is invalid")
            return (
                b"\x03" + bytes([len(encoded)]) + encoded + port.to_bytes(2, "big"),
            )
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            ),
            timeout=5.0,
        )
        if not infos:
            raise OSError("SOCKS5 target resolution returned no addresses")
        targets: list[bytes] = []
        seen: set[tuple[int, bytes]] = set()
        for info in infos:
            candidate = ipaddress.ip_address(info[4][0])
            atyp = b"\x01" if candidate.version == 4 else b"\x04"
            key = (candidate.version, candidate.packed)
            if key in seen:
                continue
            seen.add(key)
            targets.append(atyp + candidate.packed + port.to_bytes(2, "big"))
        if not targets:
            raise OSError("SOCKS5 target resolution returned no usable addresses")
        return tuple(targets)
    atyp = b"\x01" if address.version == 4 else b"\x04"
    return (atyp + address.packed + port.to_bytes(2, "big"),)


async def open_proxy_tls_connection(
    proxy: DirectProxyConfig,
    host: str,
    port: int,
    tls_context: ssl.SSLContext,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    parsed = urlparse(proxy.url)
    authenticated = bool(proxy.username_env)
    username_raw = b""
    password_raw = b""
    if authenticated:
        username = os.environ.get(proxy.username_env)
        password = os.environ.get(proxy.password_env)
        if username is None or password is None:
            raise ConnectionError("SOCKS5 credential environment variable is missing")
        username_raw = username.encode("utf-8")
        password_raw = password.encode("utf-8")
        if not 1 <= len(username_raw) <= 255 or not 1 <= len(password_raw) <= 255:
            raise ConnectionError("SOCKS5 credentials must encode to 1-255 bytes")
    targets = await _socks_targets(
        host, port, remote_dns=parsed.scheme == "socks5h"
    )
    last_error: Exception | None = None
    for target in targets:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port), timeout=5.0
        )
        try:
            methods = b"\x00\x02" if authenticated else b"\x00"
            writer.write(b"\x05" + bytes([len(methods)]) + methods)
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            response = await _read_exact(reader, 2)
            expected_method = 2 if authenticated else 0
            if response != bytes([5, expected_method]):
                raise ConnectionError(
                    "SOCKS5 proxy rejected the required authentication method"
                )
            if authenticated:
                writer.write(
                    b"\x01"
                    + bytes([len(username_raw)])
                    + username_raw
                    + bytes([len(password_raw)])
                    + password_raw
                )
                await asyncio.wait_for(writer.drain(), timeout=5.0)
                if await _read_exact(reader, 2) != b"\x01\x00":
                    raise ConnectionError(
                        "SOCKS5 username/password authentication failed"
                    )
            writer.write(b"\x05\x01\x00" + target)
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            reply = await _read_exact(reader, 4)
            if reply[0] != 5 or reply[1] != 0 or reply[2] != 0:
                raise ConnectionError(f"SOCKS5 CONNECT failed with reply {reply[1]}")
            if reply[3] == 1:
                await _read_exact(reader, 4)
            elif reply[3] == 4:
                await _read_exact(reader, 16)
            elif reply[3] == 3:
                length = (await _read_exact(reader, 1))[0]
                if length == 0:
                    raise ConnectionError("SOCKS5 reply contains an empty domain")
                await _read_exact(reader, length)
            else:
                raise ConnectionError("SOCKS5 reply contains an invalid address type")
            await _read_exact(reader, 2)
            await asyncio.wait_for(
                writer.start_tls(tls_context, server_hostname=None), timeout=5.0
            )
            return reader, writer
        except Exception as exc:
            last_error = exc
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
    if last_error is None:
        raise ConnectionError("SOCKS5 target resolution returned no usable addresses")
    raise last_error


async def write_frame(writer: asyncio.StreamWriter, value: Any) -> None:
    raw = pack(value)
    if not raw or len(raw) > MAX_WIRE_BYTES:
        raise ValueError("frame exceeds transport limit")
    writer.write(len(raw).to_bytes(4, "big") + raw)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader, *, timeout: float = 15.0) -> Any:
    prefix = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    size = int.from_bytes(prefix, "big")
    if size <= 0 or size > MAX_WIRE_BYTES:
        raise ValueError("invalid frame length")
    raw = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    return unpack(raw)
