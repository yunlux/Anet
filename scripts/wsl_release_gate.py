#!/usr/bin/env python3
"""Deterministic Anet release gate for a WSL systemd user service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class GateError(RuntimeError):
    """A release invariant failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_pytest_count(output: str) -> int:
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:\s|,|$)", str(output))
    if not matches:
        raise GateError("pytest output does not contain a passed count")
    return int(matches[-1])


def verify_status_transition(
    before: dict[str, int], after: dict[str, int]
) -> None:
    """Reject new security failures without requiring a live queue to be empty."""
    for key in ("rejections", "untrusted"):
        old = int(before.get(key, 0))
        new = int(after.get(key, 0))
        if new > old:
            raise GateError(
                f"runtime status regression: {key} increased from {old} to {new}"
            )


def safe_extract_sdist(archive: Path, destination: Path) -> Path:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        for member in members:
            candidate = (destination / member.name).resolve()
            if candidate != destination and destination not in candidate.parents:
                raise GateError(f"sdist contains an unsafe path: {member.name}")
            if member.issym() or member.islnk():
                raise GateError(f"sdist contains a link: {member.name}")
        source.extractall(destination, members=members, filter="data")
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
        raise GateError("sdist must contain exactly one project root")
    return roots[0]


def atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ReleaseGate:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.wheel = args.wheel.resolve()
        self.sdist = args.sdist.resolve()
        self.rollback_wheel = args.rollback_wheel.resolve()
        self.venv = args.venv.resolve()
        self.node_home = args.node_home.resolve()
        self.python = self.venv / "bin" / "python"
        self.anet = self.venv / "bin" / "anet"
        self.deployment_started = False
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "target_version": args.version,
            "mode": "dry-run" if args.dry_run else "deploy",
            "started_utc": datetime.now(UTC).isoformat(),
            "outcome": "running",
            "rollback": {"attempted": False, "succeeded": False},
            "steps": [],
        }

    def record(self, name: str, **details: Any) -> None:
        self.report["steps"].append({"name": name, **details})

    def run(
        self,
        command: list[str | Path],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> str:
        values = [str(item) for item in command]
        completed = subprocess.run(
            values,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout or self.args.command_timeout,
            check=False,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise GateError(
                f"command failed ({completed.returncode}): {values[0]}: {tail}"
            )
        return completed.stdout.strip()

    def json_command(self, command: list[str | Path]) -> Any:
        raw = self.run(command)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GateError(f"command did not return JSON: {command[0]}") from exc

    def version_info(self) -> dict[str, str]:
        raw = self.run(
            [
                self.python,
                "-c",
                (
                    "import anet,importlib.metadata as m,json;"
                    "print(json.dumps({'distribution':m.version('anet-fabric'),"
                    "'module':anet.__version__,'import_path':anet.__file__}))"
                ),
            ]
        )
        return json.loads(raw)

    def protected_hashes(self) -> dict[str, str]:
        names = (
            "identity.json",
            "card.json",
            "config.json",
            "peers.json",
            "relationships.json",
            "relationship-claims.json",
            "relationship-disclosures.json",
            "relationship-disclosure-schedules.json",
            "relationship-disclosure-gap-notices.json",
            "relationship-disclosure-archive.json",
            "tls-key.pem",
            "revocations.json",
        )
        return {
            name: sha256_file(self.node_home / name)
            for name in names
            if (self.node_home / name).is_file()
        }

    def permissions(self) -> dict[str, str]:
        result = {".": oct(stat.S_IMODE(self.node_home.stat().st_mode))}
        for name in (
            "identity.json",
            "card.json",
            "config.json",
            "peers.json",
            "relationships.json",
            "relationship-claims.json",
            "relationship-disclosures.json",
            "relationship-disclosure-schedules.json",
            "relationship-disclosure-gap-notices.json",
            "relationship-disclosure-archive.json",
            "tls-key.pem",
            "revocations.json",
            "anet.sqlite3",
            "anet.sqlite3-wal",
            "anet.sqlite3-shm",
        ):
            path = self.node_home / name
            if path.exists():
                result[name] = oct(stat.S_IMODE(path.stat().st_mode))
        return result

    @staticmethod
    def prekey_generations(status: dict[str, Any]) -> dict[str, Any]:
        prekeys = status.get("prekeys", {})
        local = prekeys.get("local", {}).get("by_peer", {})
        peers = prekeys.get("peers", {})
        return {
            "local": {
                key: int(value.get("generation", 0)) for key, value in local.items()
            },
            "peers": {
                key: int(value.get("generation", 0)) for key, value in peers.items()
            },
        }

    def revocations(self) -> list[dict[str, Any]]:
        help_text = self.run([self.anet, "--help"])
        if "peer-revocations" not in help_text:
            return []
        value = self.json_command(
            [self.anet, "--home", self.node_home, "peer-revocations"]
        )
        if not isinstance(value, list):
            raise GateError("peer-revocations did not return a list")
        return value

    def snapshot(self) -> dict[str, Any]:
        if self.run(["systemctl", "--user", "is-active", self.args.service]) != "active":
            raise GateError("Anet service is not active")
        status = self.json_command([self.anet, "--home", self.node_home, "status"])
        return {
            "version": self.version_info(),
            "service": self.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    self.args.service,
                    "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus",
                ]
            ),
            "node_id": str(status.get("node_id", "")),
            "status_gates": {
                key: int(status.get("store", {}).get(key, -1))
                for key in ("pending", "rejections", "untrusted")
            },
            "prekey_generations": self.prekey_generations(status),
            "peers": self.json_command(
                [self.anet, "--home", self.node_home, "peer-list"]
            ),
            "revocations": self.revocations(),
            "protected_hashes": self.protected_hashes(),
            "permissions": self.permissions(),
        }

    def validate_paths(self) -> None:
        for path, label in (
            (self.wheel, "wheel"),
            (self.sdist, "sdist"),
            (self.rollback_wheel, "rollback wheel"),
            (self.python, "persistent Python"),
            (self.anet, "persistent Anet CLI"),
            (self.node_home, "node home"),
        ):
            if not path.exists():
                raise GateError(f"{label} does not exist: {path}")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", self.args.service):
            raise GateError("invalid systemd service name")

    def verify_artifacts(self) -> None:
        actual_wheel = sha256_file(self.wheel)
        actual_sdist = sha256_file(self.sdist)
        if actual_wheel != self.args.wheel_sha256.upper():
            raise GateError("wheel SHA-256 mismatch")
        if actual_sdist != self.args.sdist_sha256.upper():
            raise GateError("sdist SHA-256 mismatch")
        self.record(
            "artifact_gate",
            wheel_sha256=actual_wheel,
            sdist_sha256=actual_sdist,
        )

    def isolated_verification(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        check = self.args.check_root.resolve() / f"{self.args.version}-{stamp}"
        check.mkdir(parents=True, exist_ok=False)
        project = safe_extract_sdist(self.sdist, check / "source")
        verify_venv = check / "venv"
        self.run([sys.executable, "-m", "venv", verify_venv], timeout=120)
        verify_python = verify_venv / "bin" / "python"
        self.run(
            [
                verify_python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                self.wheel,
                "pytest>=9.0",
                "pytest-asyncio>=1.0",
                "mcp>=1.0",
                "uvicorn>=0.35,<1",
                "websockets>=16.1,<17",
                "ruff==0.15.22",
            ],
            timeout=self.args.install_timeout,
        )
        pytest_output = self.run(
            [verify_python, "-m", "pytest", "-q"],
            cwd=project,
            timeout=self.args.test_timeout,
        )
        passed = parse_pytest_count(pytest_output)
        if passed != self.args.expected_tests:
            raise GateError(
                f"expected {self.args.expected_tests} passed tests, observed {passed}"
            )
        self.run(
            [
                verify_python,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "src",
                "tests",
                "scripts",
            ],
            cwd=project,
            timeout=self.args.test_timeout,
        )
        cli = verify_venv / "bin" / "anet"
        version_output = self.run([cli, "--version"])
        if version_output != f"Anet {self.args.version}":
            raise GateError("isolated CLI version mismatch")
        for command in ("pair-offer", "peer-revoke", "peer-revocations"):
            self.run([cli, command, "--help"])
        self.record(
            "isolated_verification",
            directory=str(check),
            tests_passed=passed,
            ruff="passed",
            cli_version=version_output,
        )

    def install(self, wheel: Path) -> None:
        uv = shutil.which("uv")
        if uv:
            self.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    self.python,
                    "--no-deps",
                    "--force-reinstall",
                    wheel,
                ],
                timeout=self.args.install_timeout,
            )
            return
        self.run(
            [
                self.python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--force-reinstall",
                wheel,
            ],
            timeout=self.args.install_timeout,
        )

    def restart(self) -> None:
        self.run(["systemctl", "--user", "restart", self.args.service])
        deadline = time.monotonic() + self.args.start_timeout
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", self.args.service],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.stdout.strip() == "active":
                return
            time.sleep(0.25)
        raise GateError("service did not become active before timeout")

    def verify_after(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        version = after["version"]
        if version["distribution"] != self.args.version or version["module"] != self.args.version:
            raise GateError("installed version mismatch")
        import_path = Path(version["import_path"]).resolve()
        if self.venv not in import_path.parents:
            raise GateError("installed Anet is not imported from the persistent venv")
        for key in (
            "node_id",
            "peers",
            "revocations",
            "protected_hashes",
            "prekey_generations",
        ):
            if after[key] != before[key]:
                raise GateError(f"protected runtime state changed: {key}")
        verify_status_transition(before["status_gates"], after["status_gates"])
        if after["permissions"].get(".") != "0o700":
            raise GateError("node directory must be mode 0700")
        for name, mode in after["permissions"].items():
            if name != "." and mode != "0o600":
                raise GateError(f"sensitive path is not mode 0600: {name}={mode}")

    def rollback(self) -> None:
        self.report["rollback"]["attempted"] = True
        try:
            self.install(self.rollback_wheel)
            self.restart()
            self.report["rollback"]["succeeded"] = True
        except BaseException as exc:
            self.report["rollback"]["error"] = str(exc)

    def execute(self) -> int:
        try:
            self.validate_paths()
            before = self.snapshot()
            self.report["before"] = before
            self.verify_artifacts()
            if self.args.dry_run:
                self.verify_after(before, before)
                self.report["after"] = before
                self.report["outcome"] = "dry-run-passed"
                return 0
            self.isolated_verification()
            self.deployment_started = True
            self.install(self.wheel)
            self.restart()
            after = self.snapshot()
            self.report["after"] = after
            self.verify_after(before, after)
            self.report["outcome"] = "deployed"
            return 0
        except BaseException as exc:
            self.report["outcome"] = "failed"
            self.report["error"] = f"{type(exc).__name__}: {exc}"
            if self.deployment_started:
                self.rollback()
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
    parser.add_argument("--node-home", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--expected-tests", type=int, required=True)
    parser.add_argument("--check-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=60)
    parser.add_argument("--install-timeout", type=int, default=300)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--start-timeout", type=int, default=15)
    return parser


def public_summary(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    after = report.get("after", {})
    return {
        "outcome": report.get("outcome", "unknown"),
        "target_version": report.get("target_version", ""),
        "installed_version": after.get("version", {}).get("distribution", ""),
        "status_gates": after.get("status_gates", {}),
        "rollback": report.get("rollback", {}),
        "report": str(Path(report_path).resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate = ReleaseGate(args)
    exit_code = gate.execute()
    print(json.dumps(public_summary(gate.report, args.report), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
