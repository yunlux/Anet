from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "install-anet"
EXPECTED_SHA256 = (
    "6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7"
)


def test_install_anet_skill_is_self_contained() -> None:
    required = (
        SKILL / "SKILL.md",
        SKILL / "scripts" / "install.py",
        SKILL / "scripts" / "bootstrap_wsl.py",
        SKILL / "references" / "after-install.md",
        SKILL / "assets" / "SHA256SUMS",
        SKILL / "assets" / "anet_fabric-0.12.1-py3-none-any.whl",
    )
    assert all(path.is_file() for path in required)


def test_install_anet_skill_bundled_wheel_is_pinned() -> None:
    wheel = (
        SKILL
        / "assets"
        / "anet_fabric-0.12.1-py3-none-any.whl"
    )
    assert hashlib.sha256(wheel.read_bytes()).hexdigest().upper() == (
        EXPECTED_SHA256
    )
    sums = (SKILL / "assets" / "SHA256SUMS").read_text(
        encoding="utf-8"
    )
    assert sums.startswith(EXPECTED_SHA256)
    installer = (SKILL / "scripts" / "install.py").read_text(
        encoding="utf-8"
    )
    assert EXPECTED_SHA256 in installer


def test_install_anet_skill_is_runtime_only_by_default() -> None:
    instructions = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(instructions.split())
    assert "Do not run `anet init` as part of installation." in normalized
    assert "Do not edit a Hermes profile" in normalized
    assert "Do not use `sudo`." in normalized


def test_skill_exposes_explicit_full_runtime_for_wsl_bootstrap() -> None:
    installer = (SKILL / "scripts" / "install.py").read_text(
        encoding="utf-8"
    )
    bootstrap = (SKILL / "scripts" / "bootstrap_wsl.py").read_text(
        encoding="utf-8"
    )
    assert 'choices=("mcp", "full")' in installer
    assert '"--feature",' in bootstrap
    assert '"full",' in bootstrap
    assert "second Ahub" in bootstrap
    assert "registered Node ID does not match node home" in bootstrap
    assert "ANET_MCP_ALLOW_RAW_INBOX" in bootstrap
    assert "ANET_MCP_ALLOW_RELATION_MODEL" in bootstrap
    assert "ANET_MCP_ALLOW_RELATION_ACTIVITY" in bootstrap
    assert "ANET_MCP_ALLOW_RELATION_DISCLOSURE" in bootstrap


def test_one_prompt_is_cross_platform_and_fail_closed() -> None:
    guide = (ROOT / "docs" / "HERMES_SKILL_INSTALL.md").read_text(
        encoding="utf-8"
    )
    assert "https://github.com/yunlux/Anet" in guide
    assert "scripts/install_windows.ps1" in guide
    assert "scripts/install_macos.py" in guide
    assert "On non-WSL platforms stop after the verified runtime install" in guide
    assert "bootstrap_wsl.py" in guide
    assert "启动第二个 Ahub" in guide
    assert "禁止复制身份" in guide
    assert "不要让我选择" in guide
    assert "without asking the user" in guide
    assert "autonomously generated random identifier" in guide
    assert re.search(r"identity_files[^\n]*0", guide)
