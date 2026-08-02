from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8").lower()


def test_clean_installers_do_not_depend_on_nodes_or_agent_runtimes() -> None:
    forbidden = (
        "hermes",
        "anet_home",
        "identity.json",
        "systemctl",
        "nodehome",
    )
    for name in (
        "install_windows.ps1",
        "install_wsl.py",
        "install_macos.py",
        "posix_runtime_installer.py",
    ):
        text = source(name)
        assert all(token not in text for token in forbidden), name
        assert "agent profile" not in text, name


def test_platform_defaults_are_platform_owned() -> None:
    assert '".local" / "anet"' in source("install_wsl.py")
    assert '"application support" / "anet"' in source(
        "install_macos.py"
    )
    assert 'join-path $env:localappdata "anet"' in source(
        "install_windows.ps1"
    )
    assert "windows_install_preflight.ps1" in source("install_windows.ps1")


def test_windows_oneclick_is_an_explicit_supervised_deployment_layer() -> None:
    installer = source("install_windows_oneclick.ps1")
    launcher = source("run-supervisor.ps1")
    assert "-controlurl" in installer
    assert "register-scheduledtask" in installer
    assert "start-scheduledtask" in installer
    assert "-admin" in installer
    assert "env:programdata" in installer
    assert "-atstartup" in installer
    assert '"system"' in installer
    assert "serviceaccount" in installer
    assert "windows-machine-scheduled-task" in installer
    assert "new-scheduledtasksettingsset" in installer
    assert "restartcount 99" in installer
    assert "executiontimelimit" in installer
    assert 'get-optionalproperty $software "sha256"' in installer
    assert 'software.wheel_url or software.repo_url' in installer
    assert '"-sourceurl", $sourceurl' in installer
    assert '"-sourceref", $sourceref' in installer
    assert "helperbranch" in installer
    assert "stop-managedsupervisortask" in installer
    assert "did not stop within 30 seconds" in installer
    assert "wait-managedsupervisortask" in installer
    assert "did not start within 30 seconds" in installer
    assert "-port" in installer
    assert "-listenhost" in installer
    assert "locatorcontext" in installer
    assert "-advertise" in installer
    assert "preflight" in installer
    assert "allowexisting" in installer
    assert "enter-installmutex" in installer
    assert "another anet installer already owns" in installer
    assert "host-scoped locators must not advertise" in installer
    assert "must use distinct listener ports" in installer
    assert "host scope must be declared on both enabled overlays" in installer
    assert "get-effectiveplatformsoftware" in installer
    assert "default_config" in installer
    assert "supervisor" in installer
    assert "-m" in launcher
    assert "supervisor" in launcher
    assert "supervisor.log" in launcher


def test_posix_oneclick_is_an_explicit_native_service_layer() -> None:
    text = source("posix_oneclick.py")
    assert "systemd" in text
    assert "launchctl" in text
    assert "anet-supervisor.service" in text
    assert "net.anet.supervisor" in text
    assert '"restart", systemd_service' in text
    assert "--control-url" in text
    assert "install_runtime" in text
    assert "validate_cross_platform_ports" in text
    assert "platform_software" in text
    assert "repository_source" in text
    assert "repository_ref" in text
    assert "software.wheel_url or software.repo_url" in text
    assert "installationlock" in text


def test_wsl_host_keepalive_is_an_explicit_user_scoped_bridge() -> None:
    text = source("register_wsl_keepalive.ps1")
    assert "wsl.exe" in text
    assert "systemctl --user start" in text
    assert "systemctl --user is-active --quiet" in text
    assert "sleep 3600" in text
    assert "executiontimelimit" in text
    assert "restartcount 99" in text
    assert "-atlogon" in text
    assert "interactiveToken".lower() in text
    assert "windows-user-wsl-keepalive" in text
    assert "wait-managedtask" in text
    assert "did not start within 30 seconds" in text


def test_termux_oneclick_uses_termux_native_service_layer() -> None:
    text = source("install_termux_oneclick.py")
    assert "python-cryptography" in text
    assert "python-msgpack" in text
    assert "termux-services" in text
    assert "start-anet-services" in text
    assert "--control-url" in text
    assert "install_preflight" in text
    assert "allow-existing" in text
    assert "--listen-host" in text
    assert "apply_locator_config" in text
    assert "platform_software" in text
    assert "existing node listens on port" in text
    assert '"restart", termux_service' in text
    assert "repository_source" in text
    assert "repository_ref" in text
    assert "software.wheel_url or software.repo_url" in text
    assert "installationlock" in text


def test_runtime_installers_record_wheel_or_repository_source() -> None:
    windows = source("install_windows.ps1")
    posix = source("posix_runtime_installer.py")
    assert "provide exactly one of -wheel or -sourceurl" in windows
    assert "source_url" in windows
    assert "provide either a wheel or repository source url" in posix
    assert "source_requirement" in posix
    assert "source_ref" in posix
    assert "enter-installmutex" in windows


def test_windows_preflight_is_bounded_and_distinguishes_ahub() -> None:
    text = source("windows_install_preflight.ps1")
    assert "localappdata" in text
    assert "programdata" in text
    assert "ahub.sqlite3" in text
    assert "scheduled-task" in text
    assert "windows-service" in text
    assert "deployment" in text
    assert "wsl[-_ ]?keepalive" in text


def test_legacy_macos_bootstrap_has_duplicate_preflight() -> None:
    text = source("bootstrap-macos.sh")
    assert "allow-existing" in text
    assert "install preflight" in text
    assert "ahub.sqlite3" in text
    assert "pgrep" in text


def test_multi_node_gate_is_explicit_optional_deployment_layer() -> None:
    text = source("wsl_multi_node_release_gate.py")
    assert "--deployment" in text
    assert "node_home=systemd_service" in text
    assert "shared" not in text


def test_agent_guides_cover_platforms_and_installer_features() -> None:
    cli_guide = (ROOT / "docs" / "CLI_AGENT_GUIDE.md").read_text(
        encoding="utf-8"
    )
    for name in (
        "install_windows.ps1",
        "install_wsl.py",
        "install_macos.py",
    ):
        assert name in cli_guide
    assert "-Feature mcp" in cli_guide
    assert "--feature mcp" in cli_guide
    normalized = " ".join(cli_guide.split())
    assert "must not automatically create a persistent node" in normalized
    assert "CLI control plane + narrowly scoped MCP data plane" in normalized


def test_mcp_guide_lists_every_registered_tool() -> None:
    server_source = (ROOT / "src" / "anet" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "docs" / "MCP_AGENT_GUIDE.md").read_text(
        encoding="utf-8"
    )
    tool_names = set(re.findall(r'name="(anet_[a-z0-9_]+)"', server_source))
    assert tool_names
    assert not {name for name in tool_names if f"`{name}`" not in guide}
    assert "CLI control plane + minimal MCP data plane" in " ".join(
        guide.split()
    )


def test_generic_mcp_example_is_valid_and_fail_closed() -> None:
    example = json.loads(
        (ROOT / "mcp-stdio.example.json").read_text(encoding="utf-8")
    )
    config = example["mcpServers"]["anet"]
    assert config["args"] == ["-m", "anet", "mcp"]
    environment = config["env"]
    assert environment["ANET_MCP_ALLOW_RAW_INBOX"] == "0"
    assert environment["ANET_MCP_ALLOW_RELATION_DISCLOSURE"] == "0"
    assert environment["ANET_MCP_ALLOWED_PEERS"] != "*"
    assert environment["ANET_MCP_TASK_ALLOWED_SENDERS"] != "*"


def test_release_gates_protect_received_relationship_disclosures() -> None:
    for name in ("wsl_release_gate.py", "windows_release_gate.ps1"):
        gate = source(name)
        for protected in (
            "relationship-disclosures.json",
            "relationship-disclosure-schedules.json",
            "relationship-disclosure-gap-notices.json",
            "relationship-disclosure-archive.json",
        ):
            assert protected in gate, (name, protected)
