"""S2: `research-project start` and `context --compact`; the default `context` output is pinned."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_context import compact_context, start_project
from workspace_lib import WorkspaceError, allocate_project
from workspace_session import _load_state, project_context, validated_project_context

from tests.plugins.research.project.test_lifecycle_commands import planned

KEYS = {
    "project_dir", "revision", "roots", "tokens", "current_tasks", "next_move", "valid", "compact", "note",
}


def cli(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, Any, str]:
    capsys.readouterr()
    with patch.object(sys, "argv", ["research-project", *arguments]):
        code = manage_workspace.main()
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip().startswith("{") else out, err


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def make(workspace: Path, target: Path, title: str = "Reduce research project overhead") -> Path:
    return allocate_project(workspace, title=title, working_directory=target)


def test_start_resumes_a_project_by_directory(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make(workspace, target)
    code, result, _ = cli(capsys, "start", "--project-dir", str(project))
    assert code == 0 and KEYS <= set(result) and result["compact"] is True
    assert result["project_dir"] == str(project) and result["revision"] == 0
    assert result["next_move"]["stop_reason"] == "user-decision"  # a new project is ALIGNING
    assert result["assignment"] is None and "matched" not in result
    assert result["tokens"]["spec"] and "valid" in result


def test_start_gives_the_assignment_when_a_task_is_next(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make(workspace, target)
    planned(project)
    _, result, _ = cli(capsys, "start", "--project-dir", str(project))
    assert result["next_move"]["continue"] == "T01"
    assert result["assignment"]["role"] == "coordinator" and result["assignment"]["selected_task"]["id"] == "T01"


def test_start_resumes_the_one_look_alike_on_the_same_repository(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make(workspace, target)
    code, result, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Reduce research project overhead",
        "--working-directory", str(target),
    )
    assert code == 0 and result["project_dir"] == str(project)
    assert result["matched"]["same_repository"] is True and result["matched"]["project"] == project.name


def test_start_chooses_nothing_among_several_matches(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make(workspace, target, "Reduce research project overhead")
    make(workspace, target, "Reduce research project overhead again")
    code, result, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Reduce research project overhead",
        "--working-directory", str(target),
    )
    assert code == 1 and len(result["candidates"]) == 2 and "project_dir" not in result


def test_start_never_resumes_a_look_alike_on_another_repository(
    workspace: Path, target: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make(workspace, target, "Reduce research project overhead")
    other = tmp_path / "other"
    other.mkdir()
    code, result, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Reduce research project overhead",
        "--working-directory", str(other),
    )
    assert code == 1 and result["candidates"][0]["same_repository"] is False


def test_start_without_a_match_returns_no_candidates_unless_asked_to_create(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Something unrelated entirely",
        "--working-directory", str(target),
    )
    assert (code, result) == (0, {"candidates": []})
    code, created, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Something unrelated entirely",
        "--working-directory", str(target), "--create",
    )
    assert code == 0 and KEYS <= set(created) and Path(created["project_dir"]).is_dir()
    # A retry (the first call's resume might have failed) finds the project and resumes it.
    code, again, _ = cli(
        capsys, "start", "--workspace-root", str(workspace), "--title", "Something unrelated entirely",
        "--working-directory", str(target), "--create",
    )
    assert code == 0 and again["project_dir"] == created["project_dir"] and again["matched"]["project"]


def test_start_creates_a_missing_workspace_root_only_when_told(
    tmp_path: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "fresh-root"
    code, result, _ = cli(
        capsys, "start", "--workspace-root", str(root), "--title", "A new thing", "--working-directory", str(target),
    )
    assert (code, result) == (0, {"candidates": []}) and not root.exists()
    with pytest.raises(WorkspaceError):
        start_project(root, title="A new thing", working_directory=target, create=True)
    result, code = start_project(root, title="A new thing", working_directory=target, create=True, create_root=True)
    assert code == 0 and root.is_dir() and isinstance(result["warnings"], list)


def test_start_refuses_a_confusing_request(workspace: Path, target: Path) -> None:
    project = make(workspace, target)
    with pytest.raises(WorkspaceError, match="give --project-dir to resume, or --title and --working-directory"):
        start_project(workspace, project_dir=project, title="x", working_directory=target)
    with pytest.raises(WorkspaceError, match="--title together with --working-directory"):
        start_project(workspace, title="only a title")
    with pytest.raises(WorkspaceError, match="--title together with --working-directory"):
        start_project(workspace, working_directory=target)


def test_start_prints_the_warnings_of_a_created_project(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("workspace_operations.vcs_warnings", return_value=["no version control here"]):
        _, result, err = cli(
            capsys, "start", "--workspace-root", str(workspace), "--title", "A warned project",
            "--working-directory", str(target), "--create",
        )
    assert result["warnings"] == ["no version control here"] and "WARNING: no version control here" in err


def test_compact_context_returns_the_summary_keys(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make(workspace, target)
    planned(project)
    code, result, _ = cli(capsys, "context", str(project), "--compact")
    assert code == 0 and set(result) == KEYS
    assert result["current_tasks"] == [] and result["next_move"]["continue"] == "T01"
    assert result["tokens"] == project_context(project)["tokens"]
    assert result["revision"] == _load_state(project)["revision"]


@pytest.mark.parametrize("flag", [["--validate"], ["--task", "T01"], ["--worker"], ["--task-only"], ["--limit", "3"]])
def test_compact_context_refuses_the_other_projections(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    project = make(workspace, target)
    code, _, err = cli(capsys, "context", str(project), "--compact", *flag)
    assert code == 1 and "context --compact cannot be combined with" in err


def test_the_default_context_output_is_unchanged(
    workspace: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A regression pin: the default and --validate outputs keep their shape; --limit still defaults to 5."""
    project = make(workspace, target)
    planned(project)
    code, default, _ = cli(capsys, "context", str(project))
    assert code == 0 and default == json.loads(json.dumps(project_context(project, limit=5)))
    assert "compact" not in default
    code, full, _ = cli(capsys, "context", str(project), "--validate", "--limit", "7")
    assert code == 0 and full == json.loads(json.dumps(validated_project_context(project, limit=7)))
    assert compact_context(validated_project_context(project), _load_state(project), project)["compact"] is True
