#!/usr/bin/env python3
"""Install Anet into a clean, Agent-runtime-independent WSL environment."""

from __future__ import annotations

import sys
from pathlib import Path

from posix_runtime_installer import main


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("install_wsl.py requires Linux under WSL")
    raise SystemExit(main("wsl", Path.home() / ".local" / "anet"))
