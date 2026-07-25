#!/usr/bin/env python3
"""Install a versioned Anet runtime without creating or inspecting nodes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class InstallError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def run(command: list[str], *, timeout: int = 300) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise InstallError(f"command failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def python_in(venv: Path) -> Path:
    return venv / "bin" / "python"


def cli_in(venv: Path) -> Path:
    return venv / "bin" / "anet"


def verify_runtime(venv: Path, version: str, feature: str) -> None:
    python = python_in(venv)
    cli = cli_in(venv)
    if not python.is_file() or not cli.is_file():
        raise InstallError("installed runtime is incomplete")
    observed = run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m;"
                "print(m.version('anet-fabric'))"
            ),
        ]
    )
    if observed != version:
        raise InstallError(
            f"runtime version mismatch: expected {version}, got {observed}"
        )
    if run([str(cli), "--version"]) != f"Anet {version}":
        raise InstallError("Anet CLI version mismatch")
    if feature in {"mcp", "full"}:
        run([str(python), "-c", "import mcp"])
    if feature == "full":
        run([str(python), "-c", "import uvicorn, websockets"])


def install_runtime(
    *,
    platform_name: str,
    version: str,
    wheel: Path,
    wheel_sha256: str,
    root: Path,
    feature: str,
) -> dict[str, str]:
    wheel = wheel.expanduser().resolve()
    if not wheel.is_file():
        raise InstallError(f"wheel does not exist: {wheel}")
    expected_hash = wheel_sha256.strip().upper()
    if sha256(wheel) != expected_hash:
        raise InstallError("wheel SHA256 mismatch")

    root = root.expanduser().resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise InstallError("install root is too broad")
    versions = root / "versions"
    release_name = version if feature == "core" else f"{version}-{feature}"
    destination = versions / release_name
    manifest = destination / "release.json"
    versions.mkdir(parents=True, exist_ok=True)

    outcome = "installed"
    if destination.exists():
        if not manifest.is_file():
            raise InstallError("existing version directory has no release manifest")
        current = json.loads(manifest.read_text(encoding="utf-8"))
        if current.get("wheel_sha256") != expected_hash:
            raise InstallError("existing version has a different wheel hash")
        if current.get("feature", "core") != feature:
            raise InstallError("existing version has a different feature set")
        verify_runtime(destination / "venv", version, feature)
        outcome = "reused"
    else:
        try:
            destination.mkdir()
            venv = destination / "venv"
            uv = shutil.which("uv")
            extras = {
                "core": "",
                "mcp": "mcp",
                "full": "mcp,ahub,qr",
            }[feature]
            requirement = (
                str(wheel)
                if not extras
                else f"anet-fabric[{extras}] @ {wheel.as_uri()}"
            )
            if uv:
                run([uv, "venv", "--python", sys.executable, str(venv)])
                run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(python_in(venv)),
                        requirement,
                    ]
                )
            else:
                run([sys.executable, "-m", "venv", str(venv)])
                run(
                    [
                        str(python_in(venv)),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        requirement,
                    ]
                )
            verify_runtime(venv, version, feature)
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "platform": platform_name,
                        "version": version,
                        "feature": feature,
                        "wheel_sha256": expected_hash,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(manifest, 0o600)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    current_link = root / "current"
    pending_link = root / ".current.new"
    pending_link.unlink(missing_ok=True)
    pending_link.symlink_to(destination, target_is_directory=True)
    os.replace(pending_link, current_link)
    return {
        "outcome": outcome,
        "platform": platform_name,
        "version": version,
        "feature": feature,
        "runtime": str(destination / "venv"),
        "cli": str(current_link / "venv" / "bin" / "anet"),
    }


def parser(default_root: Path) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Install a clean Anet platform runtime. This does not create a "
            "node, integrate an Agent runtime, or add a service."
        )
    )
    result.add_argument("--version", required=True)
    result.add_argument("--wheel", type=Path, required=True)
    result.add_argument("--wheel-sha256", required=True)
    result.add_argument("--root", type=Path, default=default_root)
    result.add_argument(
        "--feature",
        choices=("core", "mcp", "full"),
        default="core",
        help="core CLI, CLI plus MCP, or CLI plus MCP and Ahub dependencies",
    )
    return result


def main(platform_name: str, default_root: Path) -> int:
    args = parser(default_root).parse_args()
    result = install_runtime(
        platform_name=platform_name,
        version=args.version,
        wheel=args.wheel,
        wheel_sha256=args.wheel_sha256,
        root=args.root,
        feature=args.feature,
    )
    print(json.dumps(result, separators=(",", ":")))
    return 0
