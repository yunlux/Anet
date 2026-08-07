from __future__ import annotations

import json
import time

from amesh.cli import main
from amesh.policy import PermissionStore, amesh_database_path

ACTOR = "a" * 64


def test_adapter_list_unconfigured(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "adapter", "list"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["adapters"][0]["name"] == "discord"
    assert out["adapters"][0]["configured"] is False


def test_permit_add_list_revoke(tmp_path, capsys) -> None:
    code = main(
        [
            "--home",
            str(tmp_path),
            "permit",
            "add",
            "discord",
            ACTOR,
            "surface",
            "deny",
            "--reason",
            "quiet channel",
        ]
    )
    assert code == 0
    rule = json.loads(capsys.readouterr().out)
    assert rule["adapter"] == "discord"
    assert rule["actor_key"] == ACTOR
    assert rule["effect"] == "deny"

    code = main(["--home", str(tmp_path), "permit", "list", "discord"])
    assert code == 0
    listing = json.loads(capsys.readouterr().out)
    assert [item["rule_id"] for item in listing["rules"]] == [rule["rule_id"]]

    code = main(
        ["--home", str(tmp_path), "permit", "revoke", "discord", rule["rule_id"]]
    )
    assert code == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["removed"] is True

    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        assert store.rules(adapter="discord") == []
    finally:
        store.close()


def test_permit_wildcard_rule(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "permit", "add", "discord", "*", "*", "deny"])
    assert code == 0
    rule = json.loads(capsys.readouterr().out)
    assert rule["actor_key"] == "*"
    assert rule["action"] == "*"


def test_permit_decisions(tmp_path, capsys) -> None:
    store = PermissionStore(amesh_database_path(tmp_path))
    try:
        store.record_decision("discord", ACTOR, "surface", "deny")
    finally:
        store.close()
    code = main(["--home", str(tmp_path), "permit", "decisions", "discord"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["effect"] == "deny"


def test_unknown_adapter_fails(tmp_path, capsys) -> None:
    code = main(
        ["--home", str(tmp_path), "permit", "add", "telegram", ACTOR, "*", "deny"]
    )
    assert code == 1
    assert "unknown Amesh adapter" in capsys.readouterr().err


def test_invalid_actor_key_fails(tmp_path, capsys) -> None:
    code = main(
        ["--home", str(tmp_path), "permit", "add", "discord", "short", "*", "deny"]
    )
    assert code == 1
    assert "invalid Amesh actor key" in capsys.readouterr().err


def test_social_actor_needs_configured_bridge(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "social", "actor", "discord", ACTOR])
    assert code == 1
    assert (
        "not configured" in capsys.readouterr().err
        or "missing" in capsys.readouterr().err
    )


def test_adapter_list_includes_loopback(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "adapter", "list"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in out["adapters"]}
    assert names == {"discord", "loopback"}


def test_loopback_setup_inject_poll(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "adapter", "setup", "loopback"])
    assert code == 0
    setup = json.loads(capsys.readouterr().out)
    assert setup["configured"] is True

    code = main(
        [
            "--home",
            str(tmp_path),
            "social",
            "inject",
            "loopback",
            "alice",
            "--text",
            "@amesh hello",
        ]
    )
    assert code == 0
    injected = json.loads(capsys.readouterr().out)
    assert injected["spooled"] is True

    code = main(["--home", str(tmp_path), "social", "poll", "loopback"])
    assert code == 0
    poll = json.loads(capsys.readouterr().out)
    assert poll["ingested"] == 1

    code = main(["--home", str(tmp_path), "adapter", "status", "loopback"])
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["events"] == 1


def test_social_signals_lists_outbound(tmp_path, capsys) -> None:
    from amesh.adapters.loopback import LoopbackAdapter, LoopbackConfig
    from amesh.serve import amesh_outbound_dir
    from amesh.signal import DirectorySignalSink
    from amesh.policy import SocialPolicy, SocialThreshold

    low = SocialPolicy(
        surface=SocialThreshold(0, 0),
        reply=SocialThreshold(0, 0),
        amplify=SocialThreshold(0, 0),
        connect_candidate=SocialThreshold(0, 0, ("relationship:vouched",)),
    )
    LoopbackConfig(
        channels=("lobby",),
        policy=low,
        destination_id="agent-main",
        poll_interval_seconds=1.0,
    ).save(tmp_path)
    sink = DirectorySignalSink(amesh_outbound_dir(tmp_path))
    adapter = LoopbackAdapter(tmp_path)
    try:
        adapter.inject("alice", "@amesh hi")
        adapter.poll_once(
            queue_signal=lambda destination_id, kind, body: sink.emit(dict(body))
        )
    finally:
        adapter.close()

    code = main(["--home", str(tmp_path), "social", "signals", "loopback"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["signals"][0]["provenance"]["platform"] == "loopback"


def test_route_status_policy_and_flush(tmp_path, capsys) -> None:
    from amesh.route import RouteStore, route_database_path
    from amesh.signal import build_signal

    code = main(
        [
            "--home",
            str(tmp_path),
            "route",
            "policy",
            "blocked-agent",
            "loopback",
            "deny",
        ]
    )
    assert code == 0
    rule = json.loads(capsys.readouterr().out)
    assert rule["allowed"] is False

    code = main(["--home", str(tmp_path), "route", "policy-list"])
    assert code == 0
    assert (
        json.loads(capsys.readouterr().out)["rules"][0]["destination"]
        == "blocked-agent"
    )

    store = RouteStore(route_database_path(tmp_path))
    try:
        created = int(time.time() * 1000)
        signal = build_signal(
            platform="loopback",
            adapter="loopback-spool-v1",
            source_event_id="c" * 32,
            actor_key="d" * 64,
            created_ms=created,
            expires_ms=created + 1_000_000,
            content_level="mention",
            content="hi",
            labels=set(),
            evaluation={
                "action": "surface",
                "allowed_actions": ["observe", "surface"],
                "reasons": ["test"],
                "policy_version": 1,
                "reputation": {
                    "score": 50,
                    "raw_score": 50,
                    "confidence": 0,
                    "algorithm": "amesh-evidence-v1",
                },
            },
            provenance={},
        )
        store.enqueue("review-agent", "loopback", "amesh.loopback.signal", signal)
    finally:
        store.close()

    code = main(["--home", str(tmp_path), "route", "status"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 1

    code = main(["--home", str(tmp_path), "route", "flush"])
    assert code == 0
    flushed = json.loads(capsys.readouterr().out)
    assert flushed["delivery"]["delivered"] == 1

    code = main(["--home", str(tmp_path), "route", "list", "--state", "delivered"])
    assert code == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["routes"]) == 1
    assert listed["routes"][0]["destination"] == "review-agent"
