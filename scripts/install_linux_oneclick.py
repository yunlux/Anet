#!/usr/bin/env python3
"""One-click self-starting Anet deployment on non-WSL Linux."""

from __future__ import annotations

from pathlib import Path

from posix_oneclick import main


if __name__ == "__main__":
    raise SystemExit(main("linux", Path.home() / ".local" / "anet"))
