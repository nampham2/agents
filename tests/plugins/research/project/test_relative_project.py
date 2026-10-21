"""c7 of retro 2026-10-21-001: `update .` from inside the project directory.

A relative project argument keeps an empty basename, so the state validator compared `''` against
the `project` field and refused with "project field must match the project directory name". The CLI
now resolves the argument before dispatch, so every project-scoped command works from inside the
directory; the absolute spelling is unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import workspace_lib as lib
from workspace_lib import record_evidence_result
from workspace_operations import task_operation
from workspace_session import update_project_data

from tests.plugins.research.project.test_workflow_automation import fill_spec, invoke, task


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Relative project", working_directory=target, create_root=True
    )
    fill_spec(created)
    update_project_data(created, {"status": "PLANNING", "tasks": [task("T01")]}, expected_revision=0)
    return created


def test_update_dot_from_inside_the_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    code, out, err = invoke(
        ["update", ".", "-", "--expected-revision", "1", "--json"],
        stdin=json.dumps({"title": "Renamed from inside"}),
    )
    assert code == 0, out + err
    assert lib.load_json(project / "project.json")["title"] == "Renamed from inside"
    assert "project field must match" not in out + err


def test_task_finish_dot_from_inside_the_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_operation(project, "start", "T01", expected_revision=1)
    result = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    monkeypatch.chdir(project)
    code, out, err = invoke(
        ["task", ".", "finish", "T01", "--evidence", result.record_id, "--expected-revision", "2"]
    )
    assert code == 0, out + err
    assert lib.load_json(project / "project.json")["tasks"][0]["status"] == "DONE"
    assert "project field must match" not in out + err


def test_context_and_read_accept_the_dot(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    for arguments in (["context", "."], ["read", ".", "spec"]):
        code, out, err = invoke(arguments)
        assert code == 0, out + err


def test_the_absolute_spelling_still_works(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    code, out, err = invoke(
        ["update", str(project), "-", "--expected-revision", "1", "--json"],
        stdin=json.dumps({"title": "Renamed absolutely"}),
    )
    assert code == 0, out + err
    assert lib.load_json(project / "project.json")["title"] == "Renamed absolutely"


def test_the_invocation_log_is_written_for_the_dot(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    assert invoke(["context", "."])[0] == 0
    lines = [
        json.loads(line)
        for line in (project / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines and lines[-1]["command"] == "context"
