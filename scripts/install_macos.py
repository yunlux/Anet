#!/usr/bin/env python3
"""Install Anet into a clean, Agent-runtime-independent macOS environment."""

from __future__ import annotations

import sys
from pathlib import Path

from posix_runtime_installer import main


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("install_macos.py requires macOS")
    raise SystemExit(
        main(
            "macos",
            Path.home() / "Library" / "Application Support" / "Anet",
        )
    )
