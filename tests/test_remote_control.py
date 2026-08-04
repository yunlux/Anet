from __future__ import annotations

import json
from pathlib import Path

import pytest

from anet.config import NodeConfig, initialize_node
from anet.encoding import b64e
from anet.identity import Identity
from anet.peers import PeerBook
import anet.remote_control as remote_control
from anet.remote_control import (
    RemoteControlError,
    SupervisorLock,
    sign_control_page,
    sync_remote_control,
    verify_remote_control,
    write_control_settings,
)


def _write_page(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.as_uri()


def _signed_page(
    path: Path,
    payload: dict,
    publisher: Identity,
    *,
    sequence: int = 1,
    key_id: str = "community-main",
) -> str:
    now = remote_control._now_ms()
    value = sign_control_page(
        {**payload, "sequence": sequence},
        publisher,
        key_id=key_id,
        issued_ms=now - 1000,
        expires_ms=now + 3600_000,
    )
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.as_uri()


def _trusted_publisher(publisher: Identity) -> dict[str, str]:
    return {"community-main": b64e(publisher.sign_public)}


def _trusted_publishers(**publishers: Identity) -> dict[str, str]:
    return {
        key_id.replace("_", "-"): b64e(identity.sign_public)
        for key_id, identity in publishers.items()
    }


def test_control_page_applies_config_and_peer_cards(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43101,
    )
    remote = initialize_node(
        tmp_path / "remote",
        label="remote",
        listen_host="127.0.0.1",
        listen_port=43102,
    )
    remote_card = Identity.load(remote.identity_path).card(
        addresses=remote.effective_addresses(),
        capabilities=remote.capabilities,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "version": 1,
            "sequence": 1,
            "network": "test-network",
            "config": {"sync_interval": 0.5, "sync_jitter": 0.1},
            "nodes": [remote_card.to_dict()],
        },
    )

    result = sync_remote_control(local.home, url=page, apply_software=False)

    assert result["ok"] is True
    assert result["config_changed"] is True
    assert result["nodes_added"] == 1
    assert result["restart_required"] is True
    assert result["network"] == "test-network"
    assert NodeConfig.load(local.home).sync_interval == 0.5
    peers = PeerBook(local.peers_path, own_node_id=Identity.load(local.identity_path).node_id)
    assert len(peers.all()) == 1
    assert (local.home / "remote-control-state.json").exists()


def test_signed_control_page_requires_local_key_and_records_expiry(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43102,
    )
    publisher = Identity.generate("community-publisher")
    page = _signed_page(
        tmp_path / "control.json",
        {"config": {"sync_interval": 0.5}},
        publisher,
    )

    with pytest.raises(
        RemoteControlError,
        match="no local trusted key",
    ):
        sync_remote_control(local.home, url=page, apply_software=False)

    write_control_settings(
        local.home,
        url=page,
        trusted_keys=_trusted_publisher(publisher),
    )
    result = sync_remote_control(local.home, apply_software=False)

    assert result["control_signed"] is True
    assert result["control_key_id"] == "community-main"
    assert result["control_expires_ms"] > remote_control._now_ms()
    state = json.loads(
        (local.home / "remote-control-state.json").read_text(encoding="utf-8")
    )
    assert state["control_key_id"] == "community-main"
    assert NodeConfig.load(local.home).sync_interval == 0.5


def test_control_verify_is_read_only_before_initial_supervisor_sync(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43102,
    )
    publisher = Identity.generate("community-publisher")
    page = _signed_page(
        tmp_path / "control.json",
        {
            "config": {"sync_interval": 0.5},
            "software": {
                "version": "0.12.1",
                "wheel_url": "https://example.invalid/anet.whl",
            },
        },
        publisher,
    )
    write_control_settings(
        local.home,
        url=page,
        trusted_keys=_trusted_publisher(publisher),
    )
    config_before = (local.home / "config.json").read_bytes()

    result = verify_remote_control(local.home)

    assert result["ok"] is True
    assert result["control_signed"] is True
    assert result["software_present"] is True
    assert (local.home / "config.json").read_bytes() == config_before
    assert not (local.home / "remote-control-state.json").exists()


def test_control_verify_rejects_an_invalid_peer_card(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43102,
    )
    publisher = Identity.generate("community-publisher")
    page_path = tmp_path / "control.json"
    page = sign_control_page(
        {
            "sequence": 1,
            "nodes": [{"node_id": "not-a-peer-card"}],
        },
        publisher,
        key_id="community-main",
        issued_ms=remote_control._now_ms() - 1000,
        expires_ms=remote_control._now_ms() + 3600_000,
    )
    page_path.write_text(json.dumps(page), encoding="utf-8")
    write_control_settings(
        local.home,
        url=page_path.as_uri(),
        trusted_keys=_trusted_publisher(publisher),
    )

    with pytest.raises(RemoteControlError, match="invalid Peer Card"):
        verify_remote_control(local.home)


def test_signed_control_page_rejects_tampering_and_unsigned_nested_page(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    publisher = Identity.generate("community-publisher")
    child = tmp_path / "child.json"
    child_url = _write_page(child, {"sequence": 2, "config": {"max_batch": 64}})
    root = tmp_path / "root.json"
    root_url = _signed_page(root, {"pages": [child_url]}, publisher)
    write_control_settings(
        local.home,
        url=root_url,
        trusted_keys=_trusted_publisher(publisher),
    )

    with pytest.raises(RemoteControlError, match="signed control page required"):
        sync_remote_control(local.home, apply_software=False)

    signed_child = sign_control_page(
        {"sequence": 2, "config": {"max_batch": 64}},
        publisher,
        key_id="community-main",
        issued_ms=remote_control._now_ms() - 1000,
        expires_ms=remote_control._now_ms() + 3600_000,
    )
    child.write_text(json.dumps(signed_child), encoding="utf-8")
    result = sync_remote_control(local.home, apply_software=False)
    assert result["source_publishers"] == [
        {"url": root_url, "signed": True, "key_id": "community-main"},
        {"url": child_url, "signed": True, "key_id": "community-main"},
    ]

    tampered = json.loads(root.read_text(encoding="utf-8"))
    tampered["config"] = {"sync_interval": 99.0}
    root.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RemoteControlError, match="signature verification failed"):
        sync_remote_control(local.home, apply_software=False)


def test_nested_source_pins_a_distinct_community_publisher(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    root_publisher = Identity.generate("root-publisher")
    actor_publisher = Identity.generate("actor-publisher")
    child = tmp_path / "actor-page.json"
    child_url = _signed_page(
        child,
        {"sequence": 2, "config": {"max_batch": 64}},
        actor_publisher,
        key_id="actor-a",
    )
    root = tmp_path / "root.json"
    root_url = _signed_page(
        root,
        {"sequence": 1, "pages": [{"url": child_url, "key_id": "actor-a"}]},
        root_publisher,
        key_id="root",
    )
    trusted = _trusted_publishers(root=root_publisher, actor_a=actor_publisher)
    write_control_settings(local.home, url=root_url, trusted_keys=trusted)

    verified = verify_remote_control(local.home)
    result = sync_remote_control(local.home, apply_software=False)
    unchanged = sync_remote_control(local.home, apply_software=False)

    expected = [
        {"url": root_url, "signed": True, "key_id": "root"},
        {"url": child_url, "signed": True, "key_id": "actor-a"},
    ]
    assert verified["source_publishers"] == expected
    assert result["source_publishers"] == expected
    assert unchanged["changed"] is False
    assert unchanged["source_publishers"] == expected
    state = json.loads(
        (local.home / remote_control.CONTROL_STATE_NAME).read_text(encoding="utf-8")
    )
    assert state["source_publishers"] == expected
    assert NodeConfig.load(local.home).max_batch == 64


def test_source_attribution_preserves_legacy_signed_state_digest(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    publisher = Identity.generate("publisher")
    page = tmp_path / "control.json"
    page_url = _signed_page(
        page,
        {"config": {"max_batch": 64}},
        publisher,
        key_id="publisher",
    )
    trusted = _trusted_publishers(publisher=publisher)
    write_control_settings(local.home, url=page_url, trusted_keys=trusted)
    raw = json.loads(page.read_text(encoding="utf-8"))
    document = remote_control._normalise_document(
        raw,
        source_url=page_url,
        visited=set(),
        depth=0,
        sources=[],
        trusted_keys=trusted,
        now_ms=remote_control._now_ms(),
    )
    legacy_document = dict(document)
    legacy_document.pop("source_publishers")
    legacy_document.pop("source_pins")
    legacy_digest = remote_control._json_digest(legacy_document)
    (local.home / remote_control.CONTROL_STATE_NAME).write_text(
        json.dumps({"sequence": 1, "digest": legacy_digest}),
        encoding="utf-8",
    )

    result = sync_remote_control(local.home, apply_software=False)

    assert result["changed"] is False
    assert result["digest"] == legacy_digest
    assert result["source_publishers"] == [
        {"url": page_url, "signed": True, "key_id": "publisher"}
    ]


def test_adding_source_pin_requires_a_new_signed_sequence(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    publisher = Identity.generate("publisher")
    child_url = _signed_page(
        tmp_path / "child.json",
        {"sequence": 2, "config": {"max_batch": 64}},
        publisher,
        key_id="publisher",
    )
    root = tmp_path / "root.json"
    root_url = _signed_page(
        root,
        {"pages": [child_url]},
        publisher,
        sequence=1,
        key_id="publisher",
    )
    write_control_settings(
        local.home,
        url=root_url,
        trusted_keys=_trusted_publishers(publisher=publisher),
    )
    sync_remote_control(local.home, apply_software=False)
    _signed_page(
        root,
        {"pages": [{"url": child_url, "key_id": "publisher"}]},
        publisher,
        sequence=1,
        key_id="publisher",
    )

    with pytest.raises(RemoteControlError, match="reused a sequence"):
        sync_remote_control(local.home, apply_software=False)


def test_nested_source_rejects_another_trusted_publishers_signature(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    root_publisher = Identity.generate("root-publisher")
    actor_a = Identity.generate("actor-a")
    actor_b = Identity.generate("actor-b")
    child = tmp_path / "actor-page.json"
    child_url = _signed_page(
        child,
        {"sequence": 2, "config": {}},
        actor_b,
        key_id="actor-b",
    )
    root_url = _signed_page(
        tmp_path / "root.json",
        {"pages": [{"url": child_url, "key_id": "actor-a"}]},
        root_publisher,
        key_id="root",
    )
    trusted = _trusted_publishers(
        root=root_publisher,
        actor_a=actor_a,
        actor_b=actor_b,
    )
    write_control_settings(local.home, url=root_url, trusted_keys=trusted)

    with pytest.raises(RemoteControlError, match="publisher mismatch"):
        verify_remote_control(local.home)


def test_nested_source_pin_must_exist_in_local_trust_policy(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    root_publisher = Identity.generate("root-publisher")
    root_url = _signed_page(
        tmp_path / "root.json",
        {
            "pages": [
                {
                    "url": "https://unreachable.invalid/actor.json",
                    "key_id": "actor-a",
                }
            ]
        },
        root_publisher,
        key_id="root",
    )
    write_control_settings(
        local.home,
        url=root_url,
        trusted_keys=_trusted_publishers(root=root_publisher),
    )

    with pytest.raises(RemoteControlError, match="not locally trusted: actor-a"):
        verify_remote_control(local.home)


def test_duplicate_nested_source_cannot_claim_conflicting_publishers(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    root_publisher = Identity.generate("root-publisher")
    actor_a = Identity.generate("actor-a")
    actor_b = Identity.generate("actor-b")
    child_url = _signed_page(
        tmp_path / "actor-page.json",
        {"sequence": 2, "config": {}},
        actor_a,
        key_id="actor-a",
    )
    root_url = _signed_page(
        tmp_path / "root.json",
        {
            "pages": [
                {"url": child_url, "key_id": "actor-a"},
                {"url": child_url, "key_id": "actor-b"},
            ]
        },
        root_publisher,
        key_id="root",
    )
    write_control_settings(
        local.home,
        url=root_url,
        trusted_keys=_trusted_publishers(
            root=root_publisher,
            actor_a=actor_a,
            actor_b=actor_b,
        ),
    )

    with pytest.raises(RemoteControlError, match="publisher mismatch"):
        sync_remote_control(local.home, apply_software=False)


def test_stale_control_page_reports_verified_source_publishers(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    publisher = Identity.generate("publisher")
    page = tmp_path / "control.json"
    page_url = _signed_page(
        page,
        {"config": {"max_batch": 64}},
        publisher,
        sequence=2,
        key_id="publisher",
    )
    write_control_settings(
        local.home,
        url=page_url,
        trusted_keys=_trusted_publishers(publisher=publisher),
    )
    sync_remote_control(local.home, apply_software=False)
    _signed_page(
        page,
        {"config": {"max_batch": 32}},
        publisher,
        sequence=1,
        key_id="publisher",
    )

    stale = sync_remote_control(local.home, apply_software=False)

    assert stale["stale"] is True
    assert stale["current_sequence"] == 2
    assert stale["source_publishers"] == [
        {"url": page_url, "signed": True, "key_id": "publisher"}
    ]


def test_nested_source_rejects_unsupported_policy_fields(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    publisher = Identity.generate("publisher")
    root_url = _signed_page(
        tmp_path / "root.json",
        {
            "pages": [
                {
                    "url": "https://unreachable.invalid/actor.json",
                    "key_id": "publisher",
                    "reputation": 100,
                }
            ]
        },
        publisher,
        key_id="publisher",
    )
    write_control_settings(
        local.home,
        url=root_url,
        trusted_keys=_trusted_publishers(publisher=publisher),
    )

    with pytest.raises(RemoteControlError, match="unsupported fields"):
        verify_remote_control(local.home)


def test_control_source_pin_contract_is_published() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = (root / "docs" / "CONTROL_SOURCE_PINS_V1.md").read_text(
        encoding="utf-8"
    )
    assert '"key_id": "actor-a"' in contract
    assert "source_publishers" in contract
    assert "reputation score" in contract
    for path in (
        root / "README.md",
        root / "README.zh-CN.md",
        root / "docs" / "CLI_AGENT_GUIDE.md",
        root / "docs" / "HERMES_SKILL_INSTALL.md",
        root / "docs" / "WINDOWS_AUTOSTART.md",
        root / "docs" / "POSIX_AUTOSTART.md",
        root / "docs" / "TERMUX_AUTOSTART.md",
    ):
        assert "CONTROL_SOURCE_PINS_V1.md" in path.read_text(encoding="utf-8")


def test_signed_control_page_rejects_same_sequence_with_new_content(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43104,
    )
    publisher = Identity.generate("community-publisher")
    page_path = tmp_path / "control.json"
    page = _signed_page(
        page_path,
        {"config": {"sync_interval": 0.5}},
        publisher,
    )
    write_control_settings(
        local.home,
        url=page,
        trusted_keys=_trusted_publisher(publisher),
    )
    sync_remote_control(local.home, apply_software=False)

    _signed_page(
        page_path,
        {"config": {"sync_interval": 0.7}},
        publisher,
        sequence=1,
    )
    with pytest.raises(RemoteControlError, match="reused a sequence"):
        sync_remote_control(local.home, apply_software=False)


def test_signed_control_page_rejects_expired_publisher_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43105,
    )
    publisher = Identity.generate("community-publisher")
    now = remote_control._now_ms()
    issued = now - 2 * 24 * 60 * 60 * 1000
    expires = now - 24 * 60 * 60 * 1000
    with monkeypatch.context() as context:
        context.setattr(remote_control, "_now_ms", lambda: issued)
        signed = sign_control_page(
            {"sequence": 1, "config": {"sync_interval": 0.5}},
            publisher,
            key_id="community-main",
            issued_ms=issued,
            expires_ms=expires,
        )
    page = tmp_path / "expired.json"
    page.write_text(json.dumps(signed), encoding="utf-8")
    write_control_settings(
        local.home,
        url=page.as_uri(),
        trusted_keys=_trusted_publisher(publisher),
    )

    with pytest.raises(RemoteControlError, match="has expired"):
        sync_remote_control(local.home, apply_software=False)


def test_invalid_peer_card_does_not_partially_apply_the_control_page(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    remote = initialize_node(
        tmp_path / "remote",
        label="remote",
        listen_host="127.0.0.1",
        listen_port=43104,
    )
    remote_card = Identity.load(remote.identity_path).card(
        addresses=remote.effective_addresses(),
        capabilities=remote.capabilities,
    )
    Identity.load(local.identity_path).card(
        addresses=local.effective_addresses(),
        capabilities=local.capabilities,
    ).save(local.home / "card.json")
    config_before = (local.home / "config.json").read_bytes()
    card_before = (local.home / "card.json").read_bytes()
    peers_before = (local.home / "peers.json").read_bytes()
    page = _write_page(
        tmp_path / "control.json",
        {
            "version": 1,
            "sequence": 1,
            "config": {"listen_port": 43107, "sync_interval": 0.5},
            "nodes": [
                remote_card.to_dict(),
                {"node_id": "malformed-card"},
            ],
        },
    )

    with pytest.raises(RemoteControlError, match="invalid Peer Card"):
        sync_remote_control(local.home, url=page, apply_software=False)

    peers = PeerBook(
        local.peers_path,
        own_node_id=Identity.load(local.identity_path).node_id,
    )
    assert peers.all() == []
    assert (local.home / "config.json").read_bytes() == config_before
    assert (local.home / "card.json").read_bytes() == card_before
    assert (local.home / "peers.json").read_bytes() == peers_before
    assert not (local.home / "remote-control-state.json").exists()


def test_software_failure_rolls_back_config_and_peers(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43105,
    )
    remote = initialize_node(
        tmp_path / "remote",
        label="remote",
        listen_host="127.0.0.1",
        listen_port=43106,
    )
    remote_card = Identity.load(remote.identity_path).card(
        addresses=remote.effective_addresses(),
        capabilities=remote.capabilities,
    )
    Identity.load(local.identity_path).card(
        addresses=local.effective_addresses(),
        capabilities=local.capabilities,
    ).save(local.home / "card.json")
    config_before = (local.home / "config.json").read_bytes()
    card_before = (local.home / "card.json").read_bytes()
    peers_before = (local.home / "peers.json").read_bytes()
    page = _write_page(
        tmp_path / "control.json",
        {
            "version": 1,
            "sequence": 1,
            "config": {"listen_port": 43108, "sync_interval": 0.5},
            "nodes": [remote_card.to_dict()],
            "software": {
                "version": "0.12.2",
                "wheel_url": "https://example.invalid/update.whl",
            },
        },
    )

    def fail_software(
        home: Path,
        software: dict,
        state: dict,
        *,
        require_wheel_hash: bool = False,
    ) -> bool:
        del home, software, state, require_wheel_hash
        raise RuntimeError("software update failed")

    monkeypatch.setattr(remote_control, "_install_software", fail_software)
    with pytest.raises(RuntimeError, match="software update failed"):
        sync_remote_control(local.home, url=page)

    assert (local.home / "config.json").read_bytes() == config_before
    assert (local.home / "card.json").read_bytes() == card_before
    assert (local.home / "peers.json").read_bytes() == peers_before
    assert not (local.home / "remote-control-state.json").exists()


def test_signed_wheel_update_requires_sha256_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote_control,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("unsigned wheel must not download"),
    )

    with pytest.raises(RemoteControlError, match="requires software.sha256"):
        remote_control._install_software(
            tmp_path,
            {
                "version": "0.12.2",
                "wheel_url": "https://example.invalid/update.whl",
            },
            {"software_key": "previous-page"},
            require_wheel_hash=True,
        )


def test_platform_overlay_is_applied_only_to_matching_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43106,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "config": {"sync_interval": 1.0},
            "platforms": {
                "windows": {"config": {"sync_interval": 2.0}},
                "wsl": {
                    "config": {
                        "sync_interval": 0.7,
                        "listen_port": 43107,
                    }
                },
            },
        },
    )

    monkeypatch.setattr(remote_control, "runtime_platform", lambda: "wsl")
    result = sync_remote_control(local.home, url=page, apply_software=False)

    assert result["config_changed"] is True
    updated = NodeConfig.load(local.home)
    assert updated.sync_interval == 0.7
    assert updated.listen_port == 43107


def test_nested_pages_are_merged_and_replayed_idempotently(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43103,
    )
    child = tmp_path / "child.json"
    child_url = _write_page(
        child,
        {
            "sequence": 2,
            "config": {"max_batch": 64},
        },
    )
    root_url = _write_page(
        tmp_path / "root.json",
        {
            "sequence": 1,
            "config": {"sync_interval": 1.0},
            "pages": [child_url],
            "kv": [child_url],
        },
    )

    first = sync_remote_control(local.home, url=root_url, apply_software=False)
    second = sync_remote_control(local.home, url=root_url, apply_software=False)

    assert first["changed"] is True
    assert first["sequence"] == 2
    assert second["changed"] is False
    assert second["digest"] == first["digest"]
    updated = NodeConfig.load(local.home)
    assert updated.sync_interval == 1.0
    assert updated.max_batch == 64


def test_nested_local_relative_cards_are_resolved_and_updated(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43104,
    )
    remote = initialize_node(
        tmp_path / "remote",
        label="remote",
        listen_host="127.0.0.1",
        listen_port=43105,
    )
    remote_identity = Identity.load(remote.identity_path)
    card_path = tmp_path / "network" / "remote.card.json"
    card_path.parent.mkdir()

    def write_card(*, addresses: tuple[str, ...]) -> None:
        card = remote_identity.card(addresses=addresses, capabilities=())
        card_path.write_text(json.dumps(card.to_dict()), encoding="utf-8")

    write_card(addresses=remote.effective_addresses())
    child = card_path.parent / "community.json"
    child.write_text(
        json.dumps({"nodes": [{"card_url": card_path.name}]}),
        encoding="utf-8",
    )
    root = _write_page(
        tmp_path / "control.json",
        {"pages": ["network/community.json"]},
    )

    first = sync_remote_control(local.home, url=root, apply_software=False)
    assert first["nodes_added"] == 1
    assert first["nodes_updated"] == 0

    write_card(addresses=())
    second = sync_remote_control(local.home, url=root, apply_software=False)
    assert second["nodes_added"] == 0
    assert second["nodes_updated"] == 1
    assert second["restart_required"] is True


def test_network_updates_regenerate_the_signed_local_card(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43108,
    )
    identity = Identity.load(local.identity_path)
    identity.card(
        addresses=local.effective_addresses(),
        capabilities=local.capabilities,
    ).save(local.home / "card.json")
    original = json.loads((local.home / "card.json").read_text(encoding="utf-8"))
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "config": {
                "listen_host": "192.0.2.8",
                "listen_port": 43118,
                "locator_contexts": ["host:abcdefgh"],
                "advertise": [
                    "tls://192.0.2.8:43118?scope=host&zone=abcdefgh&priority=0"
                ],
            },
        },
    )

    result = sync_remote_control(local.home, url=page, apply_software=False)

    assert result["config_changed"] is True
    updated = json.loads((local.home / "card.json").read_text(encoding="utf-8"))
    assert updated["node_id"] == original["node_id"]
    assert updated["addresses"] == [
        "tls://192.0.2.8:43118?scope=host&zone=abcdefgh&priority=0"
    ]
    assert updated["signature"] != original["signature"]
    assert NodeConfig.load(local.home).listen_port == 43118


def test_cross_platform_control_pages_reject_equal_listener_ports(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43109,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "default_config": {"listen_host": "0.0.0.0"},
            "platforms": {
                "windows": {
                    "config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43119,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43119?scope=host&zone=abcdefgh"
                        ],
                    }
                },
                "wsl": {
                    "config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43119,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43119?scope=host&zone=abcdefgh"
                        ],
                    }
                },
            },
        },
    )
    monkeypatch.setattr(remote_control, "runtime_platform", lambda: "wsl")

    with pytest.raises(
        RemoteControlError,
        match="must use distinct listener ports",
    ):
        sync_remote_control(local.home, url=page, apply_software=False)


def test_cross_platform_port_validation_reads_default_config_overlays(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43109,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "platforms": {
                "windows": {
                    "default_config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43119,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43119?scope=host&zone=abcdefgh"
                        ],
                    }
                },
                "wsl": {
                    "default_config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43119,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43119?scope=host&zone=abcdefgh"
                        ],
                    }
                },
            },
        },
    )
    monkeypatch.setattr(remote_control, "runtime_platform", lambda: "wsl")

    with pytest.raises(
        RemoteControlError,
        match="must use distinct listener ports",
    ):
        sync_remote_control(local.home, url=page, apply_software=False)


def test_cross_platform_control_pages_reject_mixed_host_scope(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43111,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "platforms": {
                "windows": {
                    "config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43121,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43121?scope=host&zone=abcdefgh"
                        ],
                    }
                },
                "wsl": {
                    "config": {
                        "listen_host": "127.0.0.1",
                        "listen_port": 43122,
                    }
                },
            },
        },
    )
    monkeypatch.setattr(remote_control, "runtime_platform", lambda: "wsl")

    with pytest.raises(
        RemoteControlError,
        match="host scope must be declared on both",
    ):
        sync_remote_control(local.home, url=page, apply_software=False)


def test_cross_platform_control_pages_reject_loopback_host_locator(
    tmp_path: Path, monkeypatch
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43110,
    )
    page = _write_page(
        tmp_path / "control.json",
        {
            "sequence": 1,
            "platforms": {
                "windows": {
                    "config": {
                        "listen_host": "127.0.0.1",
                        "listen_port": 43120,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://127.0.0.1:43120?scope=host&zone=abcdefgh"
                        ],
                    }
                },
                "wsl": {
                    "config": {
                        "listen_host": "0.0.0.0",
                        "listen_port": 43121,
                        "locator_contexts": ["host:abcdefgh"],
                        "advertise": [
                            "tls://192.0.2.8:43121?scope=host&zone=abcdefgh"
                        ],
                    }
                },
            },
        },
    )
    monkeypatch.setattr(remote_control, "runtime_platform", lambda: "windows")

    with pytest.raises(RemoteControlError, match="must not listen on loopback"):
        sync_remote_control(local.home, url=page, apply_software=False)


def test_local_control_interval_is_used_when_page_omits_poll_seconds(
    tmp_path: Path,
) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43130,
    )
    page = _write_page(
        tmp_path / "control.json",
        {"sequence": 1, "config": {"sync_interval": 1.0}},
    )
    write_control_settings(local.home, url=page, interval=17)

    result = sync_remote_control(local.home, apply_software=False)

    assert result["poll_seconds"] == 17.0


def test_page_poll_seconds_override_local_control_interval(tmp_path: Path) -> None:
    local = initialize_node(
        tmp_path / "local",
        label="local",
        listen_host="127.0.0.1",
        listen_port=43131,
    )
    page = _write_page(
        tmp_path / "control.json",
        {"sequence": 1, "poll_seconds": 23, "config": {}},
    )
    write_control_settings(local.home, url=page, interval=17)

    result = sync_remote_control(local.home, apply_software=False)

    assert result["poll_seconds"] == 23.0


def test_supervisor_lock_prevents_two_control_clients_for_one_home(
    tmp_path: Path,
) -> None:
    node_home = tmp_path / "node"
    node_home.mkdir()
    first = SupervisorLock(node_home)
    second = SupervisorLock(node_home)
    first.acquire()
    try:
        with pytest.raises(RemoteControlError, match="already owns node home"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_one_shot_control_sync_respects_supervisor_lock(tmp_path: Path) -> None:
    node_home = tmp_path / "node"
    node_home.mkdir()
    owner = SupervisorLock(node_home)
    owner.acquire()
    try:
        with pytest.raises(RemoteControlError, match="already owns node home"):
            sync_remote_control(
                node_home,
                url=(tmp_path / "control.json").as_uri(),
                apply_software=False,
            )
    finally:
        owner.release()


@pytest.mark.asyncio
async def test_supervisor_wait_observes_child_exit_before_long_poll_interval() -> None:
    class FinishedProcess:
        returncode = 17

        async def wait(self) -> int:
            return self.returncode

    result = await remote_control._wait_for_child_or_interval(
        FinishedProcess(),
        86400,
    )

    assert result == 17


def test_same_version_software_change_is_not_silently_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    software = {
        "version": "0.12.1",
        "wheel_url": "https://example.invalid/new.whl",
    }
    monkeypatch.setattr(remote_control, "_current_version", lambda: "0.12.1")
    calls: list[list[str]] = []

    def fake_download(url: str, destination: Path, *, timeout: float) -> None:
        del url, timeout
        destination.write_bytes(b"new-wheel")

    monkeypatch.setattr(remote_control, "_download", fake_download)
    monkeypatch.setattr(
        remote_control.subprocess,
        "run",
        lambda command, check: calls.append(list(command)),
    )
    state = {"software_key": "previous-page"}

    assert remote_control._install_software(tmp_path, software, state) is True
    assert calls
    assert state["software_key"] != "previous-page"


def test_repository_software_update_uses_the_declared_ref(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        remote_control.subprocess,
        "run",
        lambda command, check: calls.append(list(command)),
    )
    state: dict[str, object] = {}

    assert remote_control._install_software(
        tmp_path,
        {
            "repo_url": "https://github.com/yunlux/Anet",
            "repo_ref": "v0.12.1",
        },
        state,
    ) is True

    assert calls
    assert "git+https://github.com/yunlux/Anet@v0.12.1" in calls[0]
    assert state["software_key"]


def test_empty_wheel_url_falls_back_to_repository_source(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        remote_control.subprocess,
        "run",
        lambda command, check: calls.append(list(command)),
    )

    assert remote_control._install_software(
        tmp_path,
        {
            "wheel_url": "",
            "repo_url": "https://github.com/yunlux/Anet",
            "repo_ref": "v0.12.1",
        },
        {},
    ) is True

    assert calls
    assert "git+https://github.com/yunlux/Anet@v0.12.1" in calls[0]


def test_repository_software_update_rejects_an_invalid_ref(
    tmp_path: Path,
) -> None:
    with pytest.raises(RemoteControlError, match="invalid Git reference"):
        remote_control._install_software(
            tmp_path,
            {
                "repo_url": "https://github.com/yunlux/Anet",
                "repo_ref": "../../main",
            },
            {},
        )


def test_failed_software_update_restores_the_active_package(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    package = runtime / "site-packages" / "anet"
    metadata = runtime / "site-packages" / "anet_fabric-0.12.1.dist-info"
    monkeypatch.setattr(remote_control.sys, "prefix", str(runtime))
    package.mkdir(parents=True)
    metadata.mkdir(parents=True)
    (package / "marker.py").write_text("old", encoding="utf-8")
    (metadata / "METADATA").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        remote_control,
        "_installed_package_paths",
        lambda: (package, metadata),
    )
    monkeypatch.setattr(remote_control, "_current_version", lambda: "0.12.1")

    def fake_download(url: str, destination: Path, *, timeout: float) -> None:
        del url, timeout
        destination.write_bytes(b"wheel")

    monkeypatch.setattr(remote_control, "_download", fake_download)

    def fail_install(command: list[str], check: bool) -> None:
        del command, check
        (package / "marker.py").write_text("new", encoding="utf-8")
        (metadata / "METADATA").write_text("new", encoding="utf-8")
        new_metadata = metadata.parent / "anet_fabric-0.12.2.dist-info"
        new_metadata.mkdir()
        (new_metadata / "METADATA").write_text("new", encoding="utf-8")
        raise RuntimeError("pip failed")

    monkeypatch.setattr(remote_control.subprocess, "run", fail_install)

    with pytest.raises(RuntimeError, match="pip failed"):
        remote_control._install_software(
            tmp_path / "node",
            {
                "version": "0.12.2",
                "wheel_url": "https://example.invalid/new.whl",
            },
            {"software_key": "previous-page"},
        )

    assert (package / "marker.py").read_text(encoding="utf-8") == "old"
    assert (metadata / "METADATA").read_text(encoding="utf-8") == "old"
    assert not (metadata.parent / "anet_fabric-0.12.2.dist-info").exists()


def test_initial_same_version_software_is_recorded_without_reinstall(
    tmp_path: Path, monkeypatch
) -> None:
    software = {"version": "0.12.1", "wheel_url": "https://example.invalid/initial.whl"}
    monkeypatch.setattr(remote_control, "_current_version", lambda: "0.12.1")
    monkeypatch.setattr(
        remote_control,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("initial version should not reinstall"),
    )
    state: dict[str, object] = {}

    assert remote_control._install_software(tmp_path, software, state) is False
    assert state["software_key"]
