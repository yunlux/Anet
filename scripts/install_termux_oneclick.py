#!/usr/bin/env python3
"""Install one self-starting Anet node in Termux on Android."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from install_preflight import (
    InstallationLock,
    PreflightConflict,
    assert_no_duplicate,
    collect_preflight,
    emit_preflight,
)
from posix_oneclick import (
    DEFAULT_POLL_SECONDS,
    DeploymentError,
    atomic_text,
    apply_locator_config,
    choose_port,
    download,
    platform_config,
    platform_software,
    read_node_id,
    repository_ref,
    repository_source,
    read_json_url,
    resolve_reference,
    run,
    string_list,
    trusted_keys_from_args,
    validate_cross_platform_locators,
    validate_cross_platform_ports,
    wheel_hash_for_install,
)
from posix_runtime_installer import InstallError, install_runtime


TERMUX_SERVICE = "anet-supervisor"


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return sys.platform == "linux" and bool(
        os.environ.get("TERMUX_VERSION")
        or prefix.startswith("/data/data/com.termux/")
    )


def termux_prefix() -> Path:
    value = os.environ.get("PREFIX", "").strip()
    if not value:
        raise DeploymentError("PREFIX is not set; run this inside Termux")
    prefix = Path(value).expanduser().resolve()
    if not prefix.is_dir():
        raise DeploymentError(f"Termux PREFIX does not exist: {prefix}")
    return prefix


def ensure_termux_packages(prefix: Path, *, update: bool) -> None:
    package_tool = shutil.which("pkg") or str(prefix / "bin" / "pkg")
    if not Path(package_tool).is_file() and not shutil.which(package_tool):
        raise DeploymentError("pkg is required; run this inside Termux")
    if update:
        run([package_tool, "update", "-y"], timeout=600)
    run(
        [
            package_tool,
            "install",
            "-y",
            "python",
            "python-pip",
            "python-cryptography",
            "python-msgpack",
            "termux-services",
            "git",
        ],
        timeout=600,
    )


def shell_script_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def install_termux_service(
    prefix: Path,
    python: Path,
    node_home: Path,
) -> dict[str, Any]:
    service_dir = prefix / "var" / "service" / TERMUX_SERVICE
    run_path = service_dir / "run"
    service_dir.mkdir(parents=True, exist_ok=True)
    atomic_text(
        run_path,
        "\n".join(
            [
                f"#!{prefix / 'bin' / 'sh'}",
                "exec "
                + " ".join(
                    shell_script_quote(item)
                    for item in (
                        python,
                        "-m",
                        "anet",
                        "--home",
                        node_home,
                        "supervisor",
                    )
                ),
                "",
            ]
        ),
        mode=0o700,
    )
    marker = service_dir / ".anet-managed"
    atomic_text(marker, "anet-supervisor\n")

    logger = prefix / "share" / "termux-services" / "svlogger"
    log_run = service_dir / "log" / "run"
    log_run.parent.mkdir(parents=True, exist_ok=True)
    log_run.unlink(missing_ok=True)
    if logger.is_file():
        log_run.symlink_to(logger)
    else:
        atomic_text(
            log_run,
            "\n".join(
                [
                    f"#!{prefix / 'bin' / 'sh'}",
                    "exec " + shell_script_quote(prefix / "bin" / "cat"),
                    "",
                ]
            ),
            mode=0o700,
        )

    start_services = prefix / "etc" / "profile.d" / "start-services.sh"
    shell = prefix / "bin" / "sh"
    if start_services.is_file():
        run([str(shell), "-c", f". {shell_script_quote(start_services)}"], timeout=30)
    sv_enable = prefix / "bin" / "sv-enable"
    sv = prefix / "bin" / "sv"
    if not sv_enable.is_file() or not sv.is_file():
        raise DeploymentError("termux-services did not install sv/sv-enable")
    was_running = "run:" in run(
        [str(sv), "status", TERMUX_SERVICE],
        allow_failure=True,
    )
    run([str(sv_enable), TERMUX_SERVICE], timeout=30)
    if was_running:
        run([str(sv), "restart", TERMUX_SERVICE], timeout=30)
    else:
        run([str(sv), "up", TERMUX_SERVICE], timeout=30)
    status = run([str(sv), "status", TERMUX_SERVICE], allow_failure=True)
    if "run:" not in status:
        raise DeploymentError(f"Termux service is not running: {status}")
    return {
        "kind": "termux-services",
        "name": TERMUX_SERVICE,
        "service_dir": str(service_dir),
        "status": status,
    }


def install_termux_boot(prefix: Path) -> Path:
    boot_dir = Path.home() / ".termux" / "boot"
    boot_path = boot_dir / "start-anet-services"
    boot_script = "\n".join(
        [
            f"#!{prefix / 'bin' / 'sh'}",
            "if command -v termux-wake-lock >/dev/null 2>&1; then",
            "  termux-wake-lock",
            "fi",
            f"if [ -f {shell_script_quote(prefix / 'etc' / 'profile.d' / 'start-services.sh')} ]; then",
            f"  . {shell_script_quote(prefix / 'etc' / 'profile.d' / 'start-services.sh')}",
            "fi",
            f"{shell_script_quote(prefix / 'bin' / 'sv')} up {TERMUX_SERVICE} >/dev/null 2>&1 || true",
            "",
        ]
    )
    atomic_text(boot_path, boot_script, mode=0o700)
    return boot_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install one self-starting Anet node in Termux."
    )
    result.add_argument("--control-url", required=True)
    result.add_argument("--control-key-id", default="")
    result.add_argument("--control-public-key", default="")
    result.add_argument("--feature", choices=("core", "mcp"), default="core")
    result.add_argument("--version", default="")
    result.add_argument("--wheel", type=Path)
    result.add_argument("--wheel-sha256", default="")
    result.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".local" / "anet",
    )
    result.add_argument("--node-home", type=Path)
    result.add_argument("--label", default="termux-node")
    result.add_argument("--listen-host", default="")
    result.add_argument("--port", type=int)
    result.add_argument("--advertise", action="append", default=None)
    result.add_argument("--locator-context", action="append", default=None)
    result.add_argument(
        "--allow-existing",
        action="store_true",
        help="explicitly continue when another Termux Anet install is found",
    )
    result.add_argument(
        "--no-package-update",
        action="store_true",
        help="skip pkg update and only install required Termux packages",
    )
    return result


def _main_unlocked(args: argparse.Namespace) -> int:
    if not is_termux():
        raise DeploymentError("this entry point must run inside Termux on Android")
    prefix = termux_prefix()
    root = args.root.expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise DeploymentError(f"install root is too broad: {root}")
    node_home = (
        args.node_home.expanduser().resolve()
        if args.node_home
        else root / "nodes" / "default"
    )
    preflight = collect_preflight(
        "termux",
        root,
        node_homes=(node_home,),
    )
    emit_preflight(preflight)
    try:
        assert_no_duplicate(
            preflight,
            root,
            deployment=True,
            allow_existing=args.allow_existing,
        )
    except PreflightConflict as exc:
        raise DeploymentError(str(exc)) from exc

    # Package changes happen only after the read-only duplicate check.
    ensure_termux_packages(prefix, update=not args.no_package_update)

    page = read_json_url(args.control_url)
    trusted_keys = trusted_keys_from_args(
        args.control_key_id,
        args.control_public_key,
    )
    software = platform_software(page, "termux")
    if not isinstance(software, dict):
        raise DeploymentError("control page must contain a software object")
    page_config = platform_config(page, "termux")
    requested_listen_host = str(
        args.listen_host or page_config.get("listen_host", "")
    ).strip()
    listen_host = requested_listen_host or "127.0.0.1"
    requested_port = args.port
    if requested_port is None and "listen_port" in page_config:
        try:
            requested_port = int(page_config["listen_port"])
        except (TypeError, ValueError) as exc:
            raise DeploymentError("platform listen_port is invalid") from exc
    if requested_port is not None and not 1 <= requested_port <= 65535:
        raise DeploymentError("--port/listen_port must be between 1 and 65535")
    advertise = args.advertise
    if advertise is None and "advertise" in page_config:
        advertise = string_list(page_config["advertise"], "platform advertise")
    contexts = args.locator_context
    if contexts is None and "locator_contexts" in page_config:
        contexts = string_list(
            page_config["locator_contexts"], "platform locator_contexts"
        )
    validate_cross_platform_locators(
        page,
        "termux",
        listen_host=listen_host,
        advertise=advertise,
        contexts=contexts,
    )
    validate_cross_platform_ports(
        page,
        "termux",
        listen_port=requested_port,
        contexts=contexts,
        advertise=advertise,
        listen_enabled=bool(page_config.get("listen_enabled", True)),
    )
    version = str(args.version or software.get("version", "")).strip()
    if not version:
        raise DeploymentError("control page software.version is required")

    with tempfile.TemporaryDirectory(prefix="anet-termux-") as temporary:
        wheel = args.wheel.expanduser().resolve() if args.wheel else None
        source_url = ""
        source_ref = ""
        if wheel is None:
            wheel_url = str(software.get("wheel_url", "")).strip()
            if wheel_url:
                if (
                    trusted_keys
                    or args.wheel_sha256
                    or str(software.get("sha256", "")).strip()
                ):
                    wheel_hash_for_install(
                        Path(temporary) / f"anet-fabric-{version}.whl",
                        explicit_hash=args.wheel_sha256,
                        declared_hash=software.get("sha256", ""),
                        require_hash=bool(trusted_keys),
                    )
                wheel = Path(temporary) / f"anet-fabric-{version}.whl"
                download(resolve_reference(args.control_url, wheel_url), wheel)
            else:
                source_url = repository_source(page, software, args.control_url)
                source_ref = repository_ref(page, software)
                if not source_url:
                    raise DeploymentError(
                        "control page software.wheel_url or software.repo_url "
                        "is required for initial install"
                    )
        wheel_hash = ""
        if wheel is not None:
            if not wheel.is_file():
                raise DeploymentError(f"wheel does not exist: {wheel}")
            wheel_hash = wheel_hash_for_install(
                wheel,
                explicit_hash=args.wheel_sha256,
                declared_hash=software.get("sha256", ""),
                require_hash=bool(trusted_keys),
            )
        try:
            runtime = install_runtime(
                platform_name="termux",
                version=version,
                wheel=wheel,
                wheel_sha256=wheel_hash,
                root=root,
                feature=args.feature,
                system_site_packages=True,
                install_dependencies=False,
                use_uv=False,
                verify_feature="core",
                source_url=source_url,
                source_ref=source_ref,
            )
        except InstallError as exc:
            raise DeploymentError(str(exc)) from exc

    python = Path(runtime["runtime"]) / "bin" / "python"
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    run([*pip, "defusedxml"], timeout=300)
    run(
        [
            str(python),
            "-c",
            "import anet, cryptography, defusedxml, msgpack",
        ],
    )
    if args.feature == "mcp":
        run([*pip, "mcp"], timeout=600)
        run([str(python), "-c", "import mcp"])

    config_path = node_home / "config.json"
    created = False
    if not config_path.is_file():
        if node_home.exists() and any(node_home.iterdir()):
            raise DeploymentError(
                f"refusing to initialize a non-empty incomplete node home: {node_home}"
            )
        if requested_port is None and listen_host != "127.0.0.1":
            raise DeploymentError(
                "--port/listen_port is required when --listen-host is not 127.0.0.1"
            )
        port = requested_port or choose_port()
        run(
            [
                str(python),
                "-m",
                "anet",
                "--home",
                str(node_home),
                "init",
                "--label",
                args.label,
                "--host",
                listen_host,
                "--port",
                str(port),
            ]
        )
        created = True
    else:
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(
                f"cannot read existing node config: {config_path}"
            ) from exc
        if not isinstance(existing, dict):
            raise DeploymentError("existing node config must be an object")
        port = int(existing.get("listen_port", 4242))
        existing_host = str(existing.get("listen_host", "127.0.0.1"))
        if requested_port is not None and requested_port != port:
            raise DeploymentError(
                f"existing node listens on port {port}; requested {requested_port}"
            )
        if requested_listen_host and requested_listen_host != existing_host:
            raise DeploymentError(
                f"existing node listens on {existing_host}; "
                f"requested {requested_listen_host}"
            )
        listen_host = existing_host

    apply_locator_config(
        python,
        node_home,
        contexts=contexts,
        advertise=advertise,
    )

    try:
        interval = max(5, min(float(page.get("poll_seconds", DEFAULT_POLL_SECONDS)), 86400))
    except (TypeError, ValueError) as exc:
        raise DeploymentError("control page poll_seconds is invalid") from exc
    atomic_text(
        node_home / "remote-control.json",
        json.dumps(
            {
                "version": 1,
                "url": args.control_url,
                "interval": interval,
                **({"trusted_keys": trusted_keys} if trusted_keys else {}),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    # Keep the first persistent service from starting with an unverified
    # signed page.  Verification does not write sync state, so the supervisor
    # still performs the initial software installation on its first poll.
    run(
        [
            str(python),
            "-m",
            "anet",
            "--home",
            str(node_home),
            "control-verify",
            "--url",
            args.control_url,
        ]
    )
    current_config = json.loads(config_path.read_text(encoding="utf-8"))
    node_id = read_node_id(python, node_home)
    service = install_termux_service(prefix, python, node_home)
    boot_script = install_termux_boot(prefix)
    result = {
        "ok": True,
        "outcome": "created" if created else "reused",
        "platform": "termux",
        "runtime": runtime,
        "node": {
            "home": str(node_home),
            "node_id": node_id,
            "listen_host": str(current_config.get("listen_host", listen_host)),
            "port": port,
            "advertise": current_config.get("advertise", []),
            "locator_contexts": current_config.get("locator_contexts", []),
            "control_url": args.control_url,
        },
        "service": service,
        "control_key_id": args.control_key_id,
        "boot_script": str(boot_script),
        "boot_plugin": "Termux:Boot must be installed and opened once",
        "preflight": preflight,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0


def main() -> int:
    args = parser().parse_args()
    with InstallationLock(args.root.expanduser().resolve()):
        return _main_unlocked(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
