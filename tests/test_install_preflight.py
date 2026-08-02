from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from install_preflight import (  # noqa: E402
    PreflightConflict,
    assert_no_duplicate,
    collect_preflight,
)


def test_runtime_preflight_reports_existing_runtime_and_ahub_without_nodes(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "versions").mkdir(parents=True)
    ahub = runtime / "ahub"
    ahub.mkdir()
    (ahub / "ahub.sqlite3").touch()

    report = collect_preflight(
        "linux",
        runtime,
        include_services=False,
        include_processes=False,
        include_persistent_markers=False,
    )

    assert report["target"]["markers"] == ["versions"]
    assert report["target"]["persistent"] is False
    assert report["existing_ahub"][0]["markers"] == ["ahub.sqlite3"]


def test_deployment_preflight_blocks_another_known_root(tmp_path: Path) -> None:
    target = tmp_path / "requested"
    existing = tmp_path / "existing"
    (existing / "versions").mkdir(parents=True)
    (existing / "nodes" / "default").mkdir(parents=True)
    (existing / "nodes" / "default" / "config.json").touch()

    report = collect_preflight(
        "linux",
        target,
        include_services=False,
        include_processes=False,
    )
    # The bounded production candidates do not include arbitrary tmp siblings;
    # explicitly add the existing root to model a discovered deployment.
    report["existing_anet"].append(
        {
            "kind": "anet-root",
            "path": str(existing.resolve()),
            "markers": ["versions", "nodes"],
            "persistent": True,
        }
    )

    try:
        assert_no_duplicate(report, target, deployment=True)
    except PreflightConflict as exc:
        assert "existing" in str(exc)
    else:
        raise AssertionError("a second persistent root must be rejected")

    assert_no_duplicate(
        report,
        target,
        deployment=True,
        allow_existing=True,
    )


def test_target_deployment_is_reusable(tmp_path: Path) -> None:
    target = tmp_path / "anet"
    (target / "versions").mkdir(parents=True)
    (target / "nodes" / "default").mkdir(parents=True)
    report = collect_preflight(
        "wsl",
        target,
        include_services=False,
        include_processes=False,
    )

    assert report["target"]["persistent"] is True
    assert_no_duplicate(report, target, deployment=True)
