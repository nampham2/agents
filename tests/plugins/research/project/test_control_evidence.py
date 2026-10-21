"""c3 of retro 2026-10-21-001: a deliberately failing control record.

A guard is proven by a check that fails on bad input, but `task finish` refused every failed record,
so the control could not be cited. `record-evidence --control` marks the expected failure, finish
accepts exactly that marker, an ordinary failure is still refused, and finalize warns when the
control is the task's only evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import workspace_lib as lib
from workspace_evidence import evidence_entries, failing_evidence_warnings, newly_terminal_task_errors
from workspace_lib import WorkspaceError, record_evidence, record_evidence_result
from workspace_operations import task_operation
from workspace_session import update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import REFLECTION, request
from tests.plugins.research.project.test_workflow_automation import fill_spec, invoke, task

FAIL = [sys.executable, "-c", "raise SystemExit(3)"]
PASS = [sys.executable, "-c", "print('ok')"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Control evidence", working_directory=target, create_root=True
    )
    fill_spec(created)
    update_project_data(created, {"status": "PLANNING", "tasks": [task("T01")]}, expected_revision=0)
    return created


def _start(project: Path) -> None:
    task_operation(project, "start", "T01", expected_revision=1)


def test_a_control_failure_is_recorded_and_listed(project: Path) -> None:
    _start(project)
    result = record_evidence_result(project, "T01", FAIL, control=True)
    assert (result.control, result.exit_code, result.as_dict()["passed"]) == (True, 3, False)
    (entry,) = evidence_entries(project, task_id="T01")["entries"]
    assert entry["control"] is True and entry["passed"] is False
    assert "- Control: true" in (project / "evidence.md").read_text(encoding="utf-8")


def test_an_ordinary_failure_is_recorded_without_the_marker(project: Path) -> None:
    _start(project)
    result = record_evidence_result(project, "T01", FAIL)
    assert result.control is False
    assert "- Control: true" not in (project / "evidence.md").read_text(encoding="utf-8")


def test_finish_accepts_a_control_and_still_refuses_an_ordinary_failure(project: Path) -> None:
    _start(project)
    ordinary = record_evidence_result(project, "T01", FAIL)
    with pytest.raises(WorkspaceError, match="did not pass"):
        task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[ordinary.record_id])
    control = record_evidence_result(project, "T01", FAIL, control=True)
    finished = task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[control.record_id])
    assert finished["changed_tasks"] == [{"id": "T01", "status": "DONE"}]


def test_a_passing_control_is_refused_and_records_nothing(project: Path) -> None:
    _start(project)
    with pytest.raises(WorkspaceError, match="observed failing"):
        record_evidence_result(project, "T01", PASS, control=True)
    assert "Control: true" not in (project / "evidence.md").read_text(encoding="utf-8")


def test_a_dry_run_control_reports_the_marker_without_a_verdict(project: Path) -> None:
    _start(project)
    payload = record_evidence_result(project, "T01", FAIL, control=True, dry_run=True).as_dict()
    assert (payload["control"], payload["dry_run"], payload["passed"]) == (True, True, None)
    assert "Control: true" not in (project / "evidence.md").read_text(encoding="utf-8")


def test_the_shared_terminal_check_accepts_a_control(project: Path, current_mode: str) -> None:
    _start(project)
    control = record_evidence_result(project, "T01", FAIL, control=True)
    state = lib.load_json(project / "project.json")
    candidate = {**state, "tasks": [{**state["tasks"][0], "status": "DONE", "evidence": [control.reference]}]}
    assert newly_terminal_task_errors(project, state, candidate) == ([], [])


def test_the_shared_terminal_check_still_refuses_an_ordinary_failure(project: Path, current_mode: str) -> None:
    _start(project)
    ordinary = record_evidence_result(project, "T01", FAIL)
    state = lib.load_json(project / "project.json")
    candidate = {**state, "tasks": [{**state["tasks"][0], "status": "DONE", "evidence": [ordinary.reference]}]}
    errors, _ = newly_terminal_task_errors(project, state, candidate)
    assert errors and "did not pass" in errors[0]


def test_failing_evidence_warnings_names_a_control_only_task(project: Path) -> None:
    _start(project)
    control = record_evidence_result(project, "T01", FAIL, control=True)
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[control.record_id])
    state = lib.load_json(project / "project.json")
    warnings = failing_evidence_warnings(project, state)
    assert len(warnings) == 1 and "T01" in warnings[0]


def test_failing_evidence_warnings_is_empty_for_a_passing_task(project: Path) -> None:
    _start(project)
    passing = record_evidence_result(project, "T01", PASS)
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[passing.record_id])
    state = lib.load_json(project / "project.json")
    assert failing_evidence_warnings(project, state) == []


def test_failing_evidence_warnings_ignores_a_task_without_references(project: Path) -> None:
    state = lib.load_json(project / "project.json")
    assert failing_evidence_warnings(project, state) == []


def test_failing_evidence_warnings_survives_a_malformed_state(project: Path) -> None:
    assert failing_evidence_warnings(project, {"tasks": "not a list"}) == []
    assert failing_evidence_warnings(project, {"tasks": [{"id": "T01", "status": "DONE", "evidence": "x"}]}) == []


def test_finalize_surfaces_the_failing_only_warning(project: Path) -> None:
    _start(project)
    control = record_evidence_result(project, "T01", FAIL, control=True)
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[control.record_id])
    result = workflow(
        project,
        "finalize",
        request(project, "final", reflection=REFLECTION, continuation={"next": "none"}),
    )
    assert result["failing_evidence_warnings"] and "T01" in result["failing_evidence_warnings"][0]


def test_finalize_has_no_failing_only_warning_after_a_pass(project: Path) -> None:
    _start(project)
    passing = record_evidence_result(project, "T01", PASS)
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[passing.record_id])
    result = workflow(
        project,
        "finalize",
        request(project, "final", reflection=REFLECTION, continuation={"next": "none"}),
    )
    assert result["failing_evidence_warnings"] == []


def test_the_cli_records_a_control_and_exits_zero(project: Path) -> None:
    _start(project)
    code, out, _ = invoke(
        ["record-evidence", str(project), "--task", "T01", "--control", "--json", "--", *FAIL]
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["control"] is True and payload["passed"] is False
    assert "Control: true" in (project / "evidence.md").read_text(encoding="utf-8")


def test_the_cli_prints_the_control_message_without_json(project: Path) -> None:
    _start(project)
    code, out, _ = invoke(["record-evidence", str(project), "--task", "T01", "--control", "--", *FAIL])
    assert code == 0
    assert "deliberate control failure" in out


def test_the_cli_refuses_a_control_that_passed(project: Path) -> None:
    _start(project)
    code, _, err = invoke(["record-evidence", str(project), "--task", "T01", "--control", "--", *PASS])
    assert code == 1
    assert "observed failing" in err
    assert "Control: true" not in (project / "evidence.md").read_text(encoding="utf-8")


def test_record_evidence_wrapper_passes_the_control_flag(project: Path) -> None:
    _start(project)
    assert record_evidence(project, "T01", FAIL, control=True) == 3
    assert "- Control: true" in (project / "evidence.md").read_text(encoding="utf-8")
