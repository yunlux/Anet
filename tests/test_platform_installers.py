from __future__ import annotations

import json
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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


def test_core_ci_runs_on_every_branch_push() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "  push:\n  workflow_dispatch:" in workflow
    assert "  push:\n    branches:" not in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" not in (
        workflow
    )


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
    assert "[string]$controlurl" in installer
    assert "-controlkeyid" in installer
    assert "-controlpublickey" in installer
    assert "trusted_keys" in installer
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
    assert "normalize-repositoryref" in installer
    assert "resolve-wheelsha256" in installer
    assert "does not match software.sha256" in installer
    assert 'software.wheel_url or software.repo_url' in installer
    assert '"-sourceurl", $sourceurl' in installer
    assert '"-sourceref", $sourceref' in installer
    assert "helperbranch" in installer
    assert "helperrepository" in installer
    assert "$helperrepository $helperbranch" in installer
    assert 'get-optionalproperty $software "preflight_script_url"' not in installer
    assert 'get-optionalproperty $software "runtime_installer_url"' not in installer
    assert 'get-optionalproperty $software "supervisor_script_url"' not in installer
    assert "stop-managedsupervisortask" in installer
    assert "did not stop within 30 seconds" in installer
    assert "wait-managedsupervisortask" in installer
    assert "did not start within 30 seconds" in installer
    assert '"control-verify"' in installer
    assert "-port" in installer
    assert "-listenhost" in installer
    assert "locatorcontext" in installer
    assert "-advertise" in installer
    assert "preflight" in installer
    assert '"-nodehome", $nodehome' in installer
    assert "allowexisting" in installer
    assert "enter-installmutex" in installer
    assert "another anet installer already owns" in installer
    assert "host-scoped locators must not advertise" in installer
    assert "must use distinct listener ports" in installer
    assert "host scope must be declared on both enabled overlays" in installer
    assert "get-effectiveplatformsoftware" in installer
    assert "merge-jsonobjects" in installer
    assert "default_config" in installer
    assert "return $value -is [pscustomobject]" in installer
    assert "control page platforms must be an object" in installer
    assert re.search(
        r"assert-crossplatformports\s+`\s*\$platformsforvalidation\s+`\s*\$commonconfig",
        installer,
    )
    assert "pinned control page requires software.sha256" in installer
    assert "wheel sha256 must contain 64 hex characters" in installer
    assert "supervisor" in installer
    assert "-m" in launcher
    assert "supervisor" in launcher
    assert "supervisor.log" in launcher
    assert "status" in installer
    assert "node_id" in installer
    assert 'kind = "anet.deployment.receipt"' in installer
    assert "schema_version = 1" in installer
    assert "assert-deploymentreceipt" in installer
    assert "assert-deploymentreceipt $receipt" in installer
    assert "runtimereceipt" in installer
    assert "nodereceipt" in installer
    assert "controlreceipt" in installer
    assert "supervisorreceipt" in installer
    assert 'state = "running"' in installer
    assert "autostart = $true" in installer
    assert "wait-anetsupervisorhealth" in installer
    assert '"supervisor-status"' in installer
    assert "child_process_alive" in installer
    assert "sync_complete" in installer
    assert '-runtimeroot `"$rootpath`" -controlurl' not in installer


def test_posix_oneclick_is_an_explicit_native_service_layer() -> None:
    text = source("posix_oneclick.py")
    assert "systemd" in text
    assert "launchctl" in text
    assert "launchd_service_state" in text
    assert 'state != "running"' in text
    assert "anet-supervisor.service" in text
    assert "net.anet.supervisor" in text
    assert '"restart", systemd_service' in text
    assert '"control-verify"' in text
    assert "--control-url" in text
    assert "--control-key-id" in text
    assert "--control-public-key" in text
    assert "trusted_keys_from_args" in text
    assert "install_runtime" in text
    assert "validate_cross_platform_ports" in text
    assert "platform_software" in text
    assert "_deep_merge" in text
    assert "repository_source" in text
    assert "repository_ref" in text
    assert "software.wheel_url or software.repo_url" in text
    assert "pinned control page requires software.sha256" in text
    assert "does not match software.sha256" in text
    assert "read_node_id" in text
    assert "build_deployment_receipt" in text
    assert "wait_for_supervisor_health" in text
    assert '"supervisor-status"' in text
    assert "installationlock" in text
    assert "node_homes=(node_home,)" in text


def test_checkout_free_posix_bootstrap_fetches_only_known_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = source("bootstrap_posix.py")
    assert "raw.githubusercontent.com" in bootstrap
    assert "parse_known_args" in bootstrap
    assert "install_wsl_oneclick.py" in bootstrap
    assert "install_linux_oneclick.py" in bootstrap
    assert "install_macos_oneclick.py" in bootstrap
    assert "install_termux_oneclick.py" in bootstrap
    assert "posix_runtime_installer.py" in bootstrap
    assert "deployment_receipt.py" in bootstrap
    assert "runpy.run_path" in bootstrap
    assert "temporarydirectory" in bootstrap
    assert "control_page_hints" not in bootstrap
    assert "control page is not used" in bootstrap

    spec = importlib.util.spec_from_file_location(
        "anet_bootstrap_posix", ROOT / "scripts" / "bootstrap_posix.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.normalize_script_ref("release/v0.12.1") == "release/v0.12.1"
    assert module.github_raw_url(
        "https://github.com/example/anet.git", "release/v0.12.1", "posix_oneclick.py"
    ) == (
        "https://raw.githubusercontent.com/example/anet/release/v0.12.1/scripts/"
        "posix_oneclick.py"
    )
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "repo_url": "https://github.com/example/common",
                "repo_ref": "main",
                "platforms": {
                    "wsl": {
                        "software": {
                            "repo_url": "https://github.com/example/wsl",
                            "repo_ref": "release/v0.12.1",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    downloaded: list[str] = []

    def fake_download(url: str, destination: Path) -> None:
        downloaded.append(url)

    def stop_before_running(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(0)

    monkeypatch.setattr(module, "download_source", fake_download)
    monkeypatch.setattr(module.runpy, "run_path", stop_before_running)
    assert (
        module.main(
            [
                "--platform",
                "wsl",
                "--control-url",
                str(control),
            ]
        )
        == 0
    )
    assert downloaded
    assert all("yunlux/Anet/main/scripts/" in url for url in downloaded)
    assert not any("example/evil" in url for url in downloaded)


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
    assert "--control-key-id" in text
    assert "--control-public-key" in text
    assert "trusted_keys_from_args" in text
    assert "install_preflight" in text
    assert "allow-existing" in text
    assert "--listen-host" in text
    assert "apply_locator_config" in text
    assert "validate_cross_platform_locators" in text
    assert "validate_cross_platform_ports" in text
    assert "platform_software" in text
    assert "existing node listens on port" in text
    assert '"restart", termux_service' in text
    assert '"control-verify"' in text
    assert "repository_source" in text
    assert "repository_ref" in text
    assert "software.wheel_url or software.repo_url" in text
    assert "wheel_hash_for_install" in text
    assert "build_deployment_receipt" in text
    assert "wait_for_supervisor_health" in text
    assert "require_hash=bool(trusted_keys)" in text
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
    assert "[string]$nodehome" in text
    assert "test-pathwithin" in text
    assert "env:anet_home" in text
    assert "anet-node-home" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows preflight integration test")
def test_windows_preflight_blocks_explicit_node_home_outside_target(
    tmp_path: Path,
) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    target = tmp_path / "runtime"
    outside = tmp_path / "outside-node"
    outside.mkdir()
    (outside / "config.json").touch()
    script = ROOT / "scripts" / "windows_install_preflight.ps1"

    outside_env = os.environ.copy()
    outside_env["ANET_HOME"] = str(outside)
    outside_result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-TargetRoot",
            str(target),
            "-Deployment",
        ],
        capture_output=True,
        text=True,
        env=outside_env,
        check=False,
    )
    assert outside_result.returncode == 17, (
        outside_result.stdout + outside_result.stderr
    )
    outside_report = json.loads(outside_result.stdout)
    assert any(
        item["kind"] == "anet-node-home"
        and Path(item["path"]).resolve() == outside.resolve()
        for item in outside_report["existing_anet"]
    )

    explicit_env = os.environ.copy()
    explicit_env.pop("ANET_HOME", None)
    explicit_result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-TargetRoot",
            str(target),
            "-NodeHome",
            str(outside),
            "-Deployment",
        ],
        capture_output=True,
        text=True,
        env=explicit_env,
        check=False,
    )
    assert explicit_result.returncode == 17, (
        explicit_result.stdout + explicit_result.stderr
    )
    explicit_report = json.loads(explicit_result.stdout)
    assert any(
        item["kind"] == "anet-node-home"
        and Path(item["path"]).resolve() == outside.resolve()
        for item in explicit_report["existing_anet"]
    )


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
    assert "bootstrap_posix.py" in cli_guide
    assert "--platform termux" in cli_guide
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
