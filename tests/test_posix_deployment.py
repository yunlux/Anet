from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from posix_oneclick import (  # noqa: E402
    DeploymentError,
    LAUNCHD_LABEL,
    SYSTEMD_SERVICE,
    launchd_plist,
    launchd_service_state,
    platform_config,
    platform_software,
    read_node_id,
    repository_ref,
    repository_source,
    systemd_unit,
    trusted_keys_from_args,
    validate_cross_platform_locators,
    validate_cross_platform_ports,
    wheel_hash_for_install,
)
from posix_runtime_installer import source_requirement  # noqa: E402


def test_systemd_unit_runs_the_remote_supervisor() -> None:
    unit = systemd_unit(
        Path("<HOME>/.local/anet/versions/0.12.1-mcp/venv/bin/python"),
        Path("<HOME>/.local/anet/nodes/default"),
    )
    assert SYSTEMD_SERVICE == "anet-supervisor.service"
    assert "ExecStart=" in unit
    assert "EnvironmentFile=-%h/.config/anet/discord-social.env" in unit
    assert " -m anet --home " in unit
    assert " supervisor" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_read_node_id_uses_verified_cli_status(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = "an1aaaaaaaaaaaaaaaaa"

    def fake_run(command: list[str], **_kwargs: object) -> str:
        assert command[-1] == "status"
        return '{"node_id":"' + expected + '"}'

    monkeypatch.setattr("posix_oneclick.run", fake_run)
    assert read_node_id(Path("python"), Path("node")) == expected


def test_read_node_id_rejects_incomplete_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("posix_oneclick.run", lambda *_args, **_kwargs: "{}")
    with pytest.raises(DeploymentError, match="complete Node ID"):
        read_node_id(Path("python"), Path("node"))


def test_pinned_wheel_requires_a_declared_or_explicit_hash() -> None:
    with pytest.raises(DeploymentError, match="requires software.sha256"):
        wheel_hash_for_install(Path("wheel.whl"), require_hash=True)

    expected = "a" * 64
    assert (
        wheel_hash_for_install(
            Path("wheel.whl"),
            declared_hash=expected,
            require_hash=True,
        )
        == expected
    )

    with pytest.raises(DeploymentError, match="64 hex characters"):
        wheel_hash_for_install(
            Path("wheel.whl"),
            explicit_hash="not-a-hash",
            require_hash=True,
        )

    with pytest.raises(DeploymentError, match="does not match software.sha256"):
        wheel_hash_for_install(
            Path("wheel.whl"),
            explicit_hash="a" * 64,
            declared_hash="b" * 64,
            require_hash=True,
        )


def test_platform_config_selects_the_platform_specific_node_settings() -> None:
    page = {
        "config": {"sync_interval": 1.0},
        "platforms": {
            "windows": {"config": {"listen_port": 43111}},
            "wsl": {"config": {"listen_port": 43112}},
        },
    }
    assert platform_config(page, "windows")["listen_port"] == 43111
    assert platform_config(page, "wsl")["listen_port"] == 43112
    assert platform_config(page, "linux") == {"sync_interval": 1.0}


def test_platform_config_accepts_default_config_alias() -> None:
    page = {
        "default_config": {"sync_interval": 1.0},
        "platforms": {
            "wsl": {"default_config": {"listen_port": 43112}},
        },
    }

    assert platform_config(page, "wsl") == {
        "sync_interval": 1.0,
        "listen_port": 43112,
    }


def test_platform_overlays_deep_merge_initial_config_and_software() -> None:
    page = {
        "config": {"capabilities": {"direct": True, "relay": False}},
        "software": {
            "version": "0.12.1",
            "metadata": {"channel": "stable", "track": "lts"},
        },
        "platforms": {
            "wsl": {
                "default_config": {"capabilities": {"relay": True}},
                "software": {"metadata": {"track": "edge"}},
            }
        },
    }

    assert platform_config(page, "wsl") == {
        "capabilities": {"direct": True, "relay": True}
    }
    assert platform_software(page, "wsl") == {
        "version": "0.12.1",
        "metadata": {"channel": "stable", "track": "edge"},
    }


def test_platform_software_selects_the_platform_specific_artifact() -> None:
    page = {
        "software": {
            "version": "0.12.1",
            "wheel_url": "common.whl",
        },
        "platforms": {
            "windows": {
                "software": {"wheel_url": "windows.whl"},
            },
            "termux": {
                "software": {"wheel_url": "termux.whl", "sha256": "abc"},
            },
        },
    }
    assert platform_software(page, "windows") == {
        "version": "0.12.1",
        "wheel_url": "windows.whl",
    }
    assert platform_software(page, "termux")["wheel_url"] == "termux.whl"
    assert platform_software(page, "linux")["wheel_url"] == "common.whl"


def test_repository_source_falls_back_to_the_page_repo_url() -> None:
    page = {"repo_url": "https://github.com/yunlux/Anet"}
    assert repository_source(
        page,
        {"version": "0.12.1"},
        "https://example.test/control.json",
    ) == "https://github.com/yunlux/Anet"


def test_repository_source_resolves_relative_repo_urls() -> None:
    assert repository_source(
        {},
        {"repo_url": "../Anet"},
        "https://example.test/config/control.json",
    ) == "https://example.test/Anet"


def test_repository_ref_falls_back_to_the_page_ref() -> None:
    assert repository_ref(
        {"repo_ref": "v0.12.1"},
        {"version": "0.12.1"},
    ) == "v0.12.1"


def test_control_publisher_pin_requires_a_valid_public_key() -> None:
    public_key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert trusted_keys_from_args("community-main", public_key) == {
        "community-main": public_key
    }
    with pytest.raises(DeploymentError, match="provided together"):
        trusted_keys_from_args("community-main", "")
    with pytest.raises(DeploymentError, match="32 bytes"):
        trusted_keys_from_args("community-main", "AA")


def test_source_requirement_preserves_feature_extras() -> None:
    assert source_requirement("https://github.com/yunlux/Anet", "core") == (
        "git+https://github.com/yunlux/Anet"
    )
    assert source_requirement("https://github.com/yunlux/Anet", "mcp") == (
        "anet-fabric[mcp] @ git+https://github.com/yunlux/Anet"
    )
    assert source_requirement(
        "https://github.com/yunlux/Anet",
        "core",
        "v0.12.1",
    ) == "git+https://github.com/yunlux/Anet@v0.12.1"


def test_cross_platform_host_locators_cannot_use_loopback() -> None:
    page = {"platforms": {"windows": {}, "wsl": {}}}
    try:
        validate_cross_platform_locators(
            page,
            "windows",
            listen_host="0.0.0.0",
            advertise=[
                "tls://127.0.0.1:43111?scope=host&zone=abcdefgh&priority=0"
            ],
            contexts=["host:abcdefgh"],
        )
    except DeploymentError as exc:
        assert "must not advertise" in str(exc)
    else:
        raise AssertionError("loopback must not be a cross-platform host locator")


def test_cross_platform_host_locators_require_a_reachable_address() -> None:
    page = {"platforms": {"windows": {}, "wsl": {}}}
    validate_cross_platform_locators(
        page,
        "wsl",
        listen_host="0.0.0.0",
        advertise=[
            "tls://192.0.2.10:43112?scope=host&zone=abcdefgh&priority=0"
        ],
        contexts=["host:abcdefgh"],
    )


def test_cross_platform_host_locators_require_distinct_ports() -> None:
    page = {
        "config": {},
        "platforms": {
            "windows": {
                "config": {
                    "listen_port": 43111,
                    "locator_contexts": ["host:abcdefgh"],
                    "advertise": [
                        "tls://192.0.2.10:43111?scope=host&zone=abcdefgh"
                    ],
                }
            },
            "wsl": {
                "config": {
                    "listen_port": 43111,
                    "locator_contexts": ["host:abcdefgh"],
                    "advertise": [
                        "tls://192.0.2.10:43111?scope=host&zone=abcdefgh"
                    ],
                }
            },
        },
    }
    try:
        validate_cross_platform_ports(
            page,
            "windows",
            listen_port=43111,
            contexts=["host:abcdefgh"],
            advertise=[
                "tls://192.0.2.10:43111?scope=host&zone=abcdefgh"
            ],
        )
    except DeploymentError as exc:
        assert "distinct listener ports" in str(exc)
    else:
        raise AssertionError("shared host-scoped nodes must not reuse a port")


def test_cross_platform_host_locators_accept_distinct_ports() -> None:
    page = {
        "platforms": {
            "windows": {
                "config": {
                    "listen_port": 43111,
                    "locator_contexts": ["host:abcdefgh"],
                    "advertise": [
                        "tls://192.0.2.10:43111?scope=host&zone=abcdefgh"
                    ],
                }
            },
            "wsl": {
                "config": {
                    "listen_port": 43112,
                    "locator_contexts": ["host:abcdefgh"],
                    "advertise": [
                        "tls://192.0.2.10:43112?scope=host&zone=abcdefgh"
                    ],
                }
            },
        }
    }
    validate_cross_platform_ports(
        page,
        "wsl",
        listen_port=43112,
        contexts=["host:abcdefgh"],
        advertise=["tls://192.0.2.10:43112?scope=host&zone=abcdefgh"],
    )


def test_cross_platform_host_scope_must_be_declared_on_both_overlays() -> None:
    page = {
        "platforms": {
            "windows": {
                "config": {
                    "listen_port": 43111,
                    "locator_contexts": ["host:abcdefgh"],
                    "advertise": [
                        "tls://192.0.2.10:43111?scope=host&zone=abcdefgh"
                    ],
                }
            },
            "wsl": {"config": {"listen_port": 43112}},
        }
    }
    try:
        validate_cross_platform_ports(
            page,
            "wsl",
            listen_port=43112,
            contexts=[],
            advertise=[],
        )
    except DeploymentError as exc:
        assert "host scope must be declared on both" in str(exc)
    else:
        raise AssertionError("mixed host scope must be rejected")


def test_launchd_plist_runs_at_login_and_keeps_supervisor_alive() -> None:
    value = plistlib.loads(
        launchd_plist(
            Path("<HOME>/Library/Application Support/Anet/venv/bin/python"),
            Path("<HOME>/Library/Application Support/Anet/nodes/default"),
        )
    )
    assert value["Label"] == LAUNCHD_LABEL
    assert value["RunAtLoad"] is True
    assert value["KeepAlive"] is True
    assert value["ProgramArguments"][-1] == "supervisor"
    assert value["ProgramArguments"][1:3] == ["-m", "anet"]


def test_launchd_service_state_requires_an_observed_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "posix_oneclick.run",
        lambda _command, **_kwargs: "state = running",
    )
    assert launchd_service_state("launchctl", "gui/501/net.anet.supervisor") == "running"

    monkeypatch.setattr(
        "posix_oneclick.run",
        lambda _command, **_kwargs: "state = exited",
    )
    assert launchd_service_state("launchctl", "gui/501/net.anet.supervisor") == "exited"

    monkeypatch.setattr(
        "posix_oneclick.run",
        lambda _command, **_kwargs: "path = /tmp/agent.plist",
    )
    with pytest.raises(DeploymentError, match="did not report a state"):
        launchd_service_state("launchctl", "gui/501/net.anet.supervisor")


def test_posix_entrypoints_and_documentation_are_packaged() -> None:
    for name in (
        "posix_oneclick.py",
        "install_wsl_oneclick.py",
        "install_linux_oneclick.py",
        "install_macos_oneclick.py",
        "install_termux_oneclick.py",
        "install_preflight.py",
        "windows_install_preflight.ps1",
        "register_wsl_keepalive.ps1",
        "sign_control_page.py",
    ):
        assert (ROOT / "scripts" / name).is_file()
    guide = (ROOT / "docs" / "POSIX_AUTOSTART.md").read_text(encoding="utf-8")
    assert "anet-supervisor.service" in guide
    assert "net.anet.supervisor" in guide
    assert "install_wsl_oneclick.py" in guide
    assert "install_linux_oneclick.py" in guide
    assert "install_macos_oneclick.py" in guide
    assert "control-key-id" in guide
    assert "register_wsl_keepalive.ps1" in guide
    termux_guide = (ROOT / "docs" / "TERMUX_AUTOSTART.md").read_text(
        encoding="utf-8"
    )
    assert "termux-services" in termux_guide
    assert "Termux:Boot" in termux_guide
    assert "install_termux_oneclick.py" in termux_guide
    assert "control-key-id" in termux_guide
