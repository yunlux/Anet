from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from amesh.adapters.loopback import (
    LoopbackAdapter,
    LoopbackConfig,
    LoopbackLedger,
    loopback_database_path,
    loopback_key_path,
)
from amesh.agent import AgentStore, agent_database_path
from amesh.connector import ConnectorAudit, EffectConnector, amesh_audit_path
from amesh.policy import SocialPolicy, SocialThreshold

LOW_POLICY = SocialPolicy(
    surface=SocialThreshold(0, 0),
    reply=SocialThreshold(0, 0),
    amplify=SocialThreshold(0, 0),
    connect_candidate=SocialThreshold(0, 0, ("relationship:vouched",)),
)


def _prepare_home(tmp_path):
    LoopbackConfig(channels=("lobby",), policy=LOW_POLICY).save(tmp_path)
    adapter = LoopbackAdapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh hello")
        adapter.poll_once()
    finally:
        adapter.close()
    ledger = LoopbackLedger(
        loopback_database_path(tmp_path),
        loopback_key_path(tmp_path),
    )
    try:
        event_key = ledger.events()[0]["event_key"]
    finally:
        ledger.close()
    return tmp_path, event_key


def _post(port, token, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/effects",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _running_connector(home):
    connector = EffectConnector(home, host="127.0.0.1", port=0).start()
    thread = threading.Thread(target=connector.serve_forever, daemon=True)
    thread.start()
    return connector, thread


def test_connector_health_and_audit_flow(tmp_path) -> None:
    home, event_key = _prepare_home(tmp_path)
    store = AgentStore(agent_database_path(home))
    try:
        registered = store.register("researcher", "Research Agent", scopes=("reply",))
        store.grant("researcher", "loopback", "reply", "allow")
        token = registered["token"]
    finally:
        store.close()

    connector, thread = _running_connector(home)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{connector.port}/v1/health", timeout=10
        ) as response:
            assert response.status == 200

        status, result = _post(
            connector.port,
            token,
            {
                "adapter": "loopback",
                "action": "reply",
                "event_key": event_key,
                "content": "hello from the connector",
            },
        )
        assert status == 200
        assert result["sent"] is True
    finally:
        connector.shutdown()
        thread.join(timeout=10)

    audit = ConnectorAudit(amesh_audit_path(home))
    try:
        rows = audit.recent()
        assert rows[0]["outcome"] == "authorized"
        assert rows[0]["agent_id"] == "researcher"
        assert rows[0]["code"] == 200
    finally:
        audit.close()


def test_connector_rejects_bad_token(tmp_path) -> None:
    home, event_key = _prepare_home(tmp_path)
    connector, thread = _running_connector(home)
    try:
        status, _ = _post(
            connector.port,
            "not-a-real-token",
            {
                "adapter": "loopback",
                "action": "reply",
                "event_key": event_key,
                "content": "hi",
            },
        )
        assert status == 401
    finally:
        connector.shutdown()
        thread.join(timeout=10)

    audit = ConnectorAudit(amesh_audit_path(home))
    try:
        rows = audit.recent()
        assert rows[0]["outcome"] == "rejected"
        assert rows[0]["code"] == 401
        assert rows[0]["agent_id"] == ""
    finally:
        audit.close()


def test_connector_denies_without_grant(tmp_path) -> None:
    home, event_key = _prepare_home(tmp_path)
    store = AgentStore(agent_database_path(home))
    try:
        registered = store.register("silent", "Silent Agent", scopes=("reply",))
        token = registered["token"]
    finally:
        store.close()

    connector, thread = _running_connector(home)
    try:
        status, result = _post(
            connector.port,
            token,
            {
                "adapter": "loopback",
                "action": "reply",
                "event_key": event_key,
                "content": "hi",
            },
        )
        assert status == 403
        assert "no reply grant" in result["error"]
    finally:
        connector.shutdown()
        thread.join(timeout=10)

    audit = ConnectorAudit(amesh_audit_path(home))
    try:
        rows = audit.recent()
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["agent_id"] == "silent"
        assert rows[0]["code"] == 403
    finally:
        audit.close()


def test_connector_rejects_unsupported_action_and_missing_auth(tmp_path) -> None:
    home, event_key = _prepare_home(tmp_path)
    store = AgentStore(agent_database_path(home))
    try:
        registered = store.register("curious", "Curious Agent", scopes=("admin",))
        token = registered["token"]
    finally:
        store.close()

    connector, thread = _running_connector(home)
    try:
        status, result = _post(
            connector.port,
            token,
            {
                "adapter": "loopback",
                "action": "admin",
                "event_key": event_key,
                "content": "boom",
            },
        )
        assert status == 400
        assert "action" in result["error"]

        request = urllib.request.Request(
            f"http://127.0.0.1:{connector.port}/v1/effects",
            data=json.dumps({"adapter": "loopback", "action": "reply"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 401
    finally:
        connector.shutdown()
        thread.join(timeout=10)
