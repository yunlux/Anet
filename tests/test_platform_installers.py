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
        assert "relationship-disclosures.json" in source(name), name
