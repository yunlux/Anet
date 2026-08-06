from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
POLICY_PATHS = (
    ROOT / "src",
    ROOT / "scripts",
    ROOT / "deploy",
    ROOT / "pyproject.toml",
)
PUBLIC_SCAN_EXCLUDES = {
    ".git",
    ".gradle",
    ".kotlin",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
}
LEGACY_ROLE_TOKENS = (
    "an" + "chor",
    "ala" + "ya",
    "man" + "as",
    "ma" + "no",
    "nez" + "ha",
    "her" + "mes",
    "co" + "dex",
    "clau" + "de",
)
PRIVATE_DEPLOYMENT_TOKENS = (
    "yu" + "nlu",
    "agent" + "_a",
    "node" + "_c",
    "shared" + "-fallback",
)
SENSITIVE_PATTERNS = {
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"
    ),
    "complete Node ID": re.compile(r"\ban1[a-z0-9]{30,}\b"),
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\(?!<)"),
    "Linux user path": re.compile(r"/home/(?!<)[A-Za-z0-9._-]+/"),
    "GitHub token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "JWT": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\b"
    ),
}


def _policy_files() -> list[Path]:
    result: list[Path] = []
    for path in POLICY_PATHS:
        if path.is_file():
            result.append(path)
            continue
        result.extend(
            item
            for item in path.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and not any(part.endswith(".egg-info") for part in item.parts)
        )
    return result


def _public_text_files() -> list[Path]:
    result: list[Path] = []
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode == 0:
        candidates = [ROOT / item for item in completed.stdout.splitlines()]
    else:
        candidates = list(ROOT.rglob("*"))
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in PUBLIC_SCAN_EXCLUDES for part in path.parts):
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        result.append(path)
    return result


def test_source_and_deployment_assets_are_agent_neutral() -> None:
    violations: list[str] = []
    for path in _policy_files():
        relative = path.relative_to(ROOT).as_posix()
        lowered_name = relative.casefold()
        if any(token in lowered_name for token in LEGACY_ROLE_TOKENS):
            violations.append(relative)
            continue
        try:
            content = path.read_text(encoding="utf-8").casefold()
        except UnicodeDecodeError:
            continue
        if any(token in content for token in LEGACY_ROLE_TOKENS):
            violations.append(relative)
    assert violations == []


def test_core_import_does_not_eagerly_load_ahub_server() -> None:
    code = (
        "import sys; import anet; "
        "raise SystemExit(any(name.startswith('anet.ahub') for name in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0


def test_public_tree_contains_no_private_deployment_material() -> None:
    violations: list[str] = []
    role_tokens = LEGACY_ROLE_TOKENS[:5] + PRIVATE_DEPLOYMENT_TOKENS
    for path in _public_text_files():
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        lowered = content.casefold()
        for token in role_tokens:
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
                violations.append(f"{relative}: private deployment token")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(content):
                violations.append(f"{relative}: {label}")
    assert violations == []


def test_named_agent_compatibility_files_are_not_shipped() -> None:
    assert not (ROOT / ("her" + "mes-mcp.example.json")).exists()
    assert not (ROOT / ("CLA" + "UDE.md")).exists()


def test_github_actions_are_pinned_to_full_commit_shas() -> None:
    violations: list[str] = []
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = re.search(r"\buses:\s*[^@\s]+@([^\s#]+)", line)
            if match and not re.fullmatch(r"[0-9a-f]{40}", match.group(1)):
                violations.append(f"{path.name}:{line_number}")
    assert violations == []


def test_release_metadata_is_present() -> None:
    for relative in (
        "README.md",
        "README.zh-CN.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        ".github/workflows/ci.yml",
        ".github/dependabot.yml",
        "docs/RELEASE_CHECKLIST.md",
    ):
        assert (ROOT / relative).is_file()


def test_readme_defaults_to_english_and_links_the_chinese_version() -> None:
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "**English** | [简体中文](README.zh-CN.md)" in english
    assert "[English](README.md) | **简体中文**" in chinese
    assert "## One-command deployment" in english
    assert "## Agent-assisted installation" in english
    assert "## 新设备一条命令安装" in chinese
    assert "## 让 Agent 辅助安装" in chinese
    assert "install_windows_oneclick.ps1" in english
    assert "install_windows_oneclick.ps1" in chinese
    for installer in (
        "scripts/install_windows.ps1",
        "scripts/install_wsl.py",
        "scripts/install_macos.py",
        "skills/install-anet",
    ):
        assert installer in english
        assert installer in chinese
