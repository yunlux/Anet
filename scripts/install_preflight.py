#!/usr/bin/env python3
"""Read-only local preflight checks shared by Anet installers.

The installer must be able to answer two different questions before it makes
changes:

* is the requested target already an Anet deployment that can be reused; and
* is there another deployment in the same platform boundary that would make a
  second node or supervisor surprising?

This module deliberately checks a small set of deployment-owned locations and
well-known service managers.  It does not recursively scan a home directory,
the filesystem, or another platform such as Windows from inside WSL.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO


class PreflightConflict(RuntimeError):
    """Raised when a deployment would create a second local installation."""


class InstallationLock:
    """Serialize installers targeting the same local runtime root.

    The duplicate report is intentionally read-only, but a report alone cannot
    prevent two installers started at the same time from both passing it.  The
    lock lives in the OS temporary directory, keyed by the resolved target,
    so acquiring it does not create an installation marker inside the target.
    """

    def __init__(self, target_root: Path) -> None:
        self.target_root = _resolve(Path(target_root))
        digest = hashlib.sha256(
            str(self.target_root).encode("utf-8", errors="surrogateescape")
        ).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"anet-install-{digest}.lock"
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise PreflightConflict(
                "another Anet installer already owns the install lock for "
                f"{self.target_root}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        finally:
            handle.close()

    def __enter__(self) -> "InstallationLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


_ANET_ROOT_MARKERS = (
    "current",
    "current.json",
    "versions",
    "nodes",
    "release.json",
)
_ANET_RUNTIME_MARKERS = (
    "current",
    "current.json",
    "versions",
    "release.json",
)
_ANET_PERSISTENT_MARKERS = (
    "nodes",
    "config.json",
    "identity.json",
    "card.json",
    "remote-control.json",
)
_AHUB_MARKERS = (
    "ahub.sqlite3",
    "control.sqlite3",
    "config.json",
)
_ANET_PROCESS_RE = re.compile(
    r"(?:\b-anet\b|\banet-fabric\b|(?:^|\s)-m\s+anet(?:\s|$)|"
    r"\banet\s+(?:supervisor|serve|ahub-))",
    re.IGNORECASE,
)
_AHUB_PROCESS_RE = re.compile(r"\bahub(?:-serve|\s+serve)\b", re.IGNORECASE)


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in paths:
        resolved = _resolve(candidate)
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _path_exists(path: Path) -> bool:
    # A dangling ``current`` link is still an installation marker and should
    # be reported instead of silently treated as absent.
    return path.exists() or path.is_symlink()


def _markers(root: Path, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if _path_exists(root / name)]


def _describe_anet_root(
    root: Path,
    *,
    include_persistent_markers: bool = True,
) -> dict[str, Any] | None:
    if not root.is_dir() and not root.is_symlink():
        return None
    marker_names = (
        _ANET_ROOT_MARKERS if include_persistent_markers else _ANET_RUNTIME_MARKERS
    )
    markers = _markers(root, marker_names)
    if not markers:
        return None
    persistent = bool(set(markers) & set(_ANET_PERSISTENT_MARKERS))
    return {
        "kind": "anet-root",
        "path": str(root),
        "markers": markers,
        "persistent": persistent,
    }


def _describe_ahub_root(root: Path) -> dict[str, Any] | None:
    if not root.is_dir() and not root.is_symlink():
        return None
    markers = _markers(root, _AHUB_MARKERS)
    if not markers:
        return None
    return {
        "kind": "ahub-root",
        "path": str(root),
        "markers": markers,
    }


def _anet_candidates(platform_name: str, target_root: Path) -> list[Path]:
    home = Path.home()
    candidates = [target_root]
    if platform_name in {"wsl", "linux", "termux"}:
        candidates.append(home / ".local" / "anet")
        if platform_name != "termux":
            candidates.extend((Path("/opt/anet"), Path("/var/lib/anet")))
    elif platform_name == "macos":
        candidates.append(home / "Library" / "Application Support" / "Anet")
    return _unique_paths(candidates)


def _ahub_candidates(platform_name: str, target_root: Path) -> list[Path]:
    home = Path.home()
    candidates = [
        target_root / "ahub",
        target_root / "ahub-data",
        home / ".local" / "ahub",
        home / ".config" / "anet" / "ahub",
        Path("/var/lib/anet-ahub"),
        Path("/var/lib/ahub"),
    ]
    if platform_name == "macos":
        candidates.extend(
            (
                home / "Library" / "Application Support" / "Anet" / "Ahub",
                home / "Library" / "Application Support" / "Ahub",
            )
        )
    if platform_name == "termux":
        prefix = os.environ.get("PREFIX", "").strip()
        if prefix:
            candidates.extend(
                (
                    Path(prefix) / "var" / "lib" / "anet-ahub",
                    Path(prefix) / "var" / "lib" / "ahub",
                )
            )
    return _unique_paths(candidates)


def _run(command: list[str], *, timeout: int = 10) -> tuple[int, str]:
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


def _service_findings(platform_name: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if platform_name in {"wsl", "linux"}:
        systemctl = shutil.which("systemctl")
        if systemctl:
            for unit, kind in (
                ("anet-supervisor.service", "anet"),
                ("anet-ahub.service", "ahub"),
                ("ahub.service", "ahub"),
            ):
                active_code, active = _run(
                    [systemctl, "--user", "is-active", unit]
                )
                enabled_code, enabled = _run(
                    [systemctl, "--user", "is-enabled", unit]
                )
                if active_code == 0 or enabled_code == 0:
                    findings.append(
                        {
                            "kind": kind,
                            "manager": "systemd-user",
                            "name": unit,
                            "active": active if active_code == 0 else "unknown",
                            "enabled": enabled if enabled_code == 0 else "unknown",
                        }
                    )
            code, unit_output = _run(
                [systemctl, "--user", "list-unit-files", "--no-legend"]
            )
            if code == 0:
                known = {str(item["name"]) for item in findings}
                for line in unit_output.splitlines():
                    name = line.split(None, 1)[0] if line.split(None, 1) else ""
                    if not name.endswith(".service") or name in known:
                        continue
                    if name.startswith("anet-") or name.startswith("ahub"):
                        findings.append(
                            {
                                "kind": "ahub" if "ahub" in name else "anet",
                                "manager": "systemd-user",
                                "name": name,
                                "active": "unknown",
                                "enabled": "present",
                            }
                        )
    elif platform_name == "termux":
        prefix = os.environ.get("PREFIX", "").strip()
        if prefix:
            for name, kind in (("anet-supervisor", "anet"), ("anet-ahub", "ahub"), ("ahub", "ahub")):
                service_dir = Path(prefix) / "var" / "service" / name
                if service_dir.is_dir():
                    findings.append(
                        {
                            "kind": kind,
                            "manager": "termux-services",
                            "name": name,
                            "path": str(service_dir),
                        }
                    )
    elif platform_name == "macos":
        launchctl = shutil.which("launchctl")
        if launchctl:
            uid = str(os.getuid())
            for label, kind in (
                ("net.anet.supervisor", "anet"),
                ("net.anet.ahub", "ahub"),
                ("com.anet.ahub", "ahub"),
            ):
                code, _ = _run([launchctl, "print", f"gui/{uid}/{label}"])
                if code == 0:
                    findings.append(
                        {
                            "kind": kind,
                            "manager": "launchd",
                            "name": label,
                        }
                    )
    return findings


def _process_findings() -> list[dict[str, Any]]:
    ps = shutil.which("ps")
    if not ps:
        return []
    code, output = _run([ps, "-eo", "pid=,args="], timeout=15)
    if code != 0:
        return []
    findings: list[dict[str, Any]] = []
    current_pid = str(os.getpid())
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split(None, 1)
        if len(parts) != 2 or parts[0] == current_pid:
            continue
        pid, command = parts
        is_ahub = bool(_AHUB_PROCESS_RE.search(command))
        is_anet = bool(_ANET_PROCESS_RE.search(command))
        if not is_ahub and not is_anet:
            continue
        # Do not echo complete command lines: service arguments can contain
        # control URLs or other operator-supplied values.
        findings.append(
            {
                "kind": "ahub" if is_ahub and not is_anet else "anet",
                "manager": "process",
                "pid": int(pid) if pid.isdigit() else pid,
            }
        )
    return findings


def collect_preflight(
    platform_name: str,
    target_root: Path,
    *,
    include_services: bool = True,
    include_processes: bool = True,
    include_persistent_markers: bool = True,
) -> dict[str, Any]:
    """Collect a bounded, read-only installation report."""

    target = _resolve(target_root)
    roots = [
        finding
        for root in _anet_candidates(platform_name, target)
        if (
            finding := _describe_anet_root(
                root,
                include_persistent_markers=include_persistent_markers,
            )
        )
        is not None
    ]
    ahub_roots = [
        finding
        for root in _ahub_candidates(platform_name, target)
        if (finding := _describe_ahub_root(root)) is not None
    ]
    target_finding = next(
        (finding for finding in roots if _resolve(Path(finding["path"])) == target),
        None,
    )
    services = _service_findings(platform_name) if include_services else []
    processes = _process_findings() if include_processes else []
    return {
        "schema_version": 1,
        "platform": platform_name,
        "target_root": str(target),
        "target": target_finding,
        "existing_anet": roots,
        "existing_ahub": ahub_roots,
        "services": services,
        "processes": processes,
    }


def _foreign_anet(report: dict[str, Any], target_root: Path) -> list[dict[str, Any]]:
    target = _resolve(target_root)
    return [
        finding
        for finding in report.get("existing_anet", [])
        if _resolve(Path(str(finding.get("path", "")))) != target
    ]


def assert_no_duplicate(
    report: dict[str, Any],
    target_root: Path,
    *,
    deployment: bool,
    allow_existing: bool = False,
) -> None:
    """Stop an explicit deployment before mutation when another install exists.

    A clean runtime install is allowed to keep versioned runtimes side by side;
    its caller receives the report.  The persistent one-click layer is stricter
    and will only continue when the requested target is the existing deployment
    or when the operator explicitly supplies ``allow_existing``.
    """

    if not deployment or allow_existing:
        return
    foreign = _foreign_anet(report, target_root)
    if foreign:
        locations = ", ".join(str(item["path"]) for item in foreign)
        raise PreflightConflict(
            "Anet preflight found an existing installation in the same platform "
            f"boundary ({locations}); choose that root or pass the explicit "
            "allow-existing override"
        )
    target_exists = report.get("target") is not None
    active = [
        item
        for item in [*report.get("services", []), *report.get("processes", [])]
        if item.get("kind") == "anet"
    ]
    if active and not target_exists:
        names = ", ".join(
            str(item.get("name", item.get("pid", "process"))) for item in active
        )
        raise PreflightConflict(
            "Anet preflight found an active supervisor outside the requested "
            f"target root ({names}); stop or reuse it before installing"
        )


def emit_preflight(report: dict[str, Any], *, stream: TextIO | None = None) -> None:
    """Write a compact report without changing the installer JSON on stdout."""

    destination = stream or sys.stderr
    print(
        "Anet install preflight: "
        + json.dumps(report, ensure_ascii=False, separators=(",", ":")),
        file=destination,
    )
