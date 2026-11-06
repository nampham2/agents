"""S2 and S4: `task finish --start-next` saves a checkpoint after the finish; a failed checkpoint never undoes it."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_context import checkpoint_data
from workspace_journal import OperationError
from workspace_lib import WorkspaceError, allocate_project
from workspace_operations import _group_by_output, finish_and_checkpoint, task_operation
from workspace_session import _load_state
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import planned


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "repo"
    target.mkdir()
    path = allocate_project(root, title="Finish checkpoint", working_directory=target)
    planned(path)
    task_operation(path, "start", "T01", expected_revision=1)
    return path


def evidence(project: Path) -> str:
    result = workflow(
        project, "verify",
        {"id": "check", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('verified')"]}]},
    )
    return str(result["attempts"][0]["result"]["record_id"])


def finish(project: Path, record: str, **fields: Any) -> dict[str, Any]:
    return finish_and_checkpoint(
        project, "T01", expected_revision=_load_state(project)["revision"], evidence_record_ids=[record], **fields
    )


def test_a_finish_with_start_next_saves_a_checkpoint(project: Path) -> None:
    result = finish(project, evidence(project), start_next="T02")
    assert result["checkpoint"] == "saved" and result["continue"] == "T02"
    assert result["revision"] == _load_state(project)["revision"]  # the checkpoint used no revision
    saved = checkpoint_data(project)
    assert saved is not None and saved["continuation"]["next"] == result["move_detail"]
    assert saved["continuation"]["session"] == "working"


def test_a_finish_without_start_next_writes_no_checkpoint(project: Path) -> None:
    result = finish(project, evidence(project))
    assert "checkpoint" not in result and not (project / "handoff.md").exists()
    assert _load_state(project)["tasks"][0]["status"] == "DONE"


def test_a_legacy_handoff_skips_the_checkpoint(project: Path) -> None:
    (project / "handoff.md").write_text("Old free-form notes.\n", encoding="utf-8")
    result = finish(project, evidence(project), start_next="T02")
    assert result["checkpoint_skipped"] == "legacy handoff" and "checkpoint" not in result
    assert (project / "handoff.md").read_text(encoding="utf-8") == "Old free-form notes.\n"


@pytest.mark.parametrize("failure", [WorkspaceError("handoff is locked"), OSError("disk full")])
def test_a_failed_checkpoint_keeps_the_finish_and_reports_it(project: Path, failure: Exception) -> None:
    record = evidence(project)
    with patch("workspace_workflows.workflow", side_effect=failure), pytest.raises(OperationError) as refusal:
        finish(project, record, start_next="T02")
    result = refusal.value.result
    assert result["committed"] is True and result["saved"] == ["finish"] and result["operation"] == "task.finish"
    assert result["recovery"] == "run workflow checkpoint" and str(failure) in result["error"]
    state = _load_state(project)
    assert [task["status"] for task in state["tasks"]] == ["DONE", "RUNNING"]


def test_the_cli_returns_one_for_a_failed_checkpoint_and_prints_the_result(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = evidence(project)
    revision = str(_load_state(project)["revision"])
    argv = ["research-project", "task", str(project), "finish", "T01", "--evidence", record,
            "--start-next", "T02", "--expected-revision", revision]
    with patch.object(sys, "argv", argv), patch("workspace_workflows.workflow", side_effect=WorkspaceError("boom")):
        code = manage_workspace.main()
    out, err = capsys.readouterr()
    assert code == 1 and json.loads(out)["committed"] is True and "ERROR: boom" in err


def test_the_cli_finishes_with_a_checkpoint(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = evidence(project)
    revision = str(_load_state(project)["revision"])
    argv = ["research-project", "task", str(project), "finish", "T01", "--evidence", record,
            "--start-next", "T02", "--expected-revision", revision]
    with patch.object(sys, "argv", argv):
        assert manage_workspace.main() == 0
    assert json.loads(capsys.readouterr().out)["checkpoint"] == "saved"


def test_the_cli_still_blocks_through_task_operation(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    revision = str(_load_state(project)["revision"])
    argv = [
        "research-project", "task", str(project), "block", "T01", "--reason", "waiting",
        "--expected-revision", revision,
    ]
    with patch.object(sys, "argv", argv):
        assert manage_workspace.main() == 0
    assert json.loads(capsys.readouterr().out)["changed_tasks"][0]["status"] == "BLOCKED"


def test_a_validation_refusal_is_regrouped_per_output() -> None:
    message = (
        "candidate validation failed:\n- task T01 output #1: file is missing\n- the plan is stale\n"
        "- task T01 output #1: path is not under the root\n- task T01 output #2: file is empty"
    )
    assert _group_by_output(message) == (
        "candidate validation failed:\n- task T01 output #1: file is missing; path is not under the root\n"
        "- task T01 output #2: file is empty\n- the plan is stale"
    )


@pytest.mark.parametrize("message", ["no such task", "candidate validation failed:", "candidate validation failed:\n"])
def test_other_messages_are_left_alone(message: str) -> None:
    assert _group_by_output(message) == message


def test_a_regrouped_refusal_keeps_its_first_line(project: Path) -> None:
    record = evidence(project)
    error = WorkspaceError("candidate validation failed:\n- task T01 output #1: a\n- task T01 output #1: b")
    with patch("workspace_operations.task_operation", side_effect=error), pytest.raises(WorkspaceError) as refusal:
        finish(project, record)
    assert str(refusal.value) == "candidate validation failed:\n- task T01 output #1: a; b"
    plain = WorkspaceError("unknown task: T09")
    with patch("workspace_operations.task_operation", side_effect=plain), pytest.raises(WorkspaceError) as same:
        finish(project, record)
    assert str(same.value) == "unknown task: T09"
