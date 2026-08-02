#!/usr/bin/env python3
"""Idempotently bootstrap one WSL Agent node around one host-local Ahub."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - the entrypoint rejects non-Linux hosts
    fcntl = None


AGENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
SERVICE_NAME = re.compile(r"[A-Za-z0-9_.@-]+\.service")
AHUB_SERVICE = "anet-ahub.service"
REGISTRY_SCHEMA = 1
AHUB_MARKERS = ("ahub.sqlite3", "control.sqlite3")


class BootstrapError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    timeout: int = 300,
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
        raise BootstrapError(
            f"command failed ({completed.returncode}): "
            f"{' '.join(command[:3])}: {detail}"
        )
    return completed.stdout.strip()


def run_json(command: list[str], *, timeout: int = 300) -> Any:
    raw = run(command, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            f"command did not return JSON: {' '.join(command[:3])}"
        ) from exc


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return "microsoft" in release.casefold()


def validate_agent_id(value: str) -> str:
    normalized = value.strip().casefold()
    if not AGENT_ID.fullmatch(normalized):
        raise BootstrapError(
            "agent ID must match [a-z0-9][a-z0-9._-]{0,62}"
        )
    return normalized


def validate_service_name(value: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if not SERVICE_NAME.fullmatch(normalized):
        raise BootstrapError("invalid systemd user service name")
    return normalized


def validate_ahub_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BootstrapError("Ahub URL must be a credential-free HTTP(S) base")
    if parsed.scheme == "http":
        hostname = parsed.hostname.casefold()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if not loopback:
            raise BootstrapError("plaintext Ahub URL must use loopback")
    return normalized


def private_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path in {Path("/"), Path.home().resolve()}:
        raise BootstrapError(f"state path is too broad: {path}")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def existing_ahub_state(root: Path, *, allow_empty: bool) -> bool:
    root = root.expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise BootstrapError(f"Ahub root is too broad: {root}")
    if not root.is_dir():
        if allow_empty:
            private_directory(root)
            return False
        raise BootstrapError(f"registered Ahub root is missing: {root}")
    present = [(root / marker).is_file() for marker in AHUB_MARKERS]
    if all(present):
        return True
    if not allow_empty or any(present) or any(root.iterdir()):
        raise BootstrapError(
            f"Ahub root is incomplete; refusing replacement state: {root}"
        )
    return False


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.new")
    pending.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(pending, 0o600)
    os.replace(pending, path)


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": REGISTRY_SCHEMA,
            "ahub": None,
            "nodes": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"invalid bootstrap registry: {path}") from exc
    if value.get("schema_version") != REGISTRY_SCHEMA:
        raise BootstrapError("unsupported bootstrap registry schema")
    if not isinstance(value.get("nodes"), dict):
        raise BootstrapError("bootstrap registry nodes must be an object")
    ahub = value.get("ahub")
    if ahub is not None:
        if not isinstance(ahub, dict):
            raise BootstrapError("bootstrap registry Ahub must be an object")
        root = Path(str(ahub.get("root", "")))
        if not root.is_absolute() or not ahub.get("url"):
            raise BootstrapError("registered Ahub root and URL are required")
        validate_service_name(
            str(ahub.get("service", "")),
            allow_empty=not bool(ahub.get("managed")),
        )
    for agent_id, record in value["nodes"].items():
        if validate_agent_id(str(agent_id)) != agent_id:
            raise BootstrapError("registered Agent ID is not normalized")
        if not isinstance(record, dict):
            raise BootstrapError("registered node must be an object")
        home = Path(str(record.get("home", "")))
        if not home.is_absolute() or not record.get("node_id"):
            raise BootstrapError(
                f"registered node home and Node ID are required: {agent_id}"
            )
        validate_service_name(str(record.get("service", "")))
        try:
            port = int(record.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise BootstrapError("registered node port is invalid") from exc
        if not 1 <= port <= 65535:
            raise BootstrapError("registered node port is invalid")
    return value


def systemd_user_available() -> None:
    state = run(
        ["systemctl", "--user", "is-system-running"],
        allow_failure=True,
        timeout=30,
    )
    if state not in {"running", "degraded"}:
        raise BootstrapError(
            "the WSL systemd user manager is unavailable; enable systemd "
            "and a user session before persistent bootstrap"
        )


def discover_ahub_units() -> list[str]:
    raw = run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
            "--plain",
            "--no-legend",
        ],
        allow_failure=True,
        timeout=30,
    )
    result: list[str] = []
    for line in raw.splitlines():
        fields = line.split()
        if not fields or not fields[0].endswith(".service"):
            continue
        unit = fields[0]
        command = run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ExecStart",
                "--value",
            ],
            allow_failure=True,
            timeout=30,
        )
        if "ahub-serve" in command:
            result.append(unit)
    return sorted(set(result))


def systemd_quote(value: str | Path) -> str:
    text = str(value)
    if any(character in text for character in "\r\n\0"):
        raise BootstrapError("systemd argument contains a control character")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_unit(unit_dir: Path, name: str, content: str) -> Path:
    path = unit_dir / name
    pending = path.with_name(f".{path.name}.new")
    pending.write_text(content, encoding="utf-8")
    os.chmod(pending, 0o600)
    os.replace(pending, path)
    return path


def ahub_unit(cli: Path, root: Path, host: str, port: int) -> str:
    return f"""[Unit]
Description=Anet host-local Ahub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
ExecStart={systemd_quote(cli)} ahub-serve --root {systemd_quote(root)} --host {host} --port {port}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={systemd_quote(root)}

[Install]
WantedBy=default.target
"""


def node_unit(
    cli: Path,
    home: Path,
    state_root: Path,
    agent_id: str,
    ahub_service: str = AHUB_SERVICE,
) -> str:
    dependencies = (
        f"network-online.target {ahub_service}"
        if ahub_service
        else "network-online.target"
    )
    return f"""[Unit]
Description=Anet node for local Agent {agent_id}
After={dependencies}
Wants=network-online.target

[Service]
Type=simple
UMask=0077
Environment=PYTHONUNBUFFERED=1
ExecStart={systemd_quote(cli)} --home {systemd_quote(home)} serve
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={systemd_quote(state_root)}

[Install]
WantedBy=default.target
"""


def health(url: str) -> bool:
    endpoint = url.rstrip("/") + "/healthz"
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False
    return (
        response.status == 200
        and value.get("service") == "anet-ahub"
        and value.get("status") == "ok"
    )


def wait_for_health(url: str, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health(url):
            return
        time.sleep(0.25)
    raise BootstrapError(f"Ahub did not become healthy: {url}")


def choose_port(used: set[int], start: int = 43101, end: int = 43200) -> int:
    for port in range(start, end):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise BootstrapError("no unused Anet node port is available")


def complete_node(cli: Path, home: Path) -> dict[str, Any]:
    markers = (home / "identity.json", home / "config.json")
    if all(path.is_file() for path in markers):
        status = run_json([str(cli), "--home", str(home), "status"])
        node_id = str(status.get("node_id", "")).strip()
        if not node_id:
            raise BootstrapError(f"node status has no Node ID: {home}")
        return {"node_id": node_id, "status": status}
    if home.exists() and any(home.iterdir()):
        raise BootstrapError(
            f"refusing to initialize a non-empty incomplete node home: {home}"
        )
    return {}


def ensure_ahub_carrier(
    cli: Path,
    home: Path,
    ahub_url: str,
    peer_id: str,
) -> None:
    carriers = run_json([str(cli), "--home", str(home), "carrier-list"])
    for carrier in carriers:
        if (
            carrier.get("type") == "ahub"
            and carrier.get("base_url") == ahub_url
            and peer_id in carrier.get("peers", [])
        ):
            return
    suffix = hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:12]
    run_json(
        [
            str(cli),
            "--home",
            str(home),
            "carrier-add",
            ahub_url,
            "--type",
            "ahub",
            "--name",
            f"host-ahub-{suffix}",
            "--peer",
            peer_id,
            "--mode",
            "always",
            "--allow-insecure-http",
        ]
    )


def write_mcp_config(
    path: Path,
    python: Path,
    home: Path,
    agent_id: str,
    peer_ids: list[str],
) -> None:
    peers = ",".join(sorted(peer_ids))
    value = {
        "mcpServers": {
            "anet": {
                "command": str(python),
                "args": ["-m", "anet", "mcp"],
                "env": {
                    "ANET_HOME": str(home),
                    "ANET_AGENT_ID": agent_id,
                    "ANET_MCP_GROUP_PREFIX": f"local.{agent_id}.",
                    "ANET_MCP_KIND_PREFIX": "agent.task.",
                    "ANET_MCP_ALLOWED_PEERS": peers,
                    "ANET_MCP_TASK_ALLOWED_SENDERS": peers,
                    "ANET_MCP_TASK_CAPABILITIES": "",
                    "ANET_MCP_ALLOW_RAW_INBOX": "0",
                    "ANET_MCP_ALLOW_RELATION_MODEL": "0",
                    "ANET_MCP_ALLOW_RELATION_ACTIVITY": "0",
                    "ANET_MCP_ALLOW_RELATION_DISCLOSURE": "0",
                },
            }
        }
    }
    atomic_json(path, value)


def ensure_runtime(script_dir: Path) -> dict[str, Any]:
    raw = run(
        [
            sys.executable,
            str(script_dir / "install.py"),
            "--feature",
            "full",
        ],
        timeout=600,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError("runtime installer did not return JSON") from exc
    if (
        value.get("feature") != "full"
        or value.get("mcp_import") != "ok"
        or value.get("ahub_import") != "ok"
        or value.get("identity_files") != 0
    ):
        raise BootstrapError("full runtime verification failed")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Install Anet and idempotently attach one WSL Agent to the "
            "single host-local Ahub."
        )
    )
    result.add_argument(
        "--agent-id",
        default=os.environ.get("ANET_AGENT_ID", ""),
        help="Stable, profile-scoped local Agent identifier.",
    )
    result.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local" / "state" / "anet",
    )
    result.add_argument(
        "--config-root",
        type=Path,
        default=Path.home() / ".config" / "anet",
    )
    result.add_argument("--ahub-root", type=Path)
    result.add_argument("--ahub-url")
    result.add_argument(
        "--ahub-service",
        default="",
        help="Existing systemd user unit that owns an explicit Ahub.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if not is_wsl():
        raise BootstrapError("host bootstrap is supported only inside WSL")
    if fcntl is None:
        raise BootstrapError("POSIX file locking is unavailable")
    agent_id = validate_agent_id(args.agent_id)
    systemd_user_available()

    runtime = ensure_runtime(Path(__file__).resolve().parent)
    cli = Path(runtime["cli"]).resolve()
    python = Path(runtime["python"]).resolve()
    state_root = private_directory(args.state_root)
    config_root = private_directory(args.config_root)
    unit_dir = private_directory(
        Path.home() / ".config" / "systemd" / "user"
    )
    registry_path = config_root / "bootstrap.json"
    lock_path = config_root / "bootstrap.lock"

    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        registry = load_registry(registry_path)
        explicit_ahub = bool(
            args.ahub_root or args.ahub_url or args.ahub_service
        )
        if explicit_ahub and not (args.ahub_root and args.ahub_url):
            raise BootstrapError(
                "--ahub-root and --ahub-url must be supplied together"
            )

        if explicit_ahub:
            ahub_root = args.ahub_root.expanduser().resolve()
            ahub_url = validate_ahub_url(str(args.ahub_url))
            managed_ahub = False
            ahub_service = validate_service_name(
                str(args.ahub_service),
                allow_empty=True,
            )
        elif registry.get("ahub"):
            ahub_root = Path(registry["ahub"]["root"]).resolve()
            ahub_url = validate_ahub_url(str(registry["ahub"]["url"]))
            managed_ahub = bool(registry["ahub"].get("managed"))
            ahub_service = validate_service_name(
                str(registry["ahub"].get("service", "")),
                allow_empty=not managed_ahub,
            )
        else:
            ahub_root = private_directory(state_root / "ahub")
            ahub_url = validate_ahub_url("http://127.0.0.1:8422")
            managed_ahub = True
            ahub_service = AHUB_SERVICE

        existing_health = health(ahub_url)
        detected_units = discover_ahub_units()
        expected_unit = ahub_service
        unexpected_units = [
            unit for unit in detected_units if unit != expected_unit
        ]
        if unexpected_units:
            raise BootstrapError(
                "another Ahub service already exists; refusing a second Ahub: "
                + ", ".join(unexpected_units)
            )
        has_ahub_state = existing_ahub_state(
            ahub_root,
            allow_empty=not (registry.get("ahub") or explicit_ahub),
        )
        ahub_created = not has_ahub_state
        if has_ahub_state:
            run_json([str(cli), "ahub-status", "--root", str(ahub_root)])
        elif existing_health:
            raise BootstrapError(
                "an unmanaged Ahub already occupies the endpoint; supply its "
                "deployment-owned --ahub-root and --ahub-url"
            )
        else:
            known_unit_file = run(
                [
                    "systemctl",
                    "--user",
                    "list-unit-files",
                    "--no-legend",
                    "anet-ahub*.service",
                ],
                allow_failure=True,
            )
            if known_unit_file:
                raise BootstrapError(
                    "an Ahub unit exists without complete registered state; "
                    "refusing to overwrite it or start a second Ahub"
                )

        if not existing_health:
            if not managed_ahub:
                raise BootstrapError(
                    "the operator-supplied Ahub state exists but its service "
                    "is not healthy; start the owning service explicitly"
                )
            install_unit(
                unit_dir,
                AHUB_SERVICE,
                ahub_unit(cli, ahub_root, "127.0.0.1", 8422),
            )
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "enable", "--now", AHUB_SERVICE])
            wait_for_health(ahub_url)

        registry["ahub"] = {
            "root": str(ahub_root),
            "url": ahub_url,
            "service": ahub_service,
            "managed": managed_ahub,
        }

        nodes = registry["nodes"]
        record = nodes.get(agent_id)
        home = (
            Path(record["home"]).resolve()
            if record
            else (state_root / "nodes" / agent_id).resolve()
        )
        observed = complete_node(cli, home)
        created = False
        if record and not observed:
            raise BootstrapError(
                "registered node home is missing; refusing replacement identity"
            )
        if observed:
            node_id = observed["node_id"]
            if record and record.get("node_id") != node_id:
                raise BootstrapError("registered Node ID does not match node home")
            port = int(record.get("port", 0)) if record else 0
        else:
            used = {
                int(item.get("port", 0))
                for item in nodes.values()
                if int(item.get("port", 0)) > 0
            }
            port = choose_port(used)
            initialized = run_json(
                [
                    str(cli),
                    "--home",
                    str(home),
                    "init",
                    "--label",
                    agent_id,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ]
            )
            node_id = str(initialized["node_id"])
            run_json([str(cli), "--home", str(home), "doctor"])
            created = True

        service_name = f"anet-node-{agent_id}.service"
        nodes[agent_id] = {
            "home": str(home),
            "node_id": node_id,
            "port": port,
            "service": service_name,
        }

        for item in nodes.values():
            item_home = Path(item["home"]).resolve()
            current = complete_node(cli, item_home)
            if not current or current["node_id"] != item["node_id"]:
                raise BootstrapError(
                    f"registered node is incomplete or mismatched: {item_home}"
                )
            run_json(
                [
                    str(cli),
                    "ahub-allow",
                    "--root",
                    str(ahub_root),
                    item["node_id"],
                ]
            )

        node_items = list(nodes.items())
        for left_id, left in node_items:
            left_home = Path(left["home"]).resolve()
            for right_id, right in node_items:
                if left_id == right_id:
                    continue
                right_home = Path(right["home"]).resolve()
                card = right_home / "card.json"
                run_json(
                    [str(cli), "--home", str(left_home), "peer-add", str(card)]
                )
                ensure_ahub_carrier(
                    cli,
                    left_home,
                    ahub_url,
                    str(right["node_id"]),
                )

        for item_id, item in node_items:
            item_home = Path(item["home"]).resolve()
            install_unit(
                unit_dir,
                item["service"],
                node_unit(
                    cli,
                    item_home,
                    state_root,
                    item_id,
                    ahub_service,
                ),
            )
            peers = [
                str(other["node_id"])
                for other_id, other in node_items
                if other_id != item_id
            ]
            write_mcp_config(
                config_root / "agents" / item_id / "mcp-stdio.json",
                python,
                item_home,
                item_id,
                peers,
            )

        atomic_json(registry_path, registry)
        run(["systemctl", "--user", "daemon-reload"])
        for _, item in node_items:
            run(
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "--now",
                    item["service"],
                ]
            )
            run(["systemctl", "--user", "restart", item["service"]])
        service_state = run(
            ["systemctl", "--user", "is-active", service_name]
        )
        if service_state != "active":
            raise BootstrapError(f"node service is not active: {service_name}")

        result = {
            "outcome": "created" if created else "reused",
            "platform": "wsl",
            "agent_id": agent_id,
            "runtime": runtime["runtime"],
            "cli": str(cli),
            "python": str(python),
            "ahub": {
                "url": ahub_url,
                "root": str(ahub_root),
                "service": registry["ahub"]["service"],
                "reused": not ahub_created,
            },
            "node": {
                "home": str(home),
                "node_id": node_id,
                "service": service_name,
                "port": port,
            },
            "local_agents": len(nodes),
            "mcp_config": str(
                config_root / "agents" / agent_id / "mcp-stdio.json"
            ),
            "identity_files": 1,
        }
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        print(
            json.dumps(
                {"outcome": "failed", "error": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
