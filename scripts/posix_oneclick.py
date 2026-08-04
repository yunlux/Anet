#!/usr/bin/env python3
"""Install and supervise one Anet node on WSL, Linux, or macOS.

This is the POSIX counterpart to ``install_windows_oneclick.ps1``.  It keeps
the platform runtime installer separate from the explicit persistent-node
deployment layer, then uses systemd user services on Linux/WSL and a launchd
LaunchAgent on macOS.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from install_preflight import (
    InstallationLock,
    PreflightConflict,
    assert_no_duplicate,
    collect_preflight,
    emit_preflight,
)
from posix_runtime_installer import InstallError, install_runtime


CONTROL_MAX_BYTES = 8 * 1024 * 1024
PACKAGE_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_POLL_SECONDS = 300
SYSTEMD_SERVICE = "anet-supervisor.service"
LAUNCHD_LABEL = "net.anet.supervisor"
CONTROL_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DeploymentError(RuntimeError):
    """Raised when the explicit one-click deployment cannot be completed."""


def trusted_keys_from_args(key_id: str, public_key: str) -> dict[str, str]:
    clean_key_id = str(key_id or "").strip()
    encoded = str(public_key or "").strip()
    if not clean_key_id and not encoded:
        return {}
    if not clean_key_id or not encoded:
        raise DeploymentError(
            "--control-key-id and --control-public-key must be provided together"
        )
    if not CONTROL_KEY_ID_PATTERN.fullmatch(clean_key_id):
        raise DeploymentError("--control-key-id is invalid")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error) as exc:
        raise DeploymentError("--control-public-key is not valid base64url") from exc
    if len(decoded) != 32:
        raise DeploymentError("--control-public-key must contain 32 bytes")
    return {clean_key_id: encoded}


def run(
    command: list[str],
    *,
    timeout: int = 120,
    allow_failure: bool = False,
) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode and not allow_failure:
        detail = (completed.stderr or completed.stdout)[-4000:].strip()
        raise DeploymentError(
            f"command failed ({completed.returncode}): "
            f"{' '.join(command[:4])}: {detail}"
        )
    return (completed.stdout or "").strip()


def read_node_id(python: Path, node_home: Path) -> str:
    """Read the complete Node ID through the installed CLI status path."""

    output = run(
        [
            str(python),
            "-m",
            "anet",
            "--home",
            str(node_home),
            "status",
        ]
    )
    try:
        status = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("Anet status returned invalid JSON") from exc
    if not isinstance(status, dict):
        raise DeploymentError("Anet status did not return an object")
    node_id = str(status.get("node_id", "")).strip()
    if not node_id.startswith("an1") or not 20 <= len(node_id) <= 128:
        raise DeploymentError("Anet status did not return a complete Node ID")
    return node_id


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.casefold()


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return sys.platform == "linux" and bool(
        os.environ.get("TERMUX_VERSION")
        or prefix.startswith("/data/data/com.termux/")
    )


def read_json_url(url: str) -> dict[str, Any]:
    target = str(url).strip()
    if not target:
        raise DeploymentError("control URL is empty")
    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme:
        raw = Path(target).expanduser().resolve().read_bytes()
    elif parsed.scheme == "file":
        raw = Path(urllib.request.url2pathname(parsed.path)).resolve().read_bytes()
    elif parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            target,
            headers={
                "Accept": "application/json",
                "User-Agent": "Anet-POSIX-OneClick/0.12.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(CONTROL_MAX_BYTES + 1)
        except Exception as exc:
            raise DeploymentError(f"failed to fetch control page: {target}") from exc
    else:
        raise DeploymentError(f"unsupported control URL scheme: {parsed.scheme}")
    if len(raw) > CONTROL_MAX_BYTES:
        raise DeploymentError("control page exceeds the 8 MiB prototype limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("control page is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError("control page must contain a JSON object")
    return value


def platform_config(page: dict[str, Any], platform_name: str) -> dict[str, Any]:
    common = page.get("config", page.get("default_config", {}))
    if common is None:
        common = {}
    if not isinstance(common, dict):
        raise DeploymentError("control page config must be an object")
    platforms = page.get("platforms")
    if platforms is None:
        return dict(common)
    if not isinstance(platforms, dict):
        raise DeploymentError("control page platforms must be an object")
    overlay = platforms.get(platform_name)
    if overlay is None:
        return dict(common)
    if not isinstance(overlay, dict):
        raise DeploymentError(
            f"control page platforms.{platform_name} must be an object"
        )
    config = overlay.get("config", overlay.get("default_config", {}))
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise DeploymentError(
            f"control page platforms.{platform_name}.config must be an object"
        )
    merged = dict(common)
    merged.update(config)
    return merged


def platform_software(page: dict[str, Any], platform_name: str) -> dict[str, Any]:
    common = page.get("software", {})
    if common is None:
        common = {}
    if not isinstance(common, dict):
        raise DeploymentError("control page software must be an object")
    platforms = page.get("platforms")
    if platforms is None:
        return dict(common)
    if not isinstance(platforms, dict):
        raise DeploymentError("control page platforms must be an object")
    overlay = platforms.get(platform_name)
    if overlay is None:
        return dict(common)
    if not isinstance(overlay, dict):
        raise DeploymentError(
            f"control page platforms.{platform_name} must be an object"
        )
    software = overlay.get("software", {})
    if software is None:
        software = {}
    if not isinstance(software, dict):
        raise DeploymentError(
            f"control page platforms.{platform_name}.software must be an object"
        )
    merged = dict(common)
    merged.update(software)
    return merged


def repository_source(
    page: dict[str, Any], software: dict[str, Any], control_url: str
) -> str:
    """Return the effective repository source for an initial install."""

    source = str(
        software.get("repo_url", "")
        or page.get("repo_url", "")
        or page.get("anet_repo", "")
    ).strip()
    return resolve_reference(control_url, source) if source else ""


def repository_ref(page: dict[str, Any], software: dict[str, Any]) -> str:
    """Return the optional Git branch, tag, or commit for the source."""

    return str(
        software.get("repo_ref", "")
        or page.get("repo_ref", "")
        or page.get("anet_repo_ref", "")
    ).strip()


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise DeploymentError(f"{label} must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _is_loopback_host(value: str) -> bool:
    host = str(value).strip().strip("[]").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard_host(value: str) -> bool:
    return str(value).strip().strip("[]") in {"0.0.0.0", "::"}


def validate_cross_platform_locators(
    page: dict[str, Any],
    platform_name: str,
    *,
    listen_host: str,
    advertise: list[str] | None,
    contexts: list[str] | None,
) -> None:
    """Reject loopback host locators for a Windows/WSL shared deployment."""

    if platform_name not in {"windows", "wsl"}:
        return
    platforms = page.get("platforms")
    if not isinstance(platforms, dict):
        return
    other = "wsl" if platform_name == "windows" else "windows"
    if not isinstance(platforms.get(other), dict):
        return

    host_context = any(str(item).startswith("host:") for item in contexts or [])
    host_locators: list[tuple[str, str]] = []
    for address in advertise or []:
        parsed = urllib.parse.urlsplit(str(address))
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("scope", [""])[0] == "host":
            host_locators.append((str(address), str(parsed.hostname or "")))
    if not host_context and not host_locators:
        return
    if _is_loopback_host(listen_host):
        raise DeploymentError(
            "Windows/WSL host-scoped deployment must not listen on loopback; "
            "use a mirrored host IP/hostname or another non-loopback interface"
        )
    loopback_locators = [
        address for address, host in host_locators if _is_loopback_host(host)
    ]
    if loopback_locators:
        raise DeploymentError(
            "Windows/WSL host-scoped locators must not advertise 127.0.0.1, "
            "localhost, or ::1; use the shared non-loopback host address"
        )
    if not host_locators and _is_wildcard_host(listen_host):
        raise DeploymentError(
            "a wildcard Windows/WSL listener needs an explicit host-scoped "
            "--advertise address reachable from both runtimes"
        )


def _effective_platform_config(
    page: dict[str, Any], platform_name: str
) -> dict[str, Any] | None:
    platforms = page.get("platforms")
    if not isinstance(platforms, dict):
        return None
    overlay = platforms.get(platform_name)
    if not isinstance(overlay, dict):
        return None
    base = page.get("config", {})
    if not isinstance(base, dict):
        base = {}
    patch = overlay.get("config", {})
    if not isinstance(patch, dict):
        raise DeploymentError(
            f"control page platforms.{platform_name}.config must be an object"
        )
    merged = dict(base)
    merged.update(patch)
    return merged


def _has_host_scope(config: dict[str, Any]) -> bool:
    if not bool(config.get("listen_enabled", True)):
        return False
    contexts = config.get("locator_contexts", [])
    advertise = config.get("advertise", [])
    if not isinstance(contexts, list) or not isinstance(advertise, list):
        return False
    if any(str(item).startswith("host:") for item in contexts):
        return True
    for address in advertise:
        parsed = urllib.parse.urlsplit(str(address))
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("scope", [""])[0] == "host":
            return True
    return False


def validate_cross_platform_ports(
    page: dict[str, Any],
    platform_name: str,
    *,
    listen_port: int | None,
    contexts: list[str] | None,
    advertise: list[str] | None,
    listen_enabled: bool = True,
) -> None:
    """Require distinct ports for shared host-scoped Windows and WSL nodes."""

    if platform_name not in {"windows", "wsl"}:
        return
    other_name = "wsl" if platform_name == "windows" else "windows"
    current = _effective_platform_config(page, platform_name)
    other = _effective_platform_config(page, other_name)
    if current is None or other is None:
        return
    current = dict(current)
    current["listen_enabled"] = listen_enabled
    if contexts is not None:
        current["locator_contexts"] = list(contexts)
    if advertise is not None:
        current["advertise"] = list(advertise)
    if not bool(current.get("listen_enabled", True)) or not bool(
        other.get("listen_enabled", True)
    ):
        return
    current_host_scope = _has_host_scope(current)
    other_host_scope = _has_host_scope(other)
    if current_host_scope != other_host_scope:
        raise DeploymentError(
            "Windows and WSL host scope must be declared on both enabled overlays"
        )
    if not current_host_scope:
        return
    if listen_port is None:
        raise DeploymentError(
            "Windows/WSL host-scoped deployments require explicit distinct ports"
        )
    try:
        other_port = int(other.get("listen_port"))
    except (TypeError, ValueError) as exc:
        raise DeploymentError(
            f"platforms.{other_name}.config.listen_port is invalid"
        ) from exc
    if not 1 <= int(listen_port) <= 65535 or not 1 <= other_port <= 65535:
        raise DeploymentError(
            "Windows/WSL host-scoped deployments require listener ports from 1 to 65535"
        )
    if int(listen_port) == other_port:
        raise DeploymentError(
            "Windows and WSL host-scoped deployments must use distinct listener ports"
        )


def apply_locator_config(
    python: Path,
    node_home: Path,
    *,
    contexts: list[str] | None,
    advertise: list[str] | None,
) -> None:
    if not contexts and advertise is None:
        return
    arguments = [
        str(python),
        "-m",
        "anet",
        "--home",
        str(node_home),
        "locator-config",
    ]
    for context in contexts or []:
        arguments.extend(("--add-context", context))
    for address in advertise or []:
        arguments.extend(("--advertise", address))
    run(arguments)


def resolve_reference(base_url: str, value: str) -> str:
    reference = str(value).strip()
    parsed = urllib.parse.urlparse(reference)
    if parsed.scheme:
        return reference
    base = urllib.parse.urlparse(base_url)
    if base.scheme in {"http", "https"}:
        return urllib.parse.urljoin(base_url, reference)
    if base.scheme == "file":
        base_path = Path(urllib.request.url2pathname(base.path))
    else:
        base_path = Path(base_url)
    return str((base_path.expanduser().resolve().parent / reference).resolve())


def download(url: str, destination: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        destination.write_bytes(Path(url).expanduser().resolve().read_bytes())
        return
    if parsed.scheme == "file":
        source = Path(urllib.request.url2pathname(parsed.path)).resolve()
        destination.write_bytes(source.read_bytes())
        return
    if parsed.scheme not in {"http", "https"}:
        raise DeploymentError(f"unsupported wheel URL scheme: {parsed.scheme}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Anet-POSIX-OneClick/0.12.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read(PACKAGE_MAX_BYTES + 1)
    except Exception as exc:
        raise DeploymentError(f"failed to download wheel: {url}") from exc
    if len(data) > PACKAGE_MAX_BYTES:
        raise DeploymentError("wheel exceeds the 256 MiB prototype limit")
    destination.write_bytes(data)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def wheel_hash_for_install(
    path: Path,
    *,
    explicit_hash: str = "",
    declared_hash: Any = "",
    require_hash: bool = False,
) -> str:
    """Return the wheel hash accepted by the initial runtime installer."""

    value = str(explicit_hash or declared_hash or "").strip()
    if not value:
        if require_hash:
            raise DeploymentError(
                "pinned control page requires software.sha256 or --wheel-sha256 "
                "for wheel installation"
            )
        return sha256(path)
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        raise DeploymentError("wheel SHA-256 must contain 64 hex characters")
    return value


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def atomic_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.new")
    pending.write_text(value, encoding="utf-8")
    os.chmod(pending, mode)
    os.replace(pending, path)


def systemd_quote(value: str | Path) -> str:
    text = str(value)
    if any(character in text for character in "\r\n\0"):
        raise DeploymentError("service argument contains a control character")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(python: Path, home: Path) -> str:
    return f"""[Unit]
Description=Anet remote control supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-%h/.config/anet/discord-social.env
WorkingDirectory={systemd_quote(home)}
ExecStart={systemd_quote(python)} -m anet --home {systemd_quote(home)} supervisor
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_systemd_service(python: Path, home: Path, *, enable_linger: bool) -> dict[str, Any]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise DeploymentError("systemctl is required for Linux/WSL auto-start")
    state = run(
        [systemctl, "--user", "is-system-running"],
        allow_failure=True,
    )
    if state not in {"running", "degraded"}:
        raise DeploymentError(
            "systemd user manager is unavailable; enable systemd and a user session"
        )
    was_active = (
        run(
            [systemctl, "--user", "is-active", SYSTEMD_SERVICE],
            allow_failure=True,
        )
        == "active"
    )
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / SYSTEMD_SERVICE
    atomic_text(unit_path, systemd_unit(python, home))
    run([systemctl, "--user", "daemon-reload"])
    if was_active:
        run([systemctl, "--user", "restart", SYSTEMD_SERVICE])
    else:
        run([systemctl, "--user", "enable", "--now", SYSTEMD_SERVICE])
    active = run([systemctl, "--user", "is-active", SYSTEMD_SERVICE])
    if active != "active":
        raise DeploymentError(f"systemd service is not active: {SYSTEMD_SERVICE}")

    linger = "not-requested"
    loginctl = shutil.which("loginctl")
    if enable_linger and loginctl:
        completed = subprocess.run(
            [loginctl, "enable-linger", getpass.getuser()],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        linger = "enabled" if completed.returncode == 0 else "not-enabled"
    elif enable_linger:
        linger = "loginctl-unavailable"
    return {
        "kind": "systemd-user",
        "name": SYSTEMD_SERVICE,
        "unit": str(unit_path),
        "state": active,
        "linger": linger,
    }


def launchd_plist(python: Path, home: Path) -> bytes:
    log_path = home / "supervisor.log"
    value = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "anet",
            "--home",
            str(home),
            "supervisor",
        ],
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def launchd_service_state(launchctl: str, target: str) -> str:
    """Return the launchd state and fail when it cannot be observed."""

    output = run([launchctl, "print", target])
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("state ="):
            state = line.partition("=")[2].strip()
            if state:
                return state
    raise DeploymentError(
        f"launchd did not report a state for the Anet service: {target}"
    )


def install_launchd_service(python: Path, home: Path) -> dict[str, Any]:
    launchctl = shutil.which("launchctl")
    if not launchctl:
        raise DeploymentError("launchctl is required for macOS auto-start")
    agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = agents / f"{LAUNCHD_LABEL}.plist"
    atomic_text(plist_path, launchd_plist(python, home).decode("utf-8"))
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    domain = f"gui/{os.getuid()}"
    run([launchctl, "bootout", target], allow_failure=True)
    run([launchctl, "bootstrap", domain, str(plist_path)])
    run([launchctl, "kickstart", "-k", target])
    state = launchd_service_state(launchctl, target)
    if state != "running":
        raise DeploymentError(
            f"launchd service is not running: {target} (state: {state})"
        )
    return {
        "kind": "launchd-user-agent",
        "name": LAUNCHD_LABEL,
        "plist": str(plist_path),
        "state": state,
    }


def parser(platform_name: str, default_root: Path) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            f"Install one self-starting Anet node on {platform_name} from "
            "a remote JSON control page."
        )
    )
    result.add_argument("--control-url", required=True)
    result.add_argument("--control-key-id", default="")
    result.add_argument("--control-public-key", default="")
    result.add_argument("--feature", choices=("core", "mcp", "full"), default="mcp")
    result.add_argument("--version", default="")
    result.add_argument("--wheel", type=Path)
    result.add_argument("--wheel-sha256", default="")
    result.add_argument("--root", type=Path, default=default_root)
    result.add_argument("--node-home", type=Path)
    result.add_argument("--label", default=f"{platform_name}-node")
    result.add_argument("--listen-host", default="")
    result.add_argument("--port", type=int)
    result.add_argument("--advertise", action="append", default=None)
    result.add_argument("--locator-context", action="append", default=None)
    result.add_argument(
        "--allow-existing",
        action="store_true",
        help="explicitly continue when another same-platform Anet install is found",
    )
    result.add_argument(
        "--no-linger",
        action="store_true",
        help="do not request a persistent systemd user session on Linux/WSL",
    )
    return result


def _main_unlocked(
    platform_name: str,
    default_root: Path,
    args: argparse.Namespace,
) -> int:
    if platform_name == "wsl" and not is_wsl():
        raise DeploymentError("this entry point must run inside WSL")
    if platform_name == "linux":
        if is_termux():
            raise DeploymentError(
                "Termux detected; use install_termux_oneclick.py"
            )
        if sys.platform != "linux" or is_wsl():
            raise DeploymentError("this entry point is for non-WSL Linux")
    if platform_name == "macos" and sys.platform != "darwin":
        raise DeploymentError("this entry point must run on macOS")

    root = args.root.expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise DeploymentError(f"install root is too broad: {root}")
    node_home = (
        args.node_home.expanduser().resolve()
        if args.node_home
        else root / "nodes" / "default"
    )
    preflight = collect_preflight(platform_name, root)
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
    page = read_json_url(args.control_url)
    trusted_keys = trusted_keys_from_args(
        args.control_key_id,
        args.control_public_key,
    )
    software = platform_software(page, platform_name)
    if not isinstance(software, dict):
        raise DeploymentError("control page must contain a software object")
    page_config = platform_config(page, platform_name)
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
        platform_name,
        listen_host=listen_host,
        advertise=advertise,
        contexts=contexts,
    )
    validate_cross_platform_ports(
        page,
        platform_name,
        listen_port=requested_port,
        contexts=contexts,
        advertise=advertise,
        listen_enabled=bool(page_config.get("listen_enabled", True)),
    )
    version = str(args.version or software.get("version", "")).strip()
    if not version:
        raise DeploymentError("control page software.version is required")

    wheel_path = args.wheel.expanduser().resolve() if args.wheel else None
    source_url = ""
    source_ref = ""
    with tempfile.TemporaryDirectory(prefix="anet-oneclick-") as temporary:
        if wheel_path is None:
            wheel_url = str(software.get("wheel_url", "")).strip()
            if wheel_url:
                declared_hash = str(software.get("sha256", "")).strip()
                candidate_hash = str(
                    args.wheel_sha256 or declared_hash
                ).strip()
                if trusted_keys and not candidate_hash:
                    raise DeploymentError(
                        "pinned control page requires software.sha256 for wheel "
                        "installation"
                    )
                if candidate_hash and not re.fullmatch(
                    r"[0-9A-Fa-f]{64}", candidate_hash
                ):
                    raise DeploymentError(
                        "wheel SHA-256 must contain 64 hex characters"
                    )
                wheel_path = Path(temporary) / f"anet-fabric-{version}.whl"
                download(resolve_reference(args.control_url, wheel_url), wheel_path)
            else:
                source_url = repository_source(page, software, args.control_url)
                source_ref = repository_ref(page, software)
                if not source_url:
                    raise DeploymentError(
                        "control page software.wheel_url or software.repo_url "
                        "is required for initial install"
                    )
        wheel_hash = ""
        if wheel_path is not None:
            if not wheel_path.is_file():
                raise DeploymentError(f"wheel does not exist: {wheel_path}")
            wheel_hash = wheel_hash_for_install(
                wheel_path,
                explicit_hash=args.wheel_sha256,
                declared_hash=software.get("sha256", ""),
                require_hash=bool(trusted_keys),
            )

        try:
            runtime = install_runtime(
                platform_name=platform_name,
                version=version,
                wheel=wheel_path,
                wheel_sha256=wheel_hash,
                root=root,
                feature=args.feature,
                source_url=source_url,
                source_ref=source_ref,
            )
        except InstallError as exc:
            raise DeploymentError(str(exc)) from exc

    python = Path(runtime["runtime"]) / "bin" / "python"
    if not python.is_file():
        raise DeploymentError(f"runtime Python is missing: {python}")
    config_path = node_home / "config.json"
    created = False
    if not config_path.is_file():
        if node_home.exists() and any(node_home.iterdir()):
            raise DeploymentError(
                f"refusing to initialize a non-empty incomplete node home: {node_home}"
            )
        if requested_port is None and listen_host != "127.0.0.1":
            raise DeploymentError(
                "--port is required when --listen-host is not 127.0.0.1"
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
            raise DeploymentError(f"cannot read existing node config: {config_path}") from exc
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

    interval = page.get("poll_seconds", DEFAULT_POLL_SECONDS)
    try:
        interval = max(5, min(float(interval), 86400))
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

    # Verify the complete root/nested page and local policy before a
    # persistent service is registered.  This is read-only; the first
    # supervisor sync must still install the page's software artifact.
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
    if platform_name in {"wsl", "linux"}:
        service = install_systemd_service(
            python,
            node_home,
            enable_linger=not args.no_linger,
        )
    else:
        service = install_launchd_service(python, node_home)
    result = {
        "ok": True,
        "outcome": "created" if created else "reused",
        "platform": platform_name,
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
        "preflight": preflight,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0


def main(platform_name: str, default_root: Path) -> int:
    args = parser(platform_name, default_root).parse_args()
    root = args.root.expanduser().resolve()
    with InstallationLock(root):
        return _main_unlocked(platform_name, default_root, args)


if __name__ == "__main__":
    raise SystemExit("use install_wsl_oneclick.py, install_linux_oneclick.py, or install_macos_oneclick.py")
