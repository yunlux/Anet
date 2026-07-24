from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
import ipaddress
from pathlib import Path

import pytest

from anet.config import DirectProxyConfig, NodeConfig, initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


class FakeSocks5:
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self.username = username
        self.password = password
        self.server: asyncio.AbstractServer | None = None
        self.targets: list[tuple[int, str, int]] = []

    async def __aenter__(self) -> "FakeSocks5":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *args) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        assert self.server is not None and self.server.sockets
        return f"socks5://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            assert await reader.readexactly(1) == b"\x05"
            methods = await reader.readexactly((await reader.readexactly(1))[0])
            required = 2 if self.username is not None else 0
            writer.write(bytes([5, required if required in methods else 255]))
            await writer.drain()
            if required not in methods:
                return
            if required == 2:
                assert await reader.readexactly(1) == b"\x01"
                user = await reader.readexactly((await reader.readexactly(1))[0])
                password = await reader.readexactly((await reader.readexactly(1))[0])
                ok = user.decode() == self.username and password.decode() == self.password
                writer.write(b"\x01" + (b"\x00" if ok else b"\x01"))
                await writer.drain()
                if not ok:
                    return
            header = await reader.readexactly(4)
            assert header[:3] == b"\x05\x01\x00"
            atyp = header[3]
            if atyp == 1:
                host = str(ipaddress.ip_address(await reader.readexactly(4)))
            elif atyp == 4:
                host = str(ipaddress.ip_address(await reader.readexactly(16)))
            elif atyp == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("idna")
            else:
                raise AssertionError("invalid target address type")
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.targets.append((atyp, host, port))
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()

            async def relay(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()

            await asyncio.gather(relay(reader, upstream_writer), relay(upstream_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if upstream_writer is not None:
                upstream_writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            if upstream_writer is not None:
                with suppress(Exception):
                    await upstream_writer.wait_closed()


def make_config(path: Path, label: str) -> NodeConfig:
    return initialize_node(path, label=label, listen_host="127.0.0.1", listen_port=0)


def trust(config: NodeConfig, card) -> None:
    identity = Identity.load(config.identity_path)
    PeerBook(config.peers_path, own_node_id=identity.node_id).add(card)


def test_real_message_and_receipt_through_no_auth_socks5(tmp_path) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "a")
        b_config = make_config(tmp_path / "b", "b")
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        await b.start()
        port = b._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
        b_card = b_identity.card(addresses=(f"tls://localhost:{port}",), capabilities=b_config.capabilities)
        trust(a_config, b_card)
        trust(b_config, a_card)
        try:
            async with FakeSocks5() as proxy:
                a.config = replace(
                    a.config,
                    direct_proxy=DirectProxyConfig.from_dict({"url": proxy.url}),
                )
                packet_id = a.queue(b.node_id, kind="intent", body={"via": "socks5"})
                deadline = asyncio.get_running_loop().time() + 10
                while a.store.receipt(packet_id) is None and asyncio.get_running_loop().time() < deadline:
                    await a.sync_once()
                    await asyncio.sleep(0.05)
                assert a.store.receipt(packet_id) is not None
                assert any(item[0] in {1, 4} for item in proxy.targets)
                assert next(item for item in b.store.list_inbox() if item["packet_id"] == packet_id)["body"] == {"via": "socks5"}
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_socks5h_username_password_auth(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        a_config = make_config(tmp_path / "a", "a")
        b_config = make_config(tmp_path / "b", "b")
        a_identity = Identity.load(a_config.identity_path)
        b_identity = Identity.load(b_config.identity_path)
        a = AnetNode(a_config)
        b = AnetNode(b_config)
        await b.start()
        port = b._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        trust(a_config, b_identity.card(addresses=(f"tls://localhost:{port}",), capabilities=b_config.capabilities))
        trust(b_config, a_identity.card(addresses=(), capabilities=a_config.capabilities))
        monkeypatch.setenv("ANET_PROXY_USER", "alice")
        monkeypatch.setenv("ANET_PROXY_PASS", "correct horse")
        try:
            async with FakeSocks5("alice", "correct horse") as proxy:
                url = proxy.url.replace("socks5://", "socks5h://")
                a.config = replace(a.config, direct_proxy=DirectProxyConfig.from_dict({
                    "url": url, "username_env": "ANET_PROXY_USER", "password_env": "ANET_PROXY_PASS"
                }))
                assert (await a.sync_once())["connected"] == 1
                assert proxy.targets[-1][:2] == (3, "localhost")
        finally:
            await a.stop()
            await b.stop()
            a.close()
            b.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1080", "socks5://user:pass@127.0.0.1:1080",
    "socks5://127.0.0.1:1080/path", "socks5://127.0.0.1:notaport",
    "socks5://proxy.example:1080",
])
def test_proxy_config_rejects_unsafe_urls(url) -> None:
    with pytest.raises(ValueError):
        DirectProxyConfig.from_dict({"url": url})


def test_proxy_config_requires_both_env_names() -> None:
    with pytest.raises(ValueError):
        DirectProxyConfig.from_dict({"url": "socks5://127.0.0.1:1080", "username_env": "USER"})


@pytest.mark.parametrize("allow_remote", ["false", "true", 1, None])
def test_proxy_config_rejects_non_boolean_allow_remote(allow_remote) -> None:
    with pytest.raises(ValueError, match="allow_remote must be a boolean"):
        DirectProxyConfig.from_dict({
            "url": "socks5://127.0.0.1:1080",
            "allow_remote": allow_remote,
        })


@pytest.mark.parametrize("allow_remote", [False, True])
def test_proxy_config_preserves_boolean_allow_remote(allow_remote) -> None:
    proxy = DirectProxyConfig.from_dict({
        "url": "socks5://127.0.0.1:1080",
        "allow_remote": allow_remote,
    })
    assert proxy.allow_remote is allow_remote


def test_remote_proxy_requires_and_persists_opt_in(tmp_path) -> None:
    proxy = DirectProxyConfig.from_dict(
        {"url": "socks5h://proxy.example:1080", "allow_remote": True}
    )
    config = replace(make_config(tmp_path / "node", "node"), direct_proxy=proxy)
    config.save()
    loaded = NodeConfig.load(config.home)
    assert loaded.direct_proxy == proxy
    assert loaded.to_dict()["direct_proxy"] == {
        "url": "socks5h://proxy.example:1080",
        "allow_remote": True,
        "username_env": "",
        "password_env": "",
    }


def test_missing_env_and_auth_failure_fail_closed(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        config = make_config(tmp_path / "a", "a")
        node = AnetNode(config)
        try:
            async with FakeSocks5("alice", "secret") as proxy:
                cfg = DirectProxyConfig.from_dict({
                    "url": proxy.url, "username_env": "ANET_MISSING_USER", "password_env": "ANET_MISSING_PASS"
                })
                from anet.transport import open_proxy_tls_connection
                with pytest.raises(ConnectionError, match="missing"):
                    await open_proxy_tls_connection(cfg, "127.0.0.1", 1, node._client_context)
                monkeypatch.setenv("ANET_MISSING_USER", "alice")
                monkeypatch.setenv("ANET_MISSING_PASS", "wrong")
                with pytest.raises(ConnectionError, match="failed"):
                    await open_proxy_tls_connection(cfg, "127.0.0.1", 1, node._client_context)
        finally:
            node.close()

    asyncio.run(scenario())
