from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from deployment_receipt import (  # noqa: E402
    DeploymentReceiptError,
    build_deployment_receipt,
    validate_deployment_receipt,
)


def sample_health() -> dict[str, object]:
    return {
        "kind": "anet.supervisor.health",
        "schema_version": 1,
        "ok": True,
        "state": "running",
        "fresh": True,
        "instance_id": "0123456789abcdef0123456789abcdef",
        "boot_session_id": "test:boot-session",
        "sync_complete": True,
        "supervisor_process_alive": True,
        "child_process_alive": True,
    }


def sample_receipt(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "platform": "wsl",
        "outcome": "created",
        "runtime": {
            "outcome": "installed",
            "platform": "wsl",
            "version": "0.13.0",
            "feature": "mcp",
            "runtime": "/runtime/venv",
            "cli": "/runtime/current/venv/bin/anet",
        },
        "node": {
            "home": "/state/nodes/default",
            "node_id": "an1aaaaaaaaaaaaaaaaa",
            "listen_host": "127.0.0.1",
            "port": 4242,
            "advertise": [],
            "locator_contexts": [],
        },
        "control_url": "https://example.test/anet/control.json",
        "control_key_id": "community-main",
        "supervisor": {
            "kind": "systemd-user",
            "name": "anet-supervisor.service",
            "state": "active",
            "autostart": True,
            "health": sample_health(),
        },
        "preflight": {"schema_version": 1, "findings": []},
    }
    values.update(overrides)
    return build_deployment_receipt(**values)  # type: ignore[arg-type]


def test_receipt_has_one_cross_platform_interface() -> None:
    receipt = sample_receipt()
    assert receipt["kind"] == "anet.deployment.receipt"
    assert receipt["schema_version"] == 1
    assert receipt["outcome"] == "created"
    assert receipt["control"] == {
        "url": "https://example.test/anet/control.json",
        "key_id": "community-main",
        "key_ids": ["community-main"],
        "verified": True,
    }
    assert receipt["supervisor"]["autostart"] is True  # type: ignore[index]


def test_receipt_rejects_unverified_control_or_inactive_contract() -> None:
    receipt = sample_receipt()
    receipt["control"]["verified"] = False  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="control.verified"):
        validate_deployment_receipt(receipt)

    receipt = sample_receipt()
    del receipt["supervisor"]["state"]  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="supervisor.state"):
        validate_deployment_receipt(receipt)

    receipt = sample_receipt()
    receipt["supervisor"]["state"] = "failed"  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="not running"):
        validate_deployment_receipt(receipt)

    receipt = sample_receipt()
    receipt["supervisor"]["health"]["child_process_alive"] = False  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="child process"):
        validate_deployment_receipt(receipt)

    receipt = sample_receipt()
    receipt["supervisor"]["health"]["sync_complete"] = False  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="sync is incomplete"):
        validate_deployment_receipt(receipt)


def test_receipt_reports_multiple_control_publishers_and_accepts_v1_legacy() -> None:
    receipt = sample_receipt(
        control_key_id="root",
        control_key_ids=["root", "actor-a", "actor-b"],
    )
    assert receipt["control"]["key_ids"] == ["root", "actor-a", "actor-b"]  # type: ignore[index]

    legacy = sample_receipt()
    del legacy["control"]["key_ids"]  # type: ignore[index]
    assert validate_deployment_receipt(legacy)["control"]["key_id"] == "community-main"  # type: ignore[index]

    invalid = sample_receipt(
        control_key_id="root",
        control_key_ids=["root", "actor-a"],
    )
    invalid["control"]["key_ids"] = ["actor-a", "root"]  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="first key_ids"):
        validate_deployment_receipt(invalid)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("platform", "android", "platform is unsupported"),
        ("outcome", "installed", "outcome must be created or reused"),
    ),
)
def test_receipt_rejects_unknown_contract_values(
    field: str, value: object, message: str
) -> None:
    receipt = sample_receipt()
    receipt[field] = value
    with pytest.raises(DeploymentReceiptError, match=message):
        validate_deployment_receipt(receipt)


def test_receipt_rejects_incomplete_node_identity_and_invalid_port() -> None:
    receipt = sample_receipt()
    receipt["node"]["node_id"] = "node-a"  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="node.node_id"):
        validate_deployment_receipt(receipt)

    receipt = sample_receipt()
    receipt["node"]["port"] = 0  # type: ignore[index]
    with pytest.raises(DeploymentReceiptError, match="node.port"):
        validate_deployment_receipt(receipt)


def test_receipt_isolated_from_adapter_input() -> None:
    runtime = {
        "version": "0.13.0",
        "feature": "mcp",
        "runtime": "/runtime/venv",
        "cli": "/runtime/current/venv/bin/anet",
    }
    receipt = sample_receipt(runtime=runtime)
    runtime["version"] = "changed"
    assert receipt["runtime"]["version"] == "0.13.0"  # type: ignore[index]


def test_termux_receipt_keeps_boot_prerequisite_as_platform_detail() -> None:
    receipt = sample_receipt(
        platform="termux",
        supervisor={
            "kind": "termux-services",
            "name": "anet-supervisor",
            "state": "running",
            "autostart": True,
            "health": sample_health(),
        },
        platform_details={
            "boot_script": "<TERMUX_HOME>/.termux/boot/start-anet-services",
            "boot_plugin": "Termux:Boot must be installed and opened once",
        },
    )
    assert "Termux:Boot" in receipt["platform_details"]["boot_plugin"]  # type: ignore[index]


def test_receipt_contract_is_published_for_people_and_agents() -> None:
    contract = (ROOT / "docs" / "DEPLOYMENT_RECEIPT_V1.md").read_text(
        encoding="utf-8"
    )
    assert '"kind": "anet.deployment.receipt"' in contract
    assert '"schema_version": 1' in contract
    assert '"key_ids"' in contract
    assert "private deployment evidence" in contract
    assert "does not prove survival" in contract
    assert "SUPERVISOR_HEALTH_V1.md" in contract
    assert '"boot_session_id"' in contract

    for path in (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "CLI_AGENT_GUIDE.md",
        ROOT / "docs" / "WINDOWS_AUTOSTART.md",
        ROOT / "docs" / "POSIX_AUTOSTART.md",
        ROOT / "docs" / "TERMUX_AUTOSTART.md",
    ):
        assert "DEPLOYMENT_RECEIPT_V1.md" in path.read_text(encoding="utf-8")


def test_continuity_contract_is_published_for_people_and_agents() -> None:
    contract = (ROOT / "docs" / "CONTINUITY_GATE_V1.md").read_text(
        encoding="utf-8"
    )
    assert "continuity-prepare" in contract
    assert "continuity-verify" in contract
    assert "--require-boot-change" in contract
    assert "listener ownership" in contract
    assert "physical-device checks" in contract
    for path in (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "CLI_AGENT_GUIDE.md",
        ROOT / "docs" / "PLATFORM_RELEASES.md",
        ROOT / "docs" / "SUPERVISOR_HEALTH_V1.md",
    ):
        assert "CONTINUITY_GATE_V1.md" in path.read_text(encoding="utf-8")
