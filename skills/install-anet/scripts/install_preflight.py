#!/usr/bin/env python3
"""Small self-contained preflight used by the distributable Linux Skill."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO


_RUNTIME_MARKERS = ("current", "versions", "release.json")
_ALL_MARKERS = _RUNTIME_MARKERS + (
    "nodes",
    "config.json",
    "identity.json",
    "card.json",
    "remote-control.json",
)
_AHUB_MARKERS = ("ahub.sqlite3", "control.sqlite3", "config.json")
_ANET_PROCESS = re.compile(
    r"(?:\banet-fabric\b|(?:^|\s)-m\s+anet(?:\s|$)|"
    r"\banet\s+(?:supervisor|serve)\b)",
    re.IGNORECASE,
)
_AHUB_PROCESS = re.compile(r"\bahub(?:-serve|\s+serve)\b", re.IGNORECASE)


def _resolve(value: Path) -> Path:
    return value.expanduser().resolve(strict=False)


def _run(command: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return completed.returncode, (completed.stdout or "").strip()


def _finding(root: Path, markers: tuple[str, ...]) -> dict[str, Any] | None:
    if not root.is_dir() and not root.is_symlink():
        return None
    present = [
        marker
        for marker in markers
        if (root / marker).exists() or (root / marker).is_symlink()
    ]
    if not present:
        return None
    return {
        "path": str(root),
        "markers": present,
        "persistent": bool(
            set(present)
            & {"nodes", "config.json", "identity.json", "card.json", "remote-control.json"}
        ),
    }


def _unique(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = _resolve(value)
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def collect(
    platform_name: str,
    target_root: Path,
    *,
    extra_ahub_roots: list[Path] | None = None,
    include_services: bool = True,
    include_processes: bool = True,
    include_persistent_markers: bool = True,
) -> dict[str, Any]:
    target = _resolve(target_root)
    home = Path.home()
    roots = [target, home / ".local" / "anet"]
    if platform_name != "termux":
        roots.extend((Path("/opt/anet"), Path("/var/lib/anet")))
    marker_set = _ALL_MARKERS if include_persistent_markers else _RUNTIME_MARKERS
    anet = [
        finding
        for root in _unique(roots)
        if (finding := _finding(root, marker_set)) is not None
    ]
    ahub_roots = [
        target / "ahub",
        target / "ahub-data",
        home / ".local" / "ahub",
        home / ".config" / "anet" / "ahub",
        Path("/var/lib/anet-ahub"),
        Path("/var/lib/ahub"),
        *(extra_ahub_roots or []),
    ]
    ahub = [
        finding
        for root in _unique(ahub_roots)
        if (finding := _finding(root, _AHUB_MARKERS)) is not None
    ]
    services: list[dict[str, Any]] = []
    if include_services and shutil.which("systemctl"):
        for name, kind in (
            ("anet-supervisor.service", "anet"),
            ("anet-ahub.service", "ahub"),
            ("ahub.service", "ahub"),
        ):
            active_code, active = _run(["systemctl", "--user", "is-active", name])
            enabled_code, enabled = _run(["systemctl", "--user", "is-enabled", name])
            if active_code == 0 or enabled_code == 0:
                services.append(
                    {
                        "kind": kind,
                        "name": name,
                        "active": active if active_code == 0 else "unknown",
                        "enabled": enabled if enabled_code == 0 else "unknown",
                    }
                )
        code, unit_output = _run(
            ["systemctl", "--user", "list-unit-files", "--no-legend"]
        )
        if code == 0:
            known = {str(item["name"]) for item in services}
            for line in unit_output.splitlines():
                name = line.split(None, 1)[0] if line.split(None, 1) else ""
                if name.startswith("anet-node-") and name.endswith(".service"):
                    if name not in known:
                        services.append(
                            {
                                "kind": "anet",
                                "name": name,
                                "active": "unknown",
                                "enabled": "present",
                            }
                        )
    processes: list[dict[str, Any]] = []
    if include_processes and shutil.which("ps"):
        code, output = _run(["ps", "-eo", "pid=,args="], timeout=15)
        if code == 0:
            current_pid = str(os.getpid())
            for line in output.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or parts[0] == current_pid:
                    continue
                ahub_match = bool(_AHUB_PROCESS.search(parts[1]))
                anet_match = bool(_ANET_PROCESS.search(parts[1]))
                if ahub_match or anet_match:
                    processes.append(
                        {
                            "kind": "ahub" if ahub_match and not anet_match else "anet",
                            "pid": int(parts[0]) if parts[0].isdigit() else parts[0],
                        }
                    )
    target_finding = next(
        (item for item in anet if _resolve(Path(item["path"])) == target),
        None,
    )
    return {
        "schema_version": 1,
        "platform": platform_name,
        "target_root": str(target),
        "target": target_finding,
        "existing_anet": anet,
        "existing_ahub": ahub,
        "services": services,
        "processes": processes,
    }


def emit(report: dict[str, Any], *, stream: TextIO | None = None) -> None:
    print(
        "Anet install preflight: "
        + json.dumps(report, ensure_ascii=False, separators=(",", ":")),
        file=stream or sys.stderr,
    )
