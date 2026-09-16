"""Fresh-project schema-v4 initialization and schema-v3 compatibility tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from workspace_lib import (
    ValidationReport,
    WorkspaceError,
    _probe_execution_config,
    allocate_project,
    validate_v3_state,
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    workspace.mkdir()
    target.mkdir()
    return workspace, target


def test_fresh_project_is_execution_enabled_v4(tmp_path: Path) -> None:
    workspace, target = _roots(tmp_path)

    project_dir = allocate_project(workspace, title="Parallel project", working_directory=target)

    state = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    config = json.loads((project_dir / "execution" / "config.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 4
    assert state["revision"] == 0
    assert state["execution"] == {
        "protocol_version": 1,
        "coordinator_run": None,
        "ownership_generation": 0,
        "attempts": {},
    }
    assert config["atomic_link"] is True
    assert config["same_volume"] is True
    assert config["runner"] == "posix_subprocess"
    assert config["max_concurrent"] == 2
    assert config["legacy_writers_quiesced"] is False


def test_existing_v3_state_remains_valid_and_unchanged(tmp_path: Path) -> None:
    workspace, target = _roots(tmp_path)
    project_dir = workspace / "2026-09-16-001"
    project_dir.mkdir()
    for name in ("tasks", "artifacts", "reviews"):
        (project_dir / name).mkdir()
    (project_dir / "spec.md").write_text(
        "# Legacy\n\n## Current specification\n\nKeep v3.\n\n## Decision history\n\n- Existing.\n",
        encoding="utf-8",
    )
    (project_dir / "briefing.md").write_text("# Briefing\n", encoding="utf-8")
    (project_dir / "evidence.md").write_text("# Evidence\n", encoding="utf-8")
    state = {
        "schema_version": 3,
        "project": project_dir.name,
        "title": "Legacy",
        "status": "PLANNING",
        "created": "2026-09-16T00:00:00+00:00",
        "updated": "2026-09-16T00:00:00+00:00",
        "working_directory": str(target),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": [],
    }
    original = json.dumps(state, sort_keys=True)

    report = validate_v3_state(state, project_dir, check_files=True)

    assert report.errors == []
    assert json.dumps(state, sort_keys=True) == original
    assert not (project_dir / "execution").exists()


def test_failed_runner_probe_leaves_no_canonical_project(tmp_path: Path) -> None:
    workspace, target = _roots(tmp_path)

    with patch("workspace_lib.subprocess.run", side_effect=OSError("cannot spawn")):
        with pytest.raises(WorkspaceError, match="R-NO-RUNNER"):
            allocate_project(workspace, title="Broken runner", working_directory=target)

    project_dir = next(path for path in workspace.iterdir() if path.is_dir() and path.name != "memory")
    assert not (project_dir / "project.json").exists()


def test_nonzero_runner_probe_is_rejected(tmp_path: Path) -> None:
    workspace, target = _roots(tmp_path)
    failed = subprocess.CompletedProcess(["python", "-c", "pass"], 7)

    with patch("workspace_lib.subprocess.run", return_value=failed):
        with pytest.raises(WorkspaceError, match="status 7"):
            allocate_project(workspace, title="Broken runner", working_directory=target)


def test_non_posix_runner_is_rejected(tmp_path: Path) -> None:
    workspace, _target = _roots(tmp_path)
    project_dir = workspace / "2026-09-16-001"
    project_dir.mkdir()

    with patch("workspace_lib.os.name", "nt"):
        with pytest.raises(WorkspaceError, match="R-NO-RUNNER"):
            _probe_execution_config(project_dir, legacy_writers_quiesced=False)


def test_invalid_fresh_v4_candidate_is_not_published(tmp_path: Path) -> None:
    workspace, target = _roots(tmp_path)

    with patch(
        "workspace_lib.validate_v4_state",
        return_value=ValidationReport(errors=["injected invalid state"]),
    ):
        with pytest.raises(WorkspaceError, match="fresh v4 state validation failed"):
            allocate_project(workspace, title="Invalid v4", working_directory=target)

    project_dir = next(path for path in workspace.iterdir() if path.is_dir() and path.name != "memory")
    assert not (project_dir / "project.json").exists()
