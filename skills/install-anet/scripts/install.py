#!/usr/bin/env python3
"""Install the Skill-bundled Anet release into an isolated Linux runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

VERSION = "0.12.1"
DEFAULT_FEATURE = "mcp"
WHEEL_NAME = "anet_fabric-0.12.1-py3-none-any.whl"
WHEEL_SHA256 = (
    "6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7"
)


class InstallError(RuntimeError):
    pass


def load_preflight() -> tuple[Any, Any]:
    path = Path(__file__).with_name("install_preflight.py")
    spec = importlib.util.spec_from_file_location(
        "anet_skill_install_preflight",
        path,
    )
    if spec is None or spec.loader is None:
        raise InstallError("Skill preflight module is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect, module.emit


collect, emit = load_preflight()


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
        raise InstallError(
            f"command failed ({completed.returncode}): {detail.strip()}"
        )
    return completed.stdout.strip()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest().upper()


def verify(venv: Path, feature: str) -> None:
    python = venv / "bin" / "python"
    cli = venv / "bin" / "anet"
    if not python.is_file() or not cli.is_file():
        raise InstallError("runtime is incomplete")
    version = run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m;print(m.version('anet-fabric'))",
        ]
    )
    if version != VERSION:
        raise InstallError(
            f"runtime version mismatch: expected {VERSION}, got {version}"
        )
    if run([str(cli), "--version"]) != f"Anet {VERSION}":
        raise InstallError("CLI version mismatch")
    run([str(python), "-c", "import anet.mcp_server, mcp"])
    if feature == "full":
        run([str(python), "-c", "import anet.ahub, uvicorn, websockets"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install the pinned Anet Linux runtime."
    )
    result.add_argument(
        "--feature",
        choices=("mcp", "full"),
        default=DEFAULT_FEATURE,
        help="Install MCP only, or MCP plus the optional Ahub runtime.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    feature = str(args.feature)
    if sys.platform != "linux":
        raise InstallError("this Skill supports Linux only")
    if sys.version_info < (3, 11):
        raise InstallError("Python 3.11 or newer is required")

    skill_dir = Path(__file__).resolve().parents[1]
    wheel = skill_dir / "assets" / WHEEL_NAME
    if not wheel.is_file():
        raise InstallError("bundled Anet wheel is missing")
    if digest(wheel) != WHEEL_SHA256:
        raise InstallError("bundled Anet wheel SHA256 mismatch")

    root = (Path.home() / ".local" / "anet").resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise InstallError("install root is too broad")
    preflight = collect(
        "linux",
        root,
        include_services=False,
        include_processes=False,
        include_persistent_markers=False,
    )
    emit(preflight)
    versions = root / "versions"
    destination = versions / f"{VERSION}-{feature}"
    venv = destination / "venv"
    manifest = destination / "release.json"
    versions.mkdir(parents=True, exist_ok=True)
    outcome = "installed"

    if destination.exists():
        if not manifest.is_file():
            raise InstallError(
                "existing version directory has no release manifest"
            )
        release = json.loads(manifest.read_text(encoding="utf-8"))
        expected = {
            "version": VERSION,
            "feature": feature,
            "wheel_sha256": WHEEL_SHA256,
        }
        if any(release.get(key) != value for key, value in expected.items()):
            raise InstallError("existing release manifest does not match Skill")
        verify(venv, feature)
        outcome = "reused"
    else:
        try:
            destination.mkdir()
            uv = shutil.which("uv")
            if uv:
                run([uv, "venv", "--python", sys.executable, str(venv)])
                run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(venv / "bin" / "python"),
                        (
                            f"anet-fabric[mcp,ahub,qr] @ {wheel.as_uri()}"
                            if feature == "full"
                            else f"anet-fabric[mcp] @ {wheel.as_uri()}"
                        ),
                    ]
                )
            else:
                run([sys.executable, "-m", "venv", str(venv)])
                run(
                    [
                        str(venv / "bin" / "python"),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        (
                            f"anet-fabric[mcp,ahub,qr] @ {wheel.as_uri()}"
                            if feature == "full"
                            else f"anet-fabric[mcp] @ {wheel.as_uri()}"
                        ),
                    ]
                )
            verify(venv, feature)
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "platform": "linux",
                        "version": VERSION,
                        "feature": feature,
                        "wheel_sha256": WHEEL_SHA256,
                        "installed_by": "install-anet-skill",
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

    current = root / "current"
    pending = root / ".current.new"
    pending.unlink(missing_ok=True)
    pending.symlink_to(destination, target_is_directory=True)
    os.replace(pending, current)
    identity_files = len(list(destination.rglob("identity.json")))
    result = {
        "outcome": outcome,
        "version": VERSION,
        "feature": feature,
        "runtime": str(venv),
        "python": str(current / "venv" / "bin" / "python"),
        "cli": str(current / "venv" / "bin" / "anet"),
        "identity_files": identity_files,
        "mcp_import": "ok",
        "ahub_import": "ok" if feature == "full" else "not-installed",
        "preflight": preflight,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(
            json.dumps(
                {"outcome": "failed", "error": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
