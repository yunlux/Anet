from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_mcp_module_ignores_ambient_malformed_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"TOKEN=\xff\xfe\x00\x01")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-c", "import anet.mcp_server; print('ok')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
