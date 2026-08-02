from __future__ import annotations

import json
import time

from anet.ahub_http import AhubHTTPClient
from anet.carriers.ahub import current_node_descriptor, current_node_reachability
from anet.cli import main
from anet.config import NodeConfig
from anet.control_plane import (
    HumanPrincipalIdentity,
    issue_human_device_grant,
    issue_human_device_revocation,
    issue_node_descriptor,
)
from anet.identity import Identity
from anet.node import AnetNode


def last_json(capsys):
    output = capsys.readouterr().out
    return json.loads(output)


def test_cli_imports_and_queries_signed_human_device_authority(
    tmp_path, capsys
) -> None:
    home = tmp_path / "node_b"
    assert (
        main(
            [
                "--home",
                str(home),
                "init",
                "--label",
                "node_b",
                "--port",
                "44100",
            ]
        )
        == 0
    )
    last_json(capsys)

    now_ms = int(time.time() * 1000)
    device = Identity.generate("primary-phone")
    descriptor = issue_node_descriptor(
        device,
        capabilities=("approval.sign",),
        issued_ms=now_ms,
        ttl_ms=60 * 60 * 1000,
    )
    human = HumanPrincipalIdentity.generate()
    grant = issue_human_device_grant(
        human,
        descriptor,
        capabilities=("approval.sign",),
        issued_ms=now_ms,
        ttl_ms=60 * 60 * 1000,
    )
    revocation = issue_human_device_revocation(
        human,
        descriptor,
        sequence=2,
        previous_digest=grant.digest,
        revoked_ms=now_ms + 1,
        reason_code="device-lost",
    )
    descriptor_path = tmp_path / "descriptor.json"
    grant_path = tmp_path / "grant.json"
    revocation_path = tmp_path / "revocation.json"
    descriptor_path.write_text(
        json.dumps(descriptor.to_dict()), encoding="utf-8"
    )
    grant_path.write_text(json.dumps(grant.to_dict()), encoding="utf-8")
    revocation_path.write_text(
        json.dumps(revocation.to_dict()), encoding="utf-8"
    )

    assert (
        main(
            [
                "--home",
                str(home),
                "control-import",
                str(descriptor_path),
            ]
        )
        == 0
    )
    imported_descriptor = last_json(capsys)
    assert imported_descriptor["node_id"] == device.node_id
    assert imported_descriptor["changed"] is True

    assert (
        main(
            ["--home", str(home), "control-import", str(grant_path)]
        )
        == 0
    )
    imported_grant = last_json(capsys)
    assert imported_grant["human_id"] == human.human_id
    assert imported_grant["capabilities"] == ["approval.sign"]

    assert (
        main(
            [
                "--home",
                str(home),
                "control-device",
                human.human_id,
                device.node_id,
            ]
        )
        == 0
    )
    current = last_json(capsys)
    assert current["authorized"] is True
    assert current["revoked"] is False

    assert (
        main(
            [
                "--home",
                str(home),
                "control-import",
                str(revocation_path),
            ]
        )
        == 0
    )
    assert last_json(capsys)["revoked"] is True
    assert (
        main(
            [
                "--home",
                str(home),
                "control-device",
                human.human_id,
                device.node_id,
            ]
        )
        == 0
    )
    revoked = last_json(capsys)
    assert revoked["authorized"] is False
    assert revoked["revoked"] is True
    assert revoked["capabilities"] == []


def test_cli_initializes_trusts_queues_and_exports(tmp_path, capsys) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a_card = tmp_path / "a.card.json"
    b_card = tmp_path / "b.card.json"

    assert main(["--home", str(a), "init", "--label", "a", "--port", "44101"]) == 0
    a_init = last_json(capsys)
    assert main(["--home", str(b), "init", "--label", "b", "--port", "44102"]) == 0
    b_init = last_json(capsys)

    assert main(["--home", str(a), "card", "--out", str(a_card)]) == 0
    last_json(capsys)
    assert main(["--home", str(b), "card", "--out", str(b_card)]) == 0
    last_json(capsys)
    assert main(["--home", str(a), "peer-add", str(b_card)]) == 0
    last_json(capsys)
    assert main(["--home", str(b), "peer-add", str(a_card)]) == 0
    last_json(capsys)

    a_prekeys = tmp_path / "a.prekeys.json"
    b_prekeys = tmp_path / "b.prekeys.json"
    assert (
        main(
            [
                "--home",
                str(a),
                "prekey-generate",
                str(a_prekeys),
                "--count",
                "4",
            ]
        )
        == 0
    )
    assert last_json(capsys)["generated"] == 4
    assert (
        main(
            [
                "--home",
                str(b),
                "prekey-generate",
                str(b_prekeys),
                "--count",
                "4",
            ]
        )
        == 0
    )
    assert last_json(capsys)["generated"] == 4
    assert main(["--home", str(a), "prekey-import", str(b_prekeys)]) == 0
    assert last_json(capsys)["inserted"] == 4
    assert main(["--home", str(b), "prekey-import", str(a_prekeys)]) == 0
    assert last_json(capsys)["inserted"] == 4
    assert main(["--home", str(a), "prekey-policy", "require"]) == 0
    assert last_json(capsys)["prekey_policy"] == "require"
    assert (
        main(
            [
                "--home",
                str(a),
                "prekey-config",
                "--low-watermark",
                "2",
                "--batch-size",
                "4",
                "--request-interval",
                "30",
            ]
        )
        == 0
    )
    configured = last_json(capsys)
    assert configured["prekey_auto_enabled"] is True
    assert configured["prekey_low_watermark"] == 2
    assert configured["prekey_batch_size"] == 4
    assert main(["--home", str(b), "prekey-policy", "require"]) == 0
    last_json(capsys)

    assert (
        main(
            [
                "--home",
                str(a),
                "send",
                b_init["node_id"],
                "--kind",
                "agent.ir",
                "--json-body",
                '{"performative":"QUERY"}',
                "--qos",
                "control",
            ]
        )
        == 0
    )
    sent = last_json(capsys)
    assert len(sent["queued"]) == 32
    assert sent["key_mode"] == "opk"
    assert sent["forward_secrecy"] is True

    bundle = tmp_path / "carry.anet"
    assert (
        main(
            [
                "--home",
                str(a),
                "bundle-export",
                str(bundle),
                "--destination",
                b_init["node_id"],
            ]
        )
        == 0
    )
    exported = last_json(capsys)
    assert exported["packets"] == 1
    assert main(["--home", str(b), "bundle-import", str(bundle)]) == 0
    imported = last_json(capsys)
    assert imported["imported"] == 1

    assert main(["--home", str(b), "inbox", "--limit", "1"]) == 0
    inbox = last_json(capsys)
    assert inbox[0]["qos"] == "control"
    assert main(["--home", str(b), "prekey-status"]) == 0
    prekeys = last_json(capsys)
    assert prekeys["policy"] == "require"
    assert prekeys["local"]["counts"]["consumed"] == 1

    assert (
        main(
            [
                "--home",
                str(b),
                "consumer-open",
                "workers",
                "--start",
                "earliest",
                "--kind-prefix",
                "agent.",
            ]
        )
        == 0
    )
    assert last_json(capsys)["created"] is True
    assert (
        main(
            [
                "--home",
                str(b),
                "consumer-claim",
                "workers",
                "--owner",
                "worker-a",
            ]
        )
        == 0
    )
    claim = last_json(capsys)[0]
    assert claim["packet_id"] == sent["queued"]
    assert (
        main(
            [
                "--home",
                str(b),
                "consumer-settle",
                "workers",
                claim["claim_token"],
                "--owner",
                "worker-a",
                "--action",
                "ack",
            ]
        )
        == 0
    )
    assert last_json(capsys)["state"] == "acked"
    assert main(["--home", str(b), "consumer-status", "workers"]) == 0
    assert last_json(capsys)["states"] == {"acked": 1}

    drop = tmp_path / "drop"
    assert (
        main(
            [
                "--home",
                str(a),
                "carrier-add",
                str(drop),
                "--name",
                "shared",
                "--peer",
                b_init["node_id"],
                "--interval",
                "0.2",
                "--jitter",
                "0.4",
                "--idle-backoff-max",
                "8",
            ]
        )
        == 0
    )
    added = last_json(capsys)
    assert added["added"]["name"] == "shared"
    assert added["added"]["jitter"] == 0.4
    assert added["added"]["idle_backoff_max"] == 8
    assert main(["--home", str(a), "carrier-list"]) == 0
    assert last_json(capsys)[0]["mode"] == "fallback"
    assert (
        main(
            [
                "--home",
                str(a),
                "carrier-add",
                "http://127.0.0.1:48888/dav",
                "--type",
                "webdav",
                "--name",
                "dav",
                "--peer",
                b_init["node_id"],
                "--allow-insecure-http",
                "--bearer-env",
                "ANET_TEST_DAV_TOKEN",
            ]
        )
        == 0
    )
    added_dav = last_json(capsys)
    assert added_dav["added"]["type"] == "webdav"
    assert main(["--home", str(a), "carrier-remove", "dav"]) == 0
    assert last_json(capsys)["removed"] == "dav"
    assert (
        main(
            [
                "--home",
                str(a),
                "routing-config",
                "--failure-threshold",
                "1",
                "--cooldown",
                "0",
                "--direct-retry-interval",
                "3",
                "--direct-race-width",
                "2",
                "--carrier-replica-count",
                "2",
                "--direct-race-delay",
                "0.25",
                "--direct-idle-probe-interval",
                "90",
                "--direct-probe-jitter",
                "0.4",
                "--direct-idle-backoff-max",
                "8",
                "--fallback-probe-jitter",
                "0.2",
                "--sync-interval",
                "3",
                "--sync-jitter",
                "0.3",
                "--no-listen",
            ]
        )
        == 0
    )
    routing = last_json(capsys)
    assert routing["listen_enabled"] is False
    assert routing["routing"]["direct_failure_threshold"] == 1
    assert routing["routing"]["direct_retry_interval"] == 3
    assert routing["routing"]["direct_race_width"] == 2
    assert routing["routing"]["carrier_replica_count"] == 2
    assert routing["routing"]["direct_race_delay"] == 0.25
    assert routing["routing"]["direct_idle_probe_interval"] == 90
    assert routing["routing"]["direct_probe_jitter"] == 0.4
    assert routing["sync_interval"] == 3
    assert routing["sync_jitter"] == 0.3

    assert main(["--home", str(a), "doctor"]) == 0
    doctor = last_json(capsys)
    assert doctor["ok"] is True
    assert doctor["node_id"] == a_init["node_id"]


def test_cli_direct_proxy_show_set_clear_never_prints_secret(tmp_path, capsys, monkeypatch) -> None:
    home = tmp_path / "proxy-node"
    assert main(["--home", str(home), "init", "--label", "proxy"]) == 0
    last_json(capsys)
    monkeypatch.setenv("ANET_PROXY_USER", "do-not-print-user")
    monkeypatch.setenv("ANET_PROXY_PASS", "do-not-print-password")
    assert main([
        "--home", str(home), "direct-proxy", "socks5h://127.0.0.1:1080",
        "--username-env", "ANET_PROXY_USER", "--password-env", "ANET_PROXY_PASS",
    ]) == 0
    raw = capsys.readouterr().out
    assert "do-not-print" not in raw
    configured = json.loads(raw)
    assert configured["restart_required"] is True
    assert configured["direct_proxy"]["username_env"] == "ANET_PROXY_USER"
    assert main(["--home", str(home), "direct-proxy"]) == 0
    assert last_json(capsys)["restart_required"] is False
    assert main(["--home", str(home), "direct-proxy", "--clear"]) == 0
    cleared = last_json(capsys)
    assert cleared == {"direct_proxy": None, "restart_required": True}


def test_cli_locator_config_updates_contexts_signed_card_and_config(
    tmp_path, capsys
) -> None:
    home = tmp_path / "locator-node"
    zone = "opaqueZone123"
    locator = f"tls://127.0.0.1:4242?scope=host&zone={zone}&priority=5"
    assert main([
        "--home", str(home), "init", "--label", "locator",
        "--locator-context", f"host:{zone}", "--advertise", locator,
    ]) == 0
    last_json(capsys)
    assert main(["--home", str(home), "locator-config"]) == 0
    shown = last_json(capsys)
    assert shown["locator_contexts"] == [f"host:{zone}"]
    assert shown["effective_addresses"] == [locator]
    assert shown["card_updated"] is False

    lan_zone = "opaqueLan456"
    lan_locator = (
        f"tls://192.0.2.10:4242?scope=lan&zone={lan_zone}&priority=20"
    )
    assert main([
        "--home", str(home), "locator-config",
        "--remove-context", f"host:{zone}",
        "--add-context", f"lan:{lan_zone}",
        "--advertise", lan_locator,
    ]) == 0
    changed = last_json(capsys)
    assert changed["locator_contexts"] == [f"lan:{lan_zone}"]
    assert changed["advertise"] == [lan_locator]
    assert changed["card_updated"] is True

    card = json.loads((home / "card.json").read_text(encoding="utf-8"))
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert card["addresses"] == [lan_locator]
    assert config["locator_contexts"] == [f"lan:{lan_zone}"]


def test_doctor_warns_about_cross_runtime_loopback_host_locator(
    tmp_path, capsys
) -> None:
    home = tmp_path / "loopback-host"
    zone = "opaqueZone123"
    locator = f"tls://127.0.0.1:4242?scope=host&zone={zone}&priority=0"
    assert main([
        "--home", str(home), "init", "--label", "loopback-host",
        "--locator-context", f"host:{zone}", "--advertise", locator,
    ]) == 0
    last_json(capsys)
    assert main(["--home", str(home), "doctor"]) == 0
    doctor = last_json(capsys)
    assert any("host-scoped loopback" in item for item in doctor["locators"]["warnings"])


def test_cli_materializes_raw_and_proxy_dialers_without_printing_secrets(
    tmp_path, capsys, monkeypatch
) -> None:
    home = tmp_path / "dialers"
    assert main(["--home", str(home), "init", "--label", "dialers"]) == 0
    last_json(capsys)
    assert main(["--home", str(home), "dialer-list"]) == 0
    initial = last_json(capsys)
    assert initial["source"] == "legacy-compatible"
    assert [item["name"] for item in initial["dialers"]] == ["raw"]

    monkeypatch.setenv("ANET_DIALER_USER", "never-print-user")
    monkeypatch.setenv("ANET_DIALER_PASS", "never-print-pass")
    assert main([
        "--home", str(home), "dialer-add", "mihomo", "--type", "socks5h",
        "--url", "socks5h://127.0.0.1:7890", "--priority", "20",
        "--username-env", "ANET_DIALER_USER",
        "--password-env", "ANET_DIALER_PASS",
    ]) == 0
    raw_output = capsys.readouterr().out
    assert "never-print" not in raw_output
    assert main(["--home", str(home), "dialer-list"]) == 0
    configured = last_json(capsys)
    assert configured["source"] == "explicit"
    assert [item["name"] for item in configured["dialers"]] == ["raw", "mihomo"]
    assert [item["type"] for item in configured["dialers"]] == ["raw", "socks5h"]

    assert main([
        "--home", str(home), "dialer-set", "raw", "--no-enabled",
        "--priority", "40",
    ]) == 0
    changed = last_json(capsys)
    assert changed["updated"]["enabled"] is False
    assert changed["updated"]["priority"] == 40
    assert main(["--home", str(home), "dialer-list"]) == 0
    after_change = last_json(capsys)
    assert [item["name"] for item in after_change["dialers"]] == ["mihomo"]

    assert main([
        "--home", str(home), "dialer-set", "raw", "--enabled",
    ]) == 0
    last_json(capsys)
    assert main(["--home", str(home), "dialer-remove", "mihomo"]) == 0
    removed = last_json(capsys)
    assert [item["name"] for item in removed["effective_dialers"]] == ["raw"]


def test_cli_dialer_set_materializes_legacy_raw(tmp_path, capsys) -> None:
    home = tmp_path / "dialer-set"
    assert main(["--home", str(home), "init", "--label", "dialer-set"]) == 0
    last_json(capsys)
    assert main([
        "--home", str(home), "dialer-set", "raw", "--priority", "7",
    ]) == 0
    changed = last_json(capsys)
    assert changed["updated"]["priority"] == 7
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["direct_proxy"] is None
    assert config["direct_dialers"][0]["name"] == "raw"


def test_ahub_operator_cli_keeps_root_separate_and_disables_safely(
    tmp_path, capsys
) -> None:
    root = tmp_path / "ahub"
    node = Identity.generate("allowed")

    assert main(
        ["ahub-allow", "--root", str(root), node.node_id]
    ) == 0
    allowed = last_json(capsys)
    assert allowed["allowed"] == node.node_id
    assert allowed["changed"] is True
    assert allowed["status"]["enabled_nodes"] == 1

    assert main(["ahub-nodes", "--root", str(root)]) == 0
    listed = last_json(capsys)
    assert [item["node_id"] for item in listed["nodes"]] == [node.node_id]

    assert main(
        [
            "ahub-disallow",
            "--root",
            str(root),
            node.node_id,
            "--confirm",
            "wrong",
        ]
    ) == 1
    capsys.readouterr()
    assert main(
        [
            "ahub-disallow",
            "--root",
            str(root),
            node.node_id,
            "--confirm",
            node.node_id,
        ]
    ) == 0
    disabled = last_json(capsys)
    assert disabled["changed"] is True
    assert disabled["pending_mailbox_retained_until_expiry"] is True

    assert main(
        [
            "ahub-nodes",
            "--root",
            str(root),
            "--include-disabled",
        ]
    ) == 0
    listed = last_json(capsys)
    assert listed["nodes"][0]["enabled"] is False

    assert main(["ahub-status", "--root", str(root)]) == 0
    status = last_json(capsys)
    assert status["ok"] is True
    assert status["disabled_nodes"] == 1
    assert "mailbox_bytes" in status
    assert main(["ahub-checkpoint", "--root", str(root)]) == 0
    checkpoint = last_json(capsys)
    assert checkpoint["backup_ready_if_service_stopped"] is True
    assert checkpoint["checkpoint"]["ahub"]["busy"] == 0
    assert checkpoint["checkpoint"]["control"]["busy"] == 0


def test_ahub_cli_refuses_node_home_and_unsafe_bind(
    tmp_path, capsys, monkeypatch
) -> None:
    node_home = tmp_path / "node"
    assert main(
        ["--home", str(node_home), "init", "--label", "node"]
    ) == 0
    last_json(capsys)
    assert main(["ahub-status", "--root", str(node_home)]) == 1
    error = capsys.readouterr().err
    assert "must not be a node home" in error
    assert not (node_home / "ahub.sqlite3").exists()

    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("uvicorn.run", fake_run)
    root = tmp_path / "ahub"
    assert main(
        [
            "ahub-serve",
            "--root",
            str(root),
            "--host",
            "0.0.0.0",
        ]
    ) == 1
    assert "requires --allow-non-loopback" in capsys.readouterr().err
    assert called is False


def test_ahub_serve_uses_bounded_single_worker_without_access_log(
    tmp_path, capsys, monkeypatch
) -> None:
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    root = tmp_path / "ahub"
    assert main(
        [
            "--verbose",
            "ahub-serve",
            "--root",
            str(root),
            "--port",
            "18422",
            "--limit-concurrency",
            "17",
            "--keep-alive-seconds",
            "3",
        ]
    ) == 0
    capsys.readouterr()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18422
    assert captured["workers"] == 1
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
    assert captured["server_header"] is False
    assert captured["limit_concurrency"] == 17
    assert captured["timeout_keep_alive"] == 3


def test_cli_persists_identity_authenticated_ahub_carrier(
    tmp_path, capsys
) -> None:
    a_home = tmp_path / "a"
    b_home = tmp_path / "b"
    b_card = tmp_path / "b.card.json"
    assert main(
        ["--home", str(a_home), "init", "--label", "a"]
    ) == 0
    last_json(capsys)
    assert main(
        ["--home", str(b_home), "init", "--label", "b"]
    ) == 0
    b_init = last_json(capsys)
    assert main(
        ["--home", str(b_home), "card", "--out", str(b_card)]
    ) == 0
    last_json(capsys)
    assert main(
        ["--home", str(a_home), "peer-add", str(b_card)]
    ) == 0
    last_json(capsys)

    assert main(
        [
            "--home",
            str(a_home),
            "carrier-add",
            "http://127.0.0.1:8422",
            "--type",
            "ahub",
            "--name",
            "public",
            "--peer",
            b_init["node_id"],
            "--allow-insecure-http",
            "--claim-lease-seconds",
            "17",
        ]
    ) == 0
    added = last_json(capsys)["added"]
    assert added["type"] == "ahub"
    assert added["priority"] == 50
    assert added["claim_lease_seconds"] == 17
    assert "bearer" not in added

    loaded = NodeConfig.load(a_home)
    assert len(loaded.ahub_carriers) == 1
    assert loaded.ahub_carriers[0].base_url == "http://127.0.0.1:8422"
    assert loaded.ahub_carriers[0].peers == (b_init["node_id"],)
    assert main(["--home", str(a_home), "carrier-list"]) == 0
    assert last_json(capsys)[0]["type"] == "ahub"
    assert main(
        ["--home", str(a_home), "carrier-remove", "public"]
    ) == 0
    last_json(capsys)
    assert NodeConfig.load(a_home).ahub_carriers == ()


def test_cli_queries_pinned_peer_reachability_without_editing_peerbook(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    local_home = tmp_path / "local"
    peer_home = tmp_path / "peer"
    assert main(
        [
            "--home",
            str(local_home),
            "init",
            "--label",
            "local",
            "--port",
            "44111",
        ]
    ) == 0
    local_init = last_json(capsys)
    assert main(
        [
            "--home",
            str(peer_home),
            "init",
            "--label",
            "peer",
            "--port",
            "44112",
        ]
    ) == 0
    peer_init = last_json(capsys)
    peer_card = tmp_path / "peer.card.json"
    assert main(
        [
            "--home",
            str(peer_home),
            "card",
            "--out",
            str(peer_card),
        ]
    ) == 0
    last_json(capsys)
    assert main(
        [
            "--home",
            str(local_home),
            "peer-add",
            str(peer_card),
        ]
    ) == 0
    last_json(capsys)
    peers_path = NodeConfig.load(local_home).peers_path
    peers_before = peers_path.read_text(encoding="utf-8")
    assert main(
        [
            "--home",
            str(local_home),
            "carrier-add",
            "https://ahub.example",
            "--type",
            "ahub",
            "--name",
            "control",
            "--peer",
            peer_init["node_id"],
        ]
    ) == 0
    last_json(capsys)

    peer_node = AnetNode(NodeConfig.load(peer_home))
    try:
        descriptor = current_node_descriptor(peer_node)
        record = current_node_reachability(peer_node, descriptor)
    finally:
        peer_node.close()

    monkeypatch.setattr(
        AhubHTTPClient,
        "lookup",
        lambda self, node_id: (descriptor, record),
    )
    assert main(
        [
            "--home",
            str(local_home),
            "peer-reachability",
            peer_init["node_id"],
        ]
    ) == 0
    result = last_json(capsys)
    assert result["ok"] is True
    assert result["peer_id"] == peer_init["node_id"]
    assert result["sources"][0]["candidates"] == list(record.candidates)
    assert result["effective_candidates"] == list(record.candidates)
    assert peers_path.read_text(encoding="utf-8") == peers_before
    assert local_init["node_id"] != peer_init["node_id"]
