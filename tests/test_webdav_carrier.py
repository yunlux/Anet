from __future__ import annotations

import asyncio
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anet.carriers.webdav import sync_webdav_once
from anet.config import NodeConfig, RoutingConfig, WebDAVCarrierConfig, initialize_node
from anet.identity import Identity
from anet.node import AnetNode
from anet.peers import PeerBook


class MemoryWebDAVHandler(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    collections: set[str] = {"/dav/"}
    lock = threading.Lock()
    authorization = "Bearer test-secret"
    available = True
    delay_seconds = 0.0
    propfind_paths: list[str] = []

    def log_message(self, format, *args):  # noqa: ANN001, ANN201
        return

    def _authorized(self) -> bool:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if not self.available:
            self.send_response(503)
            self.end_headers()
            return False
        if self.headers.get("Authorization") == self.authorization:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        return False

    def _body(self) -> bytes:
        size = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(size)

    def do_MKCOL(self) -> None:
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.endswith("/"):
            path += "/"
        with self.lock:
            exists = path in self.collections
            self.collections.add(path)
        self.send_response(405 if exists else 201)
        self.end_headers()

    def do_PUT(self) -> None:
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        body = self._body()
        with self.lock:
            if self.headers.get("If-None-Match") == "*" and path in self.objects:
                status = 412
            else:
                status = 204 if path in self.objects else 201
                self.objects[path] = body
        self.send_response(status)
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        with self.lock:
            body = self.objects.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        with self.lock:
            existed = self.objects.pop(path, None) is not None
        self.send_response(204 if existed else 404)
        self.end_headers()

    def do_MOVE(self) -> None:
        if not self._authorized():
            return
        source = urllib.parse.urlsplit(self.path).path
        destination = urllib.parse.urlsplit(self.headers.get("Destination", "")).path
        with self.lock:
            body = self.objects.pop(source, None)
            if body is not None:
                self.objects[destination] = body
        self.send_response(201 if body is not None else 404)
        self.end_headers()

    def do_PROPFIND(self) -> None:
        if not self._authorized():
            return
        prefix = urllib.parse.urlsplit(self.path).path
        if not prefix.endswith("/"):
            prefix += "/"
        with self.lock:
            self.propfind_paths.append(prefix)
            exists = prefix in self.collections
            child_collections = sorted(
                path
                for path in self.collections
                if path != prefix
                and path.startswith(prefix)
                and "/" not in path[len(prefix) :].strip("/")
            )
            names = sorted(path for path in self.objects if path.startswith(prefix))
        if not exists:
            self.send_response(404)
            self.end_headers()
            return
        hrefs = [prefix, *child_collections, *names]
        responses = "".join(
            f"<d:response><d:href>{href}</d:href><d:propstat>"
            f"<d:prop><d:resourcetype/></d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            for href in hrefs
        )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:multistatus xmlns:d="DAV:">{responses}</d:multistatus>'
        ).encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def webdav_server():
    handler = type(
        "IsolatedMemoryWebDAVHandler",
        (MemoryWebDAVHandler,),
        {
            "objects": {},
            "collections": {"/dav/"},
            "lock": threading.Lock(),
            "available": True,
            "delay_seconds": 0.0,
            "propfind_paths": [],
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/dav", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_nodes(root: Path) -> tuple[AnetNode, AnetNode]:
    a_config = initialize_node(root / "a", label="a", listen_port=47101)
    b_config = initialize_node(root / "b", label="b", listen_port=47102)
    a_identity = Identity.load(a_config.identity_path)
    b_identity = Identity.load(b_config.identity_path)
    a_card = a_identity.card(addresses=(), capabilities=a_config.capabilities)
    b_card = b_identity.card(addresses=(), capabilities=b_config.capabilities)
    PeerBook(a_config.peers_path, own_node_id=a_identity.node_id).add(b_card)
    PeerBook(b_config.peers_path, own_node_id=b_identity.node_id).add(a_card)
    return AnetNode(a_config), AnetNode(b_config)


def test_webdav_carrier_is_outbound_only_opaque_and_acknowledged(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ANET_TEST_WEBDAV_TOKEN", "test-secret")
    with webdav_server() as (base_url, handler):
        config = WebDAVCarrierConfig.from_dict(
            {
                "name": "dav",
                "base_url": base_url,
                "allow_insecure_http": True,
                "bearer_env": "ANET_TEST_WEBDAV_TOKEN",
                "retry_seconds": 0,
            }
        )
        a, b = make_nodes(tmp_path)
        try:
            packet_id = a.queue(
                b.node_id, kind="evidence", body={"secret": "opaque-dav"}
            )
            first = sync_webdav_once(a, config, retry_after_ms=0)
            assert first["pushed_packets"] == 1
            stored = list(handler.objects.values())
            assert len(stored) == 1
            assert b"opaque-dav" not in stored[0]
            assert packet_id.encode() not in stored[0]
            assert a.node_id.encode() not in stored[0]
            assert b.node_id.encode() not in stored[0]
            object_path = next(iter(handler.objects))
            collection, filename = object_path.rstrip("/").rsplit("/", 1)
            assert not collection.rsplit("/", 1)[-1].startswith("ch-")
            assert len(filename) == 48
            assert filename.isalnum()
            second = sync_webdav_once(b, config, retry_after_ms=0)
            assert second["pulled_packets"] == 1
            # Pulling checks historical/legacy epochs but must not create a
            # collection for every empty candidate. Only the base and two
            # directions used for real traffic exist.
            assert len(handler.collections) == 3
            # First sender pass lists only the base. Receiver pass lists the
            # base and the one existing derived mailbox; it never probes the
            # other derivable historical/future tokens.
            assert len(handler.propfind_paths) == 3
            assert handler.propfind_paths.count("/dav/") == 2
            message = next(
                item for item in b.store.list_inbox() if item["packet_id"] == packet_id
            )
            assert message["trusted"] is True
            assert message["body"] == {"secret": "opaque-dav"}

            third = sync_webdav_once(a, config, retry_after_ms=0)
            assert third["pulled_acks"] == 1
            assert third["pulled_packets"] == 1
            fourth = sync_webdav_once(b, config, retry_after_ms=0)
            assert fourth["pulled_acks"] == 1
            assert a.store.status()["pending"] == 0
            assert b.store.status()["pending"] == 0
            assert any(
                item["path_id"] == "webdav:dav" and item["state"] == "acked"
                for item in a.store.delivery_paths(packet_id)
            )
        finally:
            a.close()
            b.close()


def test_webdav_v2_rolls_down_to_v1_for_a_legacy_peer_card(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ANET_TEST_WEBDAV_TOKEN", "test-secret")
    with webdav_server() as (base_url, handler):
        config = WebDAVCarrierConfig.from_dict(
            {
                "name": "dav",
                "base_url": base_url,
                "allow_insecure_http": True,
                "bearer_env": "ANET_TEST_WEBDAV_TOKEN",
                "retry_seconds": 0,
            }
        )
        a, b = make_nodes(tmp_path)
        try:
            legacy_caps = ("webdav-carrier-v1",)
            PeerBook(a.config.peers_path, own_node_id=a.node_id).add(
                b.identity.card(addresses=(), capabilities=legacy_caps)
            )
            PeerBook(b.config.peers_path, own_node_id=b.node_id).add(
                a.identity.card(addresses=(), capabilities=legacy_caps)
            )
            a.peers.reload()
            b.peers.reload()

            packet_id = a.queue(b.node_id, kind="message", body="rolling-upgrade")
            assert sync_webdav_once(a, config, retry_after_ms=0)["pushed_packets"] == 1
            object_path = next(iter(handler.objects))
            collection, filename = object_path.rstrip("/").rsplit("/", 1)
            assert collection.rsplit("/", 1)[-1].startswith("ch-")
            assert filename.endswith(".drop")

            assert sync_webdav_once(b, config, retry_after_ms=0)["pulled_packets"] == 1
            assert any(item["packet_id"] == packet_id for item in b.store.list_inbox())
        finally:
            a.close()
            b.close()


def test_webdav_rejects_plain_remote_http_and_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="loopback"):
        WebDAVCarrierConfig.from_dict(
            {
                "name": "bad",
                "base_url": "http://example.com/dav",
                "allow_insecure_http": True,
            }
        )
    with pytest.raises(ValueError, match="credentials"):
        WebDAVCarrierConfig.from_dict(
            {"name": "bad", "base_url": "https://user:password@example.com/dav"}
        )


def test_adaptive_router_survives_primary_webdav_outage_and_recovers(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANET_TEST_WEBDAV_TOKEN", "test-secret")
    with (
        webdav_server() as (primary_url, primary_handler),
        webdav_server() as (
            secondary_url,
            _secondary_handler,
        ),
    ):
        primary = WebDAVCarrierConfig.from_dict(
            {
                "name": "primary",
                "base_url": primary_url,
                "allow_insecure_http": True,
                "bearer_env": "ANET_TEST_WEBDAV_TOKEN",
                "priority": 10,
                "retry_seconds": 0,
                "timeout": 1,
            }
        )
        secondary = WebDAVCarrierConfig.from_dict(
            {
                "name": "secondary",
                "base_url": secondary_url,
                "allow_insecure_http": True,
                "bearer_env": "ANET_TEST_WEBDAV_TOKEN",
                "priority": 20,
                "retry_seconds": 0,
                "timeout": 1,
            }
        )
        a_base = initialize_node(tmp_path / "a", label="a", listen_port=47201)
        b_base = initialize_node(tmp_path / "b", label="b", listen_port=47202)
        a_identity = Identity.load(a_base.identity_path)
        b_identity = Identity.load(b_base.identity_path)
        PeerBook(a_base.peers_path, own_node_id=a_identity.node_id).add(
            b_identity.card(addresses=(), capabilities=b_base.capabilities)
        )
        PeerBook(b_base.peers_path, own_node_id=b_identity.node_id).add(
            a_identity.card(addresses=(), capabilities=a_base.capabilities)
        )
        routing = RoutingConfig(
            carrier_failure_threshold=2,
            carrier_recovery_threshold=3,
            switch_cooldown=0,
        )
        for base in (a_base, b_base):
            replace(
                base,
                listen_enabled=False,
                direct_enabled=False,
                webdav_carriers=(primary, secondary),
                routing=routing,
            ).save()
        a = AnetNode(NodeConfig.load(a_base.home))
        b = AnetNode(NodeConfig.load(b_base.home))
        try:
            warmup = a.queue(b.node_id, kind="message", body="primary")
            asyncio.run(a.adaptive_sync_once())
            asyncio.run(b.adaptive_sync_once())
            asyncio.run(a.adaptive_sync_once())
            asyncio.run(b.adaptive_sync_once())
            assert a.store.route(b.node_id)["selected_path"] == "webdav:primary", {
                "route": a.store.route(b.node_id),
                "metrics": a.store.path_metrics(b.node_id),
            }
            assert any(item["packet_id"] == warmup for item in b.store.list_inbox())

            primary_handler.available = False
            failover_packet = a.queue(b.node_id, kind="message", body="secondary")
            for _ in range(3):
                asyncio.run(a.adaptive_sync_once())
            assert a.store.route(b.node_id)["selected_path"] == "webdav:secondary"
            for _ in range(10):
                asyncio.run(b.adaptive_sync_once())
                asyncio.run(a.adaptive_sync_once())
                if (
                    any(item["packet_id"] == failover_packet for item in b.store.list_inbox())
                    and a.store.status()["pending"] == 0
                ):
                    break
            assert any(
                item["packet_id"] == failover_packet for item in b.store.list_inbox()
            )
            assert a.store.status()["pending"] == 0
            assert any(
                item["path_id"] == "webdav:secondary" and item["state"] == "acked"
                for item in a.store.delivery_paths(failover_packet)
            )

            primary_handler.available = True
            # Recovery requires three successful probes plus a later routing
            # decision. Poll the observable route with a hard bound instead
            # of assuming HTTP worker scheduling completes in four rounds.
            for _ in range(10):
                asyncio.run(a.adaptive_sync_once())
                if a.store.route(b.node_id)["selected_path"] == "webdav:primary":
                    break
            assert a.store.route(b.node_id)["selected_path"] == "webdav:primary", {
                "route": a.store.route(b.node_id),
                "metrics": a.store.path_metrics(b.node_id),
            }
            assert "preferred fallback" in a.store.route(b.node_id)["reason"]
        finally:
            a.close()
            b.close()
