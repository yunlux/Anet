from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from anet.ahub import (
    AhubService,
    issue_ahub_request,
    issue_destination_settlement,
)
from anet.ahub_http import (
    AhubASGI,
    AhubHTTPClient,
    AhubRateLimit,
    AhubRateLimiter,
    ahub_json,
)
from anet.control_plane import issue_node_descriptor
from anet.identity import Identity
from anet.packet import inspect_packet, seal_packet


NOW = int(time.time() * 1000)


async def invoke(
    app: AhubASGI,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    received = False
    events: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "client": client,
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in (headers or {}).items()
            ],
        },
        receive,
        send,
    )
    start, response = events
    response_headers = {
        bytes(key).decode("latin-1"): bytes(value).decode("latin-1")
        for key, value in start["headers"]
    }
    return (
        int(start["status"]),
        json.loads(bytes(response["body"]).decode("utf-8")),
        response_headers,
    )


def signed_headers(
    identity: Identity,
    method: str,
    path: str,
    body: bytes = b"",
) -> dict[str, str]:
    return issue_ahub_request(
        identity,
        method=method,
        path=path,
        body=body,
    ).to_headers()


@pytest.mark.asyncio
async def test_asgi_rendezvous_mailbox_vertical_slice(tmp_path: Path) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")

    with AhubService(tmp_path / "ahub") as service:
        service.allow_node(sender.node_id)
        service.allow_node(recipient.node_id)
        app = AhubASGI(service)
        for identity in (sender, recipient):
            descriptor = issue_node_descriptor(
                identity,
                capabilities=("agent.task",),
                ttl_ms=60 * 60 * 1000,
            )
            path = f"/v1/descriptors/{identity.node_id}"
            status, value, _ = await invoke(
                app,
                "PUT",
                path,
                body=ahub_json(descriptor.to_dict()),
            )
            assert status == 200
            assert value["accepted"]

        lookup_path = f"/v1/nodes/{recipient.node_id}"
        status, value, headers = await invoke(
            app,
            "GET",
            lookup_path,
            headers=signed_headers(sender, "GET", lookup_path),
        )
        assert status == 200
        assert value["descriptor"]["node_id"] == recipient.node_id
        assert value["reachability"] is None
        assert headers["cache-control"] == "no-store"

        raw = seal_packet(
            sender,
            recipient.card(),
            kind="agent.message",
            body={"text": "encrypted"},
            ttl_seconds=60,
        )
        status, custody, _ = await invoke(
            app,
            "POST",
            "/v1/mailbox",
            body=raw,
            headers=signed_headers(sender, "POST", "/v1/mailbox", raw),
        )
        assert status == 200
        assert custody["meaning"] == "ahub_custody_only"
        assert custody["packet_id"] == inspect_packet(raw).packet_id

        claim_body = ahub_json({"lease_ms": 5_000, "limit": 10})
        status, claims, _ = await invoke(
            app,
            "POST",
            "/v1/mailbox/claims",
            body=claim_body,
            headers=signed_headers(
                recipient, "POST", "/v1/mailbox/claims", claim_body
            ),
        )
        assert status == 200
        assert len(claims["packets"]) == 1
        claim = claims["packets"][0]

        packet_id = custody["packet_id"]
        settle_path = f"/v1/mailbox/{packet_id}/settle"
        proof = issue_destination_settlement(
            recipient,
            packet_id=packet_id,
            raw=raw,
            uploader_id=sender.node_id,
            expires_ms=custody["expires_ms"],
        )
        settle_body = ahub_json(
            {
                "claim_token": claim["claim_token"],
                "proof": proof.to_dict(),
            }
        )
        status, settled, _ = await invoke(
            app,
            "POST",
            settle_path,
            body=settle_body,
            headers=signed_headers(
                recipient, "POST", settle_path, settle_body
            ),
        )
        assert status == 200
        assert settled == {"settled": True}

        settlement_body = ahub_json(
            {"destination_id": recipient.node_id, "limit": 10}
        )
        status, settlements, _ = await invoke(
            app,
            "POST",
            "/v1/mailbox/settlements",
            body=settlement_body,
            headers=signed_headers(
                sender,
                "POST",
                "/v1/mailbox/settlements",
                settlement_body,
            ),
        )
        assert status == 200
        assert settlements["meaning"] == "destination_settlement_only"
        assert settlements["settlements"][0]["packet_id"] == packet_id

        ack_path = f"/v1/mailbox/settlements/{packet_id}/ack"
        status, acked, _ = await invoke(
            app,
            "POST",
            ack_path,
            headers=signed_headers(sender, "POST", ack_path),
        )
        assert status == 200
        assert acked == {"acknowledged": True}

        status, settlements, _ = await invoke(
            app,
            "POST",
            "/v1/mailbox/settlements",
            body=settlement_body,
            headers=signed_headers(
                sender,
                "POST",
                "/v1/mailbox/settlements",
                settlement_body,
            ),
        )
        assert status == 200
        assert settlements["settlements"] == []

        status, duplicate, _ = await invoke(
            app,
            "POST",
            "/v1/mailbox",
            body=raw,
            headers=signed_headers(sender, "POST", "/v1/mailbox", raw),
        )
        assert status == 200
        assert duplicate["stored"] is False


@pytest.mark.asyncio
async def test_asgi_rejects_missing_auth_replay_and_route_mismatch(
    tmp_path: Path,
) -> None:
    caller = Identity.generate("caller")
    target = Identity.generate("target")
    with AhubService(tmp_path / "ahub") as service:
        for identity in (caller, target):
            service.allow_node(identity.node_id)
            service.publish_descriptor(
                issue_node_descriptor(
                    identity,
                    capabilities=(),
                    ttl_ms=60 * 60 * 1000,
                )
            )
        app = AhubASGI(service)
        path = f"/v1/nodes/{target.node_id}"

        status, _, _ = await invoke(app, "GET", path)
        assert status == 400

        headers = signed_headers(caller, "GET", path)
        status, _, _ = await invoke(app, "GET", path, headers=headers)
        assert status == 200
        status, value, _ = await invoke(app, "GET", path, headers=headers)
        assert status == 403
        assert value["error"] == "forbidden"

        wrong_path = f"/v1/nodes/{caller.node_id}"
        status, value, _ = await invoke(
            app, "GET", wrong_path, headers=signed_headers(caller, "GET", path)
        )
        assert status == 403
        assert value["error"] == "invalid_signature"


def test_http_client_requires_tls_by_default() -> None:
    identity = Identity.generate("client")
    with pytest.raises(ValueError, match="requires TLS"):
        AhubHTTPClient("http://ahub.example", identity)
    client = AhubHTTPClient(
        "http://127.0.0.1:8080",
        identity,
        allow_insecure_http=True,
    )
    assert client.base_url == "http://127.0.0.1:8080"


@pytest.mark.asyncio
async def test_asgi_rate_limit_rejects_and_recovers_by_peer(
    tmp_path: Path,
) -> None:
    now = [0.0]
    with AhubService(tmp_path / "ahub") as service:
        app = AhubASGI(
            service,
            rate_limit=AhubRateLimit(requests_per_minute=60, burst=2),
            clock=lambda: now[0],
        )
        for _ in range(2):
            status, value, _ = await invoke(
                app,
                "GET",
                "/healthz",
                client=("192.0.2.10", 1000),
            )
            assert status == 200
            assert value["status"] == "ok"

        status, value, headers = await invoke(
            app,
            "GET",
            "/healthz",
            client=("192.0.2.10", 1001),
        )
        assert status == 429
        assert value == {
            "detail": "request rate limit exceeded",
            "error": "rate_limited",
        }
        assert headers["retry-after"] == "1"

        status, _, _ = await invoke(
            app,
            "GET",
            "/healthz",
            client=("192.0.2.11", 1000),
        )
        assert status == 200

        now[0] = 1.0
        status, _, _ = await invoke(
            app,
            "GET",
            "/healthz",
            client=("192.0.2.10", 1002),
        )
        assert status == 200


def test_rate_limiter_bucket_bound_does_not_reset_existing_peer() -> None:
    limiter = AhubRateLimiter(
        AhubRateLimit(requests_per_minute=60, burst=1, max_buckets=1),
        clock=lambda: 0.0,
    )
    assert limiter.allow("peer:a") == (True, 0)
    assert limiter.allow("peer:a") == (False, 1)
    assert limiter.allow("peer:a") == (False, 1)
    assert limiter.status() == {
        "active_buckets": 1,
        "limited_requests": 2,
    }


@pytest.mark.asyncio
async def test_health_and_request_logs_expose_no_node_or_packet_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    node = Identity.generate("node")
    with AhubService(tmp_path / "ahub") as service:
        service.allow_node(node.node_id)
        service.publish_descriptor(
            issue_node_descriptor(
                node, capabilities=(), ttl_ms=60 * 60 * 1000
            )
        )
        app = AhubASGI(service)
        caplog.set_level(logging.INFO, logger="anet.ahub_http")

        status, value, _ = await invoke(app, "GET", "/healthz")
        assert status == 200
        assert value == {
            "protocol": 1,
            "service": "anet-ahub",
            "status": "ok",
        }

        path = f"/v1/nodes/{node.node_id}"
        status, _, _ = await invoke(
            app,
            "GET",
            path,
            headers=signed_headers(node, "GET", path),
        )
        assert status == 200
        logs = caplog.text
        assert "route=health" in logs
        assert "route=node_lookup" in logs
        assert node.node_id not in logs
