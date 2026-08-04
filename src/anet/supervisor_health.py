"""Durable, machine-readable health for the persistent Anet supervisor."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .encoding import atomic_json


SUPERVISOR_HEALTH_KIND = "anet.supervisor.health"
SUPERVISOR_HEALTH_SCHEMA_VERSION = 1
SUPERVISOR_HEALTH_NAME = "supervisor-health.json"
_MIN_STALE_AFTER_MS = 30_000
_STALE_GRACE_MS = 30_000
_MAX_ERROR_LENGTH = 512


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_error(error: BaseException | str) -> str:
    text = " ".join(str(error).split())
    return text[:_MAX_ERROR_LENGTH]


def current_boot_session_id() -> str:
    """Return a stable, non-secret identifier for the current OS boot session."""

    material = ""
    platform_name = "unknown"
    if os.name == "nt":
        class SystemTimeOfDayInformation(ctypes.Structure):
            _fields_ = (
                ("boot_time", ctypes.c_longlong),
                ("current_time", ctypes.c_longlong),
                ("time_zone_bias", ctypes.c_longlong),
                ("time_zone_id", ctypes.c_ulong),
                ("reserved", ctypes.c_ulong),
                ("boot_time_bias", ctypes.c_ulonglong),
                ("sleep_time_bias", ctypes.c_ulonglong),
            )

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtQuerySystemInformation.argtypes = (
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        )
        ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        value = SystemTimeOfDayInformation()
        status = ntdll.NtQuerySystemInformation(
            3,
            ctypes.byref(value),
            ctypes.sizeof(value),
            None,
        )
        if status == 0 and value.boot_time > 0:
            platform_name = "windows"
            material = str(value.boot_time)
    elif sys.platform.startswith("linux"):
        try:
            material = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            material = ""
        if material:
            platform_name = "linux"
    elif sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            material = completed.stdout.strip()
        if material:
            platform_name = "macos"
    if not material:
        return "unknown"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{platform_name}:{digest[:32]}"


class SupervisorHealthReporter:
    """Own the private health document for one running supervisor process."""

    def __init__(self, home: Path, *, now_ms: int | None = None) -> None:
        self.home = Path(home).expanduser().resolve()
        started_at_ms = _now_ms() if now_ms is None else int(now_ms)
        self._value: dict[str, Any] = {
            "kind": SUPERVISOR_HEALTH_KIND,
            "schema_version": SUPERVISOR_HEALTH_SCHEMA_VERSION,
            "state": "starting",
            "instance_id": uuid.uuid4().hex,
            "boot_session_id": current_boot_session_id(),
            "supervisor_pid": os.getpid(),
            "started_at_ms": started_at_ms,
            "heartbeat_at_ms": started_at_ms,
            "poll_seconds": 300.0,
            "child_pid": None,
            "child_state": "stopped",
            "last_sync_at_ms": None,
            "last_sequence": None,
            "last_error_at_ms": None,
            "last_error": "",
            "consecutive_failures": 0,
            "stopped_at_ms": None,
        }
        self._write()

    @property
    def path(self) -> Path:
        return self.home / SUPERVISOR_HEALTH_NAME

    def _write(self, *, now_ms: int | None = None) -> None:
        self._value["heartbeat_at_ms"] = _now_ms() if now_ms is None else int(now_ms)
        atomic_json(self.path, self._value, private=True)

    def synced(
        self,
        result: dict[str, Any],
        *,
        poll_seconds: float,
        now_ms: int | None = None,
    ) -> None:
        observed_ms = _now_ms() if now_ms is None else int(now_ms)
        self._value.update(
            {
                "last_sync_at_ms": observed_ms,
                "last_sequence": result.get("sequence"),
                "poll_seconds": float(poll_seconds),
                "last_error_at_ms": None,
                "last_error": "",
                "consecutive_failures": 0,
            }
        )
        self._write(now_ms=observed_ms)

    def child_running(self, pid: int, *, now_ms: int | None = None) -> None:
        self._value.update(
            {
                "state": "running",
                "child_pid": int(pid),
                "child_state": "running",
            }
        )
        self._write(now_ms=now_ms)

    def degraded(
        self,
        error: BaseException | str,
        *,
        child_pid: int | None = None,
        now_ms: int | None = None,
    ) -> None:
        observed_ms = _now_ms() if now_ms is None else int(now_ms)
        self._value.update(
            {
                "state": "degraded",
                "child_pid": child_pid,
                "child_state": "running" if child_pid is not None else "stopped",
                "last_error_at_ms": observed_ms,
                "last_error": _safe_error(error),
                "consecutive_failures": int(
                    self._value.get("consecutive_failures", 0)
                )
                + 1,
            }
        )
        self._write(now_ms=observed_ms)

    def child_exited(
        self, returncode: int, *, now_ms: int | None = None
    ) -> None:
        self.degraded(
            f"Anet server child exited with return code {returncode}",
            child_pid=None,
            now_ms=now_ms,
        )

    def restarting(self, *, now_ms: int | None = None) -> None:
        self._value.update(
            {
                "state": "restarting",
                "child_pid": None,
                "child_state": "stopped",
            }
        )
        self._write(now_ms=now_ms)

    def stopped(self, *, now_ms: int | None = None) -> None:
        observed_ms = _now_ms() if now_ms is None else int(now_ms)
        self._value.update(
            {
                "state": "stopped",
                "child_pid": None,
                "child_state": "stopped",
                "stopped_at_ms": observed_ms,
            }
        )
        self._write(now_ms=observed_ms)


def inspect_supervisor_health(
    home: Path, *, now_ms: int | None = None
) -> dict[str, Any]:
    """Return one validated health observation without mutating supervisor state."""

    path = Path(home).expanduser().resolve() / SUPERVISOR_HEALTH_NAME
    observed_ms = _now_ms() if now_ms is None else int(now_ms)
    if not path.is_file():
        return {
            "kind": SUPERVISOR_HEALTH_KIND,
            "schema_version": SUPERVISOR_HEALTH_SCHEMA_VERSION,
            "ok": False,
            "state": "missing",
            "reason": "supervisor health file is missing",
            "observed_at_ms": observed_ms,
            "path": str(path),
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        return {
            "kind": SUPERVISOR_HEALTH_KIND,
            "schema_version": SUPERVISOR_HEALTH_SCHEMA_VERSION,
            "ok": False,
            "state": "invalid",
            "reason": "supervisor health file is invalid",
            "observed_at_ms": observed_ms,
            "path": str(path),
        }
    try:
        if value.get("kind") != SUPERVISOR_HEALTH_KIND:
            raise ValueError("unexpected health kind")
        if int(value.get("schema_version", -1)) != SUPERVISOR_HEALTH_SCHEMA_VERSION:
            raise ValueError("unsupported health schema")
        pid = int(value["supervisor_pid"])
        started_at_ms = int(value["started_at_ms"])
        heartbeat_at_ms = int(value["heartbeat_at_ms"])
        poll_seconds = float(value["poll_seconds"])
        instance_id = str(value["instance_id"])
        uuid.UUID(hex=instance_id)
        boot_session_id = str(value["boot_session_id"])
        if started_at_ms <= 0 or poll_seconds <= 0 or not boot_session_id:
            raise ValueError("invalid poll interval")
    except (KeyError, TypeError, ValueError):
        return {
            "kind": SUPERVISOR_HEALTH_KIND,
            "schema_version": SUPERVISOR_HEALTH_SCHEMA_VERSION,
            "ok": False,
            "state": "invalid",
            "reason": "supervisor health fields are invalid",
            "observed_at_ms": observed_ms,
            "path": str(path),
        }
    heartbeat_age_ms = max(0, observed_ms - heartbeat_at_ms)
    stale_after_ms = max(
        _MIN_STALE_AFTER_MS,
        int(poll_seconds * 1000) + _STALE_GRACE_MS,
    )
    process_alive = _process_alive(pid)
    state = str(value.get("state", "invalid"))
    child_pid_value = value.get("child_pid")
    try:
        child_pid = int(child_pid_value) if child_pid_value is not None else 0
    except (TypeError, ValueError):
        child_pid = 0
    child_process_alive = _process_alive(child_pid)
    child_running = (
        value.get("child_state") == "running" and child_process_alive
    )
    try:
        last_sync_at_ms = int(value.get("last_sync_at_ms"))
    except (TypeError, ValueError):
        last_sync_at_ms = 0
    sync_complete = last_sync_at_ms >= started_at_ms
    fresh = heartbeat_age_ms <= stale_after_ms
    ok = (
        state == "running"
        and child_running
        and process_alive
        and fresh
        and sync_complete
    )
    result = dict(value)
    result.update(
        {
            "ok": ok,
            "observed_at_ms": observed_ms,
            "heartbeat_age_ms": heartbeat_age_ms,
            "stale_after_ms": stale_after_ms,
            "supervisor_process_alive": process_alive,
            "child_process_alive": child_process_alive,
            "fresh": fresh,
            "sync_complete": sync_complete,
            "path": str(path),
        }
    )
    if not ok:
        if not process_alive:
            result["reason"] = "supervisor process is not running"
        elif not fresh:
            result["reason"] = "supervisor heartbeat is stale"
        elif not child_running:
            result["reason"] = "Anet server child is not running"
        elif not sync_complete:
            result["reason"] = "supervisor has not completed a control sync"
        else:
            result["reason"] = f"supervisor state is {state}"
    return result
