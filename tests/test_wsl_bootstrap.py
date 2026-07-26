from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "install-anet"
    / "scripts"
    / "bootstrap_wsl.py"
)
SPEC = importlib.util.spec_from_file_location("bootstrap_wsl", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def test_agent_id_is_stable_local_namespace() -> None:
    assert bootstrap.validate_agent_id("Profile-01") == "profile-01"
    for invalid in ("", "../escape", "human name", "_hidden", "a" * 64):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.validate_agent_id(invalid)


def test_ahub_url_is_https_or_loopback_only() -> None:
    assert (
        bootstrap.validate_ahub_url("http://127.0.0.1:8422/")
        == "http://127.0.0.1:8422"
    )
    assert (
        bootstrap.validate_ahub_url("https://ahub.example")
        == "https://ahub.example"
    )
    for invalid in (
        "http://ahub.example",
        "https://user:secret@ahub.example",
        "file:///tmp/ahub",
    ):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.validate_ahub_url(invalid)


def test_service_name_rejects_unit_injection() -> None:
    assert (
        bootstrap.validate_service_name("anet-ahub.service")
        == "anet-ahub.service"
    )
    for invalid in ("", "anet-ahub", "a.service\nAfter=bad", "../a.service"):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.validate_service_name(invalid)


def test_existing_ahub_requires_both_databases(tmp_path: Path) -> None:
    root = tmp_path / "ahub"
    root.mkdir()
    assert bootstrap.existing_ahub_state(root, allow_empty=True) is False

    (root / "ahub.sqlite3").write_bytes(b"")
    with pytest.raises(bootstrap.BootstrapError, match="incomplete"):
        bootstrap.existing_ahub_state(root, allow_empty=True)

    (root / "control.sqlite3").write_bytes(b"")
    assert bootstrap.existing_ahub_state(root, allow_empty=False) is True


def test_registered_missing_ahub_is_not_recreated(tmp_path: Path) -> None:
    with pytest.raises(bootstrap.BootstrapError, match="missing"):
        bootstrap.existing_ahub_state(
            tmp_path / "missing",
            allow_empty=False,
        )


def test_registry_rejects_relative_node_home(tmp_path: Path) -> None:
    registry = tmp_path / "bootstrap.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ahub": None,
                "nodes": {
                    "profile-01": {
                        "home": "relative",
                        "node_id": "an1example",
                        "port": 43101,
                        "service": "anet-node-profile-01.service",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.BootstrapError, match="home and Node ID"):
        bootstrap.load_registry(registry)


def test_incomplete_node_home_is_never_initialized(tmp_path: Path) -> None:
    home = tmp_path / "node"
    home.mkdir()
    (home / "identity.json").write_text("{}", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="incomplete"):
        bootstrap.complete_node(Path("anet"), home)


def test_generated_mcp_config_is_profile_scoped_and_fail_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "mcp.json"
    bootstrap.write_mcp_config(
        output,
        Path("/runtime/python"),
        Path("/state/nodes/profile-01"),
        "profile-01",
        ["an1peer-b", "an1peer-a"],
    )
    config = json.loads(output.read_text(encoding="utf-8"))["mcpServers"][
        "anet"
    ]
    assert Path(config["command"]).parts[-2:] == ("runtime", "python")
    assert config["env"]["ANET_AGENT_ID"] == "profile-01"
    assert config["env"]["ANET_MCP_GROUP_PREFIX"] == "local.profile-01."
    assert config["env"]["ANET_MCP_ALLOWED_PEERS"] == "an1peer-a,an1peer-b"
    assert config["env"]["ANET_MCP_TASK_CAPABILITIES"] == ""
    assert config["env"]["ANET_MCP_ALLOW_RAW_INBOX"] == "0"
    assert config["env"]["ANET_MCP_ALLOW_RELATION_ACTIVITY"] == "0"


def test_user_units_use_current_runtime_and_local_state(tmp_path: Path) -> None:
    cli = tmp_path / ".local" / "anet" / "current" / "venv" / "bin" / "anet"
    state = tmp_path / ".local" / "state" / "anet"
    ahub = bootstrap.ahub_unit(
        cli,
        state / "ahub",
        "127.0.0.1",
        8422,
    )
    node = bootstrap.node_unit(
        cli,
        state / "nodes" / "profile-01",
        state,
        "profile-01",
    )
    assert "ahub-serve" in ahub
    assert "--home" in node
    normalized = node.replace("\\", "/").replace("//", "/")
    assert ".local/state/anet" in normalized
    assert ".local/share" not in normalized
