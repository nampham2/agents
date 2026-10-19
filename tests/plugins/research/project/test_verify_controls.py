"""A verify batch reports the control it declared, and finalize asks for one that fired.

Retro candidate c1: a batch that only runs checks proves the checks can pass, never that they can
fail. The `control` is a check-shaped command declared to fail on the current content; a batch only
passes when every check recorded zero and the control was observed failing (a recorded non-zero
exit). Timeout, launch error and rejection carry no exit code, so they never count as firing.
Finalize then surfaces - as a warning, never an error - a project that verified without any control
ever observed failing, including legacy journals written before the key existed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import workspace_journal as journal
import workspace_lib as lib
from workspace_checks import control_warnings
from workspace_operations import task_operation
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import REFLECTION, planned, request

CHECK = {"task": "T01", "argv": [sys.executable, "-c", "print('checked')"]}
FAILING_CONTROL = {"task": "T01", "argv": [sys.executable, "-c", "raise SystemExit(3)"]}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Verify controls", working_directory=target, create_root=True
    )
    planned(created)
    return created


def batch(identity: str, **extra: Any) -> dict[str, Any]:
    return {"id": identity, "checks": [CHECK], **extra}


def finish(project: Path, record: str) -> None:
    """Close the planned project on that record; the finalize tests need a completable project."""
    task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[record])
    task_operation(project, "skip", "T02", expected_revision=3, reason="Superseded within scope")


def close(project: Path, identity: str = "final") -> dict[str, Any]:
    return workflow(
        project,
        "finalize",
        request(project, identity, reflection=REFLECTION, continuation={"next": "none"}),
    )


def test_a_batch_without_a_control_still_reports_zero_of_zero(project: Path) -> None:
    """Row 1: the response key is always present; the passed formula is untouched."""
    result = workflow(project, "verify", batch("plain"))
    assert result["passed"] is True
    assert result["controls_fired"] == "0/0"
    assert result["omitted_checks"] == 0


def test_a_control_observed_firing_passes_the_batch_and_records_its_exit(project: Path) -> None:
    """Row 2: checks pass and the control fails -> passed, 1/1, and the exit is in the evidence."""
    result = workflow(project, "verify", batch("fires", control=FAILING_CONTROL))
    assert result["passed"] is True
    assert result["controls_fired"] == "1/1"
    control_attempt = result["attempts"][1]
    assert control_attempt["control"] is True and control_attempt["result"]["exit_code"] == 3
    entry = workflow(project, "read", {"document": "evidence", "entry": control_attempt["result"]["record_id"]})
    assert "Exit code: 3" in entry["text"]
    saved = lib.load_json(journal.journal_path(project, "fires"))
    assert saved["attempts"][1]["control"] is True


def test_a_control_that_cannot_fire_fails_the_batch(project: Path) -> None:
    """Row 3: a guard that exits zero watched the checks pass and proved nothing."""
    silent = {"task": "T01", "argv": [sys.executable, "-c", "print('all clear')"]}
    result = workflow(project, "verify", batch("sleeps", control=silent))
    assert result["passed"] is False
    assert result["controls_fired"] == "0/1"


@pytest.mark.parametrize("mode", ["timeout", "launch_error", "rejected"])
def test_a_control_that_never_produces_an_exit_does_not_fire(project: Path, mode: str) -> None:
    """Row 4: no fabricated exit code means not fired, whatever the attempt status became."""
    control: dict[str, Any]
    if mode == "timeout":
        control = {
            "task": "T01",
            "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
            "timeout": 0.01,
        }
    elif mode == "launch_error":
        control = {"task": "T01", "argv": [str(project / "absent-executable")]}
    else:
        control = {"task": "unknown", "argv": [sys.executable, "-c", "pass"]}
    result = workflow(project, "verify", batch(mode.replace("_", "-"), control=control))
    assert result["passed"] is False
    assert result["controls_fired"] == "0/1"
    assert result["attempts"][1]["status"] == mode


def test_a_short_circuited_batch_never_reaches_the_control(project: Path) -> None:
    """Row 5: the failing check stops the batch; omitted_checks counts checks only."""
    broken = {"task": "T01", "argv": [sys.executable, "-c", "raise SystemExit(2)"]}
    result = workflow(
        project,
        "verify",
        {"id": "short", "checks": [broken, CHECK], "control": FAILING_CONTROL},
    )
    assert result["passed"] is False
    assert result["controls_fired"] == "0/1"
    assert len(result["attempts"]) == 1
    assert result["omitted_checks"] == 1
    # With every check run, the control attempt must not be counted as an omitted check.
    complete = workflow(
        project, "verify", {"id": "all-run", "checks": [CHECK], "control": FAILING_CONTROL}
    )
    assert complete["omitted_checks"] == 0 and len(complete["attempts"]) == 2


@pytest.mark.parametrize(
    "control",
    [
        {"argv": ["true"]},
        {"task": "T01", "step": "report", "argv": ["true"]},
        {"task": "T01", "argv": []},
        {"task": "T01", "argv": ["true"], "timeout": 0},
        {"task": "T01", "argv": ["true"], "timeout": 3601},
        {"task": "T01", "argv": [1]},
        {"task": "T01", "argv": ["true"], "bogus": True},
    ],
)
def test_a_malformed_control_records_nothing(project: Path, control: dict[str, Any]) -> None:
    """Row 6: the pre-pass validates the control before any journal exists."""
    with pytest.raises(lib.WorkspaceError, match="control"):
        workflow(project, "verify", batch("never-created", control=control))
    assert not journal.journal_path(project, "never-created").exists()


def test_a_same_id_retry_covers_the_control_and_repairs_its_evidence(project: Path) -> None:
    """Row 7: the signature binds the control, saved attempts validate, repair never reruns."""
    payload = batch("repair", control=FAILING_CONTROL)
    with patch.object(
        lib, "_append_evidence_entry", side_effect=[None, lib.WorkspaceError("disk full")]
    ):
        with pytest.raises(journal.OperationError) as failure:
            workflow(project, "verify", payload)
    partial = failure.value.result
    assert partial["attempts"][1]["control"] is True
    assert partial["attempts"][1]["status"] == "persistable"
    # Row 13: a partial result never claims a completed batch's verdict.
    assert "controls_fired" not in partial and "passed" not in partial
    with pytest.raises(lib.WorkspaceError, match="different input"):
        workflow(project, "verify", batch("repair", control={"task": "T01", "argv": ["true"]}))
    with patch.object(lib.subprocess, "run", side_effect=AssertionError("must not rerun")):
        result = workflow(project, "verify", payload)
    assert result["passed"] is True and result["controls_fired"] == "1/1"


def test_a_control_cannot_stand_in_for_the_checks(project: Path) -> None:
    """Row 8: checks stays required and nonempty; the control supplements a batch."""
    with pytest.raises(lib.WorkspaceError, match="checks"):
        workflow(project, "verify", {"id": "control-only", "control": FAILING_CONTROL})
    with pytest.raises(lib.WorkspaceError, match="nonempty"):
        workflow(project, "verify", {"id": "empty", "checks": [], "control": FAILING_CONTROL})


def test_finalize_without_any_verify_batch_warns_about_nothing(project: Path) -> None:
    """Row 9: no batches means there was nothing to require a control for."""
    task_operation(project, "start", "T01", expected_revision=1)
    record = lib.record_evidence_result(project, "T01", [sys.executable, "-c", "pass"]).record_id
    finish(project, record)
    assert close(project)["control_warnings"] == []


def test_finalize_names_batches_that_never_observed_a_control_firing(project: Path) -> None:
    """Row 10: a verify batch with no control observed failing is a warning, not a refusal."""
    task_operation(project, "start", "T01", expected_revision=1)
    record = workflow(project, "verify", batch("no-control"))["attempts"][0]["result"]["record_id"]
    finish(project, record)
    result = close(project)
    assert len(result["control_warnings"]) == 1
    assert "control" in result["control_warnings"][0]
    assert result["committed"] is True


def test_one_control_firing_in_any_batch_satisfies_finalize(project: Path) -> None:
    """Row 11: one observed failure somewhere is the proof the gate asks for."""
    task_operation(project, "start", "T01", expected_revision=1)
    plain = workflow(project, "verify", batch("earlier"))["attempts"][0]["result"]["record_id"]
    guarded = workflow(project, "verify", batch("guarded", control=FAILING_CONTROL))
    finish(project, plain)
    assert guarded["controls_fired"] == "1/1"
    assert close(project)["control_warnings"] == []


def test_a_legacy_or_unreadable_journal_is_read_as_never_fired(tmp_path: Path) -> None:
    """Row 10/12: pre-control layouts count as no observed firing; corrupt files never block close."""
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    project = lib.allocate_project(root, title="Legacy journals", working_directory=target, create_root=True)
    planned(project)
    operations = project / ".operations"
    operations.mkdir()
    legacy = {
        "kind": "checks",
        "id": "legacy",
        "signature": "x",
        "complete": True,
        "attempts": [{"index": 0, "status": "recorded", "result": {"exit_code": 0}, "lines": []}],
        "fingerprints": [],
    }
    lib.atomic_write_json(operations / "legacy.json", legacy)
    # A damaged attempt entry is skipped like the rest of a journal that cannot be trusted.
    damaged = json.loads(json.dumps(legacy))
    damaged["id"] = "damaged"
    damaged["attempts"].append("not-an-attempt")
    lib.atomic_write_json(operations / "damaged.json", damaged)
    (operations / "corrupt.json").write_text("{not json", encoding="utf-8")
    (operations / "foreign.json").write_text(json.dumps({"kind": "metadata", "attempts": []}), encoding="utf-8")
    warnings = control_warnings(project)
    assert len(warnings) == 1 and "observed failing" in warnings[0]
    # A recorded control that exited zero in an earlier attempt is still not an observed firing.
    unfired = json.loads(json.dumps(legacy))
    unfired["id"] = "unfired"
    unfired["attempts"].append(
        {"index": 1, "status": "recorded", "control": True, "result": {"exit_code": 0}, "lines": []}
    )
    lib.atomic_write_json(operations / "unfired.json", unfired)
    assert len(control_warnings(project)) == 1
    # Corrupt and foreign files alone never block nor warn: they are skipped, not repaired.
    for path in operations.glob("*.json"):
        path.unlink()
    (operations / "corrupt.json").write_text("{not json", encoding="utf-8")
    assert control_warnings(project) == []


def test_a_firing_control_recorded_only_as_an_attempt_still_reads_as_fired(tmp_path: Path) -> None:
    """Row 11's unit form: recorded, control-marked, non-zero exit is the whole condition."""
    target = tmp_path / "target"
    target.mkdir()
    project = lib.allocate_project(
        tmp_path / "ws", title="Firing journal", working_directory=target, create_root=True
    )
    operations = project / ".operations"
    operations.mkdir()
    lib.atomic_write_json(
        operations / "fired.json",
        {
            "kind": "checks",
            "id": "fired",
            "signature": "x",
            "complete": True,
            "attempts": [
                {"index": 0, "status": "recorded", "result": {"exit_code": 0}, "lines": []},
                {"index": 1, "status": "recorded", "control": True, "result": {"exit_code": 1}, "lines": []},
            ],
            "fingerprints": [],
        },
    )
    assert control_warnings(project) == []
