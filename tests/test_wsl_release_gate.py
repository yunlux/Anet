from __future__ import annotations

import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "wsl_release_gate.py"
SPEC = importlib.util.spec_from_file_location("wsl_release_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as handle:
        for name, raw in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            handle.addfile(item, io.BytesIO(raw))


def test_sha256_and_pytest_count(tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"anet")
    assert (
        gate.sha256_file(artifact)
        == "8F202BDBF250AA9BB932743A005ED2714FEBDD7B4A9900E904F018302CA58867"
    )
    assert gate.parse_pytest_count("71 passed in 20.82s") == 71
    assert gate.parse_pytest_count("1 failed, 70 passed in 1.0s") == 70
    with pytest.raises(gate.GateError, match="passed count"):
        gate.parse_pytest_count("collection failed")


def test_status_transition_allows_existing_live_state_but_rejects_regression() -> None:
    before = {"pending": 79, "rejections": 2, "untrusted": 10}
    gate.verify_status_transition(
        before,
        {"pending": 81, "rejections": 2, "untrusted": 9},
    )
    with pytest.raises(gate.GateError, match="rejections increased"):
        gate.verify_status_transition(
            before,
            {"pending": 0, "rejections": 3, "untrusted": 10},
        )
    with pytest.raises(gate.GateError, match="untrusted increased"):
        gate.verify_status_transition(
            before,
            {"pending": 0, "rejections": 2, "untrusted": 11},
        )


def test_safe_extract_accepts_one_project_root(tmp_path) -> None:
    archive = tmp_path / "release.tar.gz"
    _archive(
        archive,
        {
            "anet-1.0/pyproject.toml": b"[project]\nname='anet'\n",
            "anet-1.0/src/anet/__init__.py": b"",
        },
    )
    root = gate.safe_extract_sdist(archive, tmp_path / "extract")
    assert root.name == "anet-1.0"
    assert (root / "pyproject.toml").is_file()


def test_safe_extract_rejects_path_traversal_and_links(tmp_path) -> None:
    traversal = tmp_path / "traversal.tar.gz"
    _archive(
        traversal,
        {
            "anet-1.0/pyproject.toml": b"[project]\n",
            "../outside": b"escape",
        },
    )
    with pytest.raises(gate.GateError, match="unsafe path"):
        gate.safe_extract_sdist(traversal, tmp_path / "traversal-out")
    assert not (tmp_path / "outside").exists()

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as handle:
        project = tarfile.TarInfo("anet-1.0/pyproject.toml")
        project.size = 0
        handle.addfile(project, io.BytesIO())
        link = tarfile.TarInfo("anet-1.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        handle.addfile(link)
    with pytest.raises(gate.GateError, match="contains a link"):
        gate.safe_extract_sdist(linked, tmp_path / "link-out")


def test_atomic_report_is_private_and_complete(tmp_path) -> None:
    report = tmp_path / "report.json"
    gate.atomic_private_json(report, {"outcome": "passed", "tests": 71})
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "outcome": "passed",
        "tests": 71,
    }
    if os.name == "posix":
        assert report.stat().st_mode & 0o777 == 0o600


def test_public_summary_excludes_peer_and_key_material(tmp_path) -> None:
    report = {
        "outcome": "dry-run-passed",
        "target_version": "0.5.1",
        "after": {
            "version": {"distribution": "0.5.1"},
            "status_gates": {"pending": 0, "rejections": 0, "untrusted": 0},
            "peers": [{"sign_public": "must-not-leak"}],
            "protected_hashes": {"identity.json": "must-not-leak"},
        },
        "rollback": {"attempted": False, "succeeded": False},
    }
    summary = gate.public_summary(report, tmp_path / "report.json")
    encoded = json.dumps(summary)
    assert summary["installed_version"] == "0.5.1"
    assert summary["status_gates"]["pending"] == 0
    assert "must-not-leak" not in encoded
    assert "peers" not in summary


def test_isolated_test_env_points_home_into_check_root(tmp_path) -> None:
    check = tmp_path / "check"
    home, env = gate.isolated_test_env(check)
    assert home == check / "home"
    assert home.is_dir()
    assert env["HOME"] == str(home)
    assert check / "home" in list(check.rglob("home"))


def test_isolated_test_env_preserves_parent_environment(tmp_path) -> None:
    check = tmp_path / "check"
    try:
        os.environ["ANET_TEST_SENTINEL"] = "keep-me"
        _home, env = gate.isolated_test_env(check)
        assert env["ANET_TEST_SENTINEL"] == "keep-me"
    finally:
        os.environ.pop("ANET_TEST_SENTINEL", None)
