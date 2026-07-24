#!/usr/bin/env python3
"""Atomically release one platform runtime to explicit WSL node services."""

from __future__ import annotations

import argparse
import copy
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wsl_release_gate import (
    GateError,
    ReleaseGate,
    atomic_private_json,
)


def parse_deployment(value: str) -> tuple[Path, str]:
    home_text, separator, service = str(value).rpartition("=")
    if not separator or not home_text or not service:
        raise argparse.ArgumentTypeError(
            "deployment must be NODE_HOME=SYSTEMD_SERVICE"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", service):
        raise argparse.ArgumentTypeError("invalid systemd service name")
    return Path(home_text).expanduser(), service


def deployment_gate(
    args: argparse.Namespace, home: Path, service: str
) -> ReleaseGate:
    values = copy.copy(args)
    values.node_home = home
    values.service = service
    return ReleaseGate(values)


def safe_node_home(home: Path) -> Path:
    resolved = home.resolve()
    if resolved in {Path("/"), Path.home().resolve()}:
        raise GateError("node home is too broad")
    for name in ("identity.json", "config.json"):
        if not (resolved / name).is_file():
            raise GateError(f"node home is incomplete: {resolved}")
    for item in resolved.rglob("*"):
        if item.is_symlink():
            raise GateError(f"node home contains a symbolic link: {item}")
    return resolved


class MultiNodeRelease:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.deployments = [
            (safe_node_home(home), service)
            for home, service in args.deployment
        ]
        if len({home for home, _ in self.deployments}) != len(self.deployments):
            raise GateError("duplicate node home")
        if len({service for _, service in self.deployments}) != len(
            self.deployments
        ):
            raise GateError("duplicate systemd service")
        names = [home.name for home, _ in self.deployments]
        if len(set(names)) != len(names):
            raise GateError("node home basenames must be unique")
        first_home, first_service = self.deployments[0]
        self.primary = deployment_gate(args, first_home, first_service)
        self.backup_dir: Path | None = None
        self.deployment_started = False
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "target_version": args.version,
            "platform": "wsl-multi-node",
            "started_utc": datetime.now(UTC).isoformat(),
            "outcome": "running",
            "rollback": {"attempted": False, "succeeded": False},
            "deployments": [
                {"home": str(home), "service": service}
                for home, service in self.deployments
            ],
        }

    def gates(self) -> list[ReleaseGate]:
        return [
            deployment_gate(self.args, home, service)
            for home, service in self.deployments
        ]

    def stop_all(self) -> None:
        for _, service in self.deployments:
            self.primary.run(["systemctl", "--user", "stop", service])
        for _, service in self.deployments:
            completed = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                text=True,
                capture_output=True,
                check=False,
            )
            state = completed.stdout.strip()
            if state != "inactive":
                raise GateError(f"service did not stop: {service}")

    def start_all(self) -> None:
        for _, service in self.deployments:
            self.primary.run(["systemctl", "--user", "start", service])
        deadline = time.monotonic() + self.args.start_timeout
        pending = {service for _, service in self.deployments}
        while pending and time.monotonic() < deadline:
            for service in tuple(pending):
                completed = subprocess.run(
                    ["systemctl", "--user", "is-active", service],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if completed.stdout.strip() == "active":
                    pending.remove(service)
            if pending:
                time.sleep(0.25)
        if pending:
            raise GateError(
                "services did not become active: " + ", ".join(sorted(pending))
            )

    def backup(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self.args.backup_root.resolve() / (
            f"anet-{self.args.version}-{stamp}"
        )
        destination.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(destination, 0o700)
        for home, _ in self.deployments:
            shutil.copytree(
                home,
                destination / home.name,
                copy_function=shutil.copy2,
            )
        self.backup_dir = destination
        self.report["backup_dir"] = str(destination)

    def restore_backup(self) -> None:
        if self.backup_dir is None:
            raise GateError("release backup is unavailable")
        failed_root = self.backup_dir / "failed-upgrade-state"
        failed_root.mkdir(mode=0o700, exist_ok=False)
        for home, _ in self.deployments:
            os.replace(home, failed_root / home.name)
            shutil.copytree(
                self.backup_dir / home.name,
                home,
                copy_function=shutil.copy2,
            )

    def execute(self) -> int:
        try:
            for gate in self.gates():
                gate.validate_paths()
            before = {
                service: gate.snapshot()
                for gate, (_, service) in zip(
                    self.gates(), self.deployments, strict=True
                )
            }
            self.report["before"] = before
            self.primary.verify_artifacts()
            self.primary.isolated_verification()
            if self.args.dry_run:
                self.report["after"] = before
                self.report["outcome"] = "dry-run-passed"
                return 0
            self.deployment_started = True
            self.stop_all()
            self.backup()
            self.primary.install(self.primary.wheel)
            self.start_all()
            after = {
                service: gate.snapshot()
                for gate, (_, service) in zip(
                    self.gates(), self.deployments, strict=True
                )
            }
            for gate, (_, service) in zip(
                self.gates(), self.deployments, strict=True
            ):
                gate.verify_after(before[service], after[service])
            self.report["after"] = after
            self.report["outcome"] = "deployed"
            return 0
        except BaseException as exc:
            self.report["outcome"] = "failed"
            self.report["error"] = f"{type(exc).__name__}: {exc}"
            if self.deployment_started:
                self.report["rollback"]["attempted"] = True
                try:
                    self.stop_all()
                    self.primary.install(self.primary.rollback_wheel)
                    if self.backup_dir is not None:
                        self.restore_backup()
                    self.start_all()
                    self.report["rollback"]["succeeded"] = True
                except BaseException as rollback_exc:
                    self.report["rollback"]["error"] = (
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            return 1
        finally:
            self.report["finished_utc"] = datetime.now(UTC).isoformat()
            atomic_private_json(self.args.report, self.report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--rollback-wheel", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument(
        "--deployment",
        action="append",
        type=parse_deployment,
        required=True,
        help="repeat NODE_HOME=SYSTEMD_SERVICE for each existing node",
    )
    parser.add_argument("--expected-tests", type=int, required=True)
    parser.add_argument("--check-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=60)
    parser.add_argument("--install-timeout", type=int, default=300)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--start-timeout", type=int, default=15)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.deployment:
        raise SystemExit("at least one deployment is required")
    release = MultiNodeRelease(args)
    code = release.execute()
    print(
        f"outcome={release.report['outcome']} "
        f"target={args.version} nodes={len(release.deployments)}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
