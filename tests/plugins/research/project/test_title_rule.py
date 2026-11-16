"""The title rule after creation: `update`, the v3 and v4 validators, `context` and migration.

Creation has refused a multi-line title, or one holding `<!--`, since project 2026-11-16-001. These
tests cover the paths that accepted one until project 2026-11-16-004: an `update` patch, a title
edited by hand into `project.json` (which every state command must then refuse by name), and a v2
project migrated to v3. Every test goes through a public path, so on code without the rule it fails
on behaviour, not on an import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from workspace_lib import (
    WorkspaceError,
    allocate_project,
    load_json,
    migrate_v2_state,
    validate_project,
    validate_v3_state,
)
from workspace_session import project_context, update_project_data

BAD_TITLES = [
    pytest.param("two\nlines", "one line", id="newline"),
    pytest.param("trailing\n", "one line", id="trailing-newline"),
    pytest.param("trailing\r\n", "one line", id="trailing-crlf"),
    pytest.param("a b", "one line", id="line-separator"),
    pytest.param("x ", "one line", id="trailing-line-separator"),
    pytest.param("x\x85y", "one line", id="next-line"),
    pytest.param("plain <!-- note", "marker", id="marker"),
    pytest.param("   ", "non-empty string", id="whitespace-only"),
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "repo").mkdir()
    return allocate_project(tmp_path / "workspace", title="A project", working_directory=tmp_path / "repo")


def _state(project: Path) -> dict[str, Any]:
    return load_json(project / "project.json")


def _hand_edit_title(project: Path, title: str) -> None:
    state = _state(project)
    state["title"] = title
    (project / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize(("title", "message"), BAD_TITLES)
def test_update_refuses_a_bad_title_and_writes_nothing(project: Path, title: str, message: str) -> None:
    before = (project / "project.json").read_bytes()
    revision = _state(project)["revision"]
    with pytest.raises(WorkspaceError, match=f"title must .*{message}"):
        update_project_data(project, {"title": title}, expected_revision=revision)
    assert (project / "project.json").read_bytes() == before


def test_update_still_accepts_a_good_title(project: Path) -> None:
    revision = _state(project)["revision"]
    update_project_data(project, {"title": "A better title"}, expected_revision=revision)
    assert _state(project)["title"] == "A better title"


@pytest.mark.parametrize("schema", [3, 4])
@pytest.mark.parametrize(("title", "message"), BAD_TITLES)
def test_research_validate_reports_a_bad_title_on_v3_and_v4(
    project: Path, schema: int, title: str, message: str
) -> None:
    state = _state(project)
    state["title"] = title
    if schema == 3:
        state["schema_version"] = 3
        # v3 has no tags or predecessor fields.
        state.pop("tags", None)
        state.pop("predecessor", None)
    (project / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    report = validate_project(project)
    title_errors = [error for error in report.errors if "title must" in error]
    assert title_errors and all(error.startswith("project: title must") for error in title_errors)
    assert message in title_errors[0]


def test_an_empty_title_gives_exactly_one_error(project: Path) -> None:
    _hand_edit_title(project, "")
    report = validate_project(project)
    assert [error for error in report.errors if "title" in error] == ["project: title must be a non-empty string"]


def test_a_hand_edited_bad_title_stops_update_and_context_by_name(project: Path) -> None:
    _hand_edit_title(project, "two\nlines")
    with pytest.raises(WorkspaceError, match="project: title must be one line"):
        update_project_data(project, {"title": "Fixed"}, expected_revision=_state(project)["revision"])
    with pytest.raises(WorkspaceError, match="project: title must be one line"):
        project_context(project)
    # The recovery is the same kind of edit that caused it.
    _hand_edit_title(project, "Fixed")
    assert not [error for error in validate_project(project).errors if "title" in error]


def _v2_state(project_dir: Path, target: Path, title: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "project": project_dir.name,
        "title": title,
        "status": "DONE",
        "created": "2026-08-28",
        "updated": "2026-08-28T10:00:00+02:00",
        "working_directory": str(target),
        "current_task": None,
        "review_cycle": 0,
        "tasks": [],
    }


def test_a_v2_project_with_a_multi_line_title_does_not_migrate_to_valid_v3(project: Path, tmp_path: Path) -> None:
    migrated = migrate_v2_state(_v2_state(project, tmp_path / "repo", "Legacy\n## forged"), project)
    errors = validate_v3_state(migrated, project).errors
    assert "project: title must be one line" in errors


def test_a_v2_project_with_a_good_title_still_migrates(project: Path, tmp_path: Path) -> None:
    migrated = migrate_v2_state(_v2_state(project, tmp_path / "repo", "Legacy project"), project)
    assert not [error for error in validate_v3_state(migrated, project).errors if "title" in error]
