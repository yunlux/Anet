from __future__ import annotations

import plistlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from posix_oneclick import (  # noqa: E402
    DeploymentError,
    LAUNCHD_LABEL,
    SYSTEMD_SERVICE,
    launchd_plist,
    platform_config,
    systemd_unit,
    validate_cross_platform_locators,
    validate_cross_platform_ports,
)


def test_systemd_unit_runs_the_remote_supervisor() -> None:
    unit = systemd_unit(
        Path("<HOME>/.local/anet/versions/0.12.1-mcp/venv/bin/python"),
        Path("<HOME>/.local/anet/nodes/default"),
    )
    assert SYSTEMD_SERVICE == "anet-supervisor.service"
    assert "ExecStart=" in unit
    assert " -m anet --home " in unit
    assert " supervisor" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


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
    ):
        assert (ROOT / "scripts" / name).is_file()
    guide = (ROOT / "docs" / "POSIX_AUTOSTART.md").read_text(encoding="utf-8")
    assert "anet-supervisor.service" in guide
    assert "net.anet.supervisor" in guide
    assert "install_wsl_oneclick.py" in guide
    assert "install_linux_oneclick.py" in guide
    assert "install_macos_oneclick.py" in guide
    assert "register_wsl_keepalive.ps1" in guide
    termux_guide = (ROOT / "docs" / "TERMUX_AUTOSTART.md").read_text(
        encoding="utf-8"
    )
    assert "termux-services" in termux_guide
    assert "Termux:Boot" in termux_guide
    assert "install_termux_oneclick.py" in termux_guide
