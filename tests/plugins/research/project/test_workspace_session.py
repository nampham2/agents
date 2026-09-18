"""Small updates preserve transaction guards; resume context stays small as history grows."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_lib import WorkspaceError, allocate_project, atomic_write_json, record_evidence, validate_project
from workspace_session import project_context, update_project

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_workspace import WorkspaceFixture


@pytest.fixture
def fixture(tmp_path: Path) -> WorkspaceFixture:
    result = WorkspaceFixture(tmp_path)
    state = result.state("PLANNING")
    state["tasks"] = []
    atomic_write_json(result.project_dir / "project.json", state)
    return result


def change(fixture: WorkspaceFixture, changes: dict[str, Any], revision: int) -> dict[str, Any]:
    path = fixture.workspace_root / "patch.json"
    atomic_write_json(path, changes)
    return update_project(fixture.project_dir, path, expected_revision=revision)


def task(task_id: str = "T01") -> dict[str, Any]:
    return {
        "id": task_id,
        "name": "Verify output",
        "success_criteria": "Output has expected contents",
        "verification": "Run the output check",
        "effect": {"kind": "none"},
        "outputs": [{"root": "target", "path": "out.txt", "required": True}],
    }


def test_small_updates_execute_verify_and_close(fixture: WorkspaceFixture) -> None:
    first, second = task(), task("T02")
    second["depends_on"] = ["T01"]
    state = change(fixture, {"tasks": [first, second]}, 0)
    assert state["tasks"][0]["authorization"]["status"] == "not_required"
    state = change(fixture, {"status": "EXECUTING", "tasks": [{"id": "T01", "status": "RUNNING"}]}, 1)
    assert state["current_tasks"] == ["T01"]
    assert record_evidence(fixture.project_dir, "T01", [sys.executable, "-c", "print('checked output')"]) == 0
    evidence = [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}]
    state = change(
        fixture,
        {
            "tasks": [
                {"id": "T01", "status": "DONE", "evidence": evidence},
                {"id": "T02", "status": "RUNNING"},
            ]
        },
        2,
    )
    assert state["current_tasks"] == ["T02"]
    assert state["tasks"][1]["depends_on"] == ["T01"]
    history = state["tasks"][0]
    state = change(fixture, {"status": "DONE", "tasks": [{"id": "T02", "status": "DONE", "evidence": evidence}]}, 3)
    assert state["current_tasks"] == []
    assert state["tasks"][0] == history
    assert validate_project(fixture.project_dir, close=True, check_index=True).valid


def test_rejects_stale_update_and_terminal_rewrite(fixture: WorkspaceFixture) -> None:
    change(fixture, {"tasks": [task()]}, 0)
    original = (fixture.project_dir / "project.json").read_bytes()
    with pytest.raises(WorkspaceError, match="revision conflict"):
        change(fixture, {"title": "Lost update"}, 0)
    assert (fixture.project_dir / "project.json").read_bytes() == original
    change(fixture, {"tasks": [{"id": "T01", "status": "SKIPPED", "skip_reason": "User withdrew this work"}]}, 1)
    with pytest.raises(WorkspaceError, match="terminal task history"):
        change(fixture, {"tasks": [{"id": "T01", "name": "Rewrite history"}]}, 2)


def test_external_defaults_never_grant_consent(fixture: WorkspaceFixture) -> None:
    external = task()
    external["effect"] = {"kind": "external", "description": "Publish the guide"}
    state = change(fixture, {"tasks": [external]}, 0)
    assert state["tasks"][0]["authorization"]["status"] == "pending"
    assert state["tasks"][0]["authorization"]["required"] is True
    assert project_context(fixture.project_dir)["ready"] == []
    with pytest.raises(WorkspaceError, match="explicit authorization"):
        change(fixture, {"status": "EXECUTING", "tasks": [{"id": "T01", "status": "RUNNING"}]}, 1)


def test_missing_outputs_and_evidence_still_prevent_completion(fixture: WorkspaceFixture) -> None:
    change(fixture, {"status": "EXECUTING", "tasks": [{**task(), "status": "RUNNING"}]}, 0)
    with pytest.raises(WorkspaceError, match="evidence"):
        change(fixture, {"tasks": [{"id": "T01", "status": "DONE"}]}, 1)
    (fixture.target_dir / "out.txt").unlink()
    with pytest.raises(WorkspaceError, match="output"):
        change(
            fixture,
            {
                "tasks": [
                    {
                        "id": "T01",
                        "status": "DONE",
                        "evidence": [
                            {"root": "workspace", "path": "evidence.md", "anchor": None},
                        ],
                    }
                ]
            },
            1,
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"current_tasks": []}, "unsupported update fields"),
        ({"tasks": {}}, "array"),
        ({"tasks": [None]}, "non-empty id"),
        ({"tasks": [{"id": ""}]}, "non-empty id"),
        ({"tasks": [task(), task()]}, "duplicate task"),
        ({"tasks": [{"id": "T01"}]}, "explicit effect.kind"),
        ({"tasks": [{**task(), "effect": {"kind": []}}]}, "explicit effect.kind"),
        ({"tasks": [{**task(), "surprise": True}]}, "unexpected fields"),
    ],
)
def test_malformed_patches_leave_state_unchanged(
    fixture: WorkspaceFixture, changes: dict[str, Any], message: str
) -> None:
    original = (fixture.project_dir / "project.json").read_bytes()
    with pytest.raises(WorkspaceError, match=message):
        change(fixture, changes, 0)
    assert (fixture.project_dir / "project.json").read_bytes() == original


def test_resume_payload_does_not_grow_with_completed_history(fixture: WorkspaceFixture) -> None:
    state = fixture.state("EXECUTING")
    state["tasks"] = [fixture.task(f"H{i}") for i in range(200)]
    state["tasks"].extend(fixture.task(f"T{i}", "TODO") for i in range(12))
    state["tasks"][-1].update(status="BLOCKED", block_reason="Need a decision")
    state["tasks"][-2].update(status="RUNNING")
    state["current_tasks"] = ["T10"]
    atomic_write_json(fixture.project_dir / "project.json", state)
    (fixture.project_dir / "spec.md").write_text(
        "## Current specification\n\n" + "x" * 7000 + "\n## Decision history\nPRIVATE_OLD_HISTORY",
        encoding="utf-8",
    )
    result = project_context(fixture.project_dir)
    assert result["task_counts"]["DONE"] == 200
    assert len(result["tasks"]) == 5
    assert result["tasks"][0]["id"] == "T10"
    assert result["tasks"][1]["id"] == "T11"
    assert result["omitted_active_tasks"] == 7
    assert result["omitted_ready_tasks"] == 5
    assert result["spec_truncated"] is True
    assert len(result["spec"]) == 6000
    assert "PRIVATE_OLD_HISTORY" not in json.dumps(result)
    assert "selected_task" not in result
    assert len(json.dumps(result)) < 8500
    selected = project_context(fixture.project_dir, task_id="H199")
    assert selected["selected_task"] == state["tasks"][199]
    with pytest.raises(WorkspaceError, match="unknown task"):
        project_context(fixture.project_dir, task_id="absent")
    with pytest.raises(WorkspaceError, match="limit"):
        project_context(fixture.project_dir, limit=0)


def test_context_missing_spec_and_invalid_state(fixture: WorkspaceFixture) -> None:
    (fixture.project_dir / "spec.md").unlink()
    assert project_context(fixture.project_dir)["spec"] == ""
    state = fixture.state("PLANNING")
    state["tasks"] = [None]
    atomic_write_json(fixture.project_dir / "project.json", state)
    with pytest.raises(WorkspaceError, match="invalid project state"):
        project_context(fixture.project_dir)
    state["schema_version"] = 2
    atomic_write_json(fixture.project_dir / "project.json", state)
    with pytest.raises(WorkspaceError, match="legacy state"):
        project_context(fixture.project_dir)


def test_temporary_filesystem_failure_is_workspace_error(fixture: WorkspaceFixture) -> None:
    with patch("workspace_session.tempfile.TemporaryDirectory", side_effect=OSError("disk full")):
        with pytest.raises(WorkspaceError, match="cannot prepare project update"):
            change(fixture, {"title": "New"}, 0)


def test_v4_updates_remain_guarded_by_executor_ownership(fixture: WorkspaceFixture) -> None:
    project = allocate_project(fixture.workspace_root, title="V4", working_directory=fixture.target_dir)
    path = fixture.workspace_root / "patch.json"
    atomic_write_json(path, {"status": "PLANNING"})
    update_project(project, path, expected_revision=0)
    state = json.loads((project / "project.json").read_text())
    state["execution"]["coordinator_run"] = "live-run"
    atomic_write_json(project / "project.json", state)
    assert project_context(project)["execution_active"] is True
    with pytest.raises(WorkspaceError, match="mutating commands are refused"):
        update_project(project, path, expected_revision=1)


def test_cli_context_and_update(fixture: WorkspaceFixture, capsys: pytest.CaptureFixture[str]) -> None:
    path = fixture.workspace_root / "patch.json"
    atomic_write_json(path, {"tasks": [task()]})
    with patch.object(
        sys, "argv", ["research-project", "update", str(fixture.project_dir), str(path), "--expected-revision", "0"]
    ):
        assert manage_workspace.main() == 0
    assert "Committed revision 1" in capsys.readouterr().out
    with patch.object(sys, "argv", ["research-project", "context", str(fixture.project_dir), "--task", "T01"]):
        assert manage_workspace.main() == 0
    assert json.loads(capsys.readouterr().out)["selected_task"]["id"] == "T01"


@pytest.mark.parametrize("surface", ["bin", "skills/project/scripts"])
def test_both_host_launchers_support_small_updates(tmp_path: Path, surface: str) -> None:
    launcher = REPO_ROOT / "plugins/research" / surface / "research-project"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    initialized = subprocess.run(
        [str(launcher), "init", str(workspace), "--title", "Small", "--working-directory", str(tmp_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    project = Path(initialized.stdout.strip())
    assert not (project / "briefing.md").exists()
    path = tmp_path / "patch.json"
    atomic_write_json(path, {"status": "PLANNING", "tasks": [task()]})
    subprocess.run([str(launcher), "update", str(project), str(path), "--expected-revision", "0"], check=True)
    result = subprocess.run([str(launcher), "context", str(project)], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["ready"] == ["T01"]
