from __future__ import annotations

import json
from pathlib import Path

import pytest

from anet.config import NodeConfig, initialize_node
from anet.identity import Identity
from anet.peers import PeerBook
import anet.remote_control as remote_control
from anet.remote_control import (
    RemoteControlError,
    SupervisorLock,
    sync_remote_control,
    write_control_settings,
)


def _write_page(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.as_uri()


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

    def fail_software(home: Path, software: dict, state: dict) -> bool:
        del home, software, state
        raise RuntimeError("software update failed")

    monkeypatch.setattr(remote_control, "_install_software", fail_software)
    with pytest.raises(RuntimeError, match="software update failed"):
        sync_remote_control(local.home, url=page)

    assert (local.home / "config.json").read_bytes() == config_before
    assert (local.home / "card.json").read_bytes() == card_before
    assert (local.home / "peers.json").read_bytes() == peers_before
    assert not (local.home / "remote-control-state.json").exists()


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
