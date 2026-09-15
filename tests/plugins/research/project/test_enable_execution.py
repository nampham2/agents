"""Tests for Phase 5 PR A additions: validate_v4_state, v4 dispatch in check/commit_candidate,
enable_execution(), and the enable-execution CLI subcommand."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from workspace_lib import (
    READER_ONLY_REFUSAL,
    ValidationReport,
    WorkspaceConflict,
    WorkspaceError,
    check_candidate,
    commit_candidate,
    enable_execution,
    validate_v4_state,
)

TIMESTAMP = "2026-08-28T10:00:00+02:00"


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_workspace(root: Path) -> tuple[Path, Path, Path]:
    """Return (workspace_root, project_dir, target_dir) with v4-compatible files."""
    ws = root / "ws"
    project_dir = ws / "2026-08-28-001"
    target_dir = root / "target"
    project_dir.mkdir(parents=True)
    target_dir.mkdir(exist_ok=True)
    for sub in ("reviews", "tasks", "artifacts"):
        (project_dir / sub).mkdir(exist_ok=True)
    content = "# fn\n\n## Current specification\n\nContent.\n\n## Decision history\n\nDecision.\n"
    for fn in ("spec.md", "evidence.md", "reflection.md", "briefing.md"):
        (project_dir / fn).write_text(content, encoding="utf-8")
    (target_dir / "out.txt").write_text("result\n", encoding="utf-8")
    return ws, project_dir, target_dir


def _base_task(task_id: str = "T01", status: str = "DONE") -> dict:
    return {
        "id": task_id,
        "name": "Produce output",
        "status": status,
        "depends_on": [],
        "outputs": [{"root": "target", "path": "out.txt", "required": True}],
        "success_criteria": "Output exists.",
        "verification": "Inspect out.txt.",
        "evidence": [{"root": "workspace", "path": "evidence.md", "anchor": task_id}],
        "effect": {"kind": "none", "description": None},
        "authorization": {
            "required": False,
            "status": "not_required",
            "scope": None,
            "source": None,
            "authorized_at": None,
        },
        "receipts": [],
        "skip_reason": None,
        "block_reason": None,
    }


def _base_v4_state(project_dir: Path, target_dir: Path, status: str = "PLANNING") -> dict:
    return {
        "schema_version": 4,
        "project": project_dir.name,
        "title": "Test",
        "status": status,
        "created": TIMESTAMP,
        "updated": TIMESTAMP,
        "working_directory": str(target_dir),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": [],
        "execution": {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        },
    }


def _base_v3_state(project_dir: Path, target_dir: Path) -> dict:
    return {
        "schema_version": 3,
        "project": project_dir.name,
        "title": "Test",
        "status": "PLANNING",
        "created": TIMESTAMP,
        "updated": TIMESTAMP,
        "working_directory": str(target_dir),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": [],
    }


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


# ===========================================================================
# validate_v4_state — v4-specific execution block
# ===========================================================================


class ValidateV4StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)

    def _state(self, **kwargs) -> dict:
        s = _base_v4_state(self.project_dir, self.target_dir)
        s.update(kwargs)
        return s

    def _exec(self, **kwargs) -> dict:
        base = {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        }
        base.update(kwargs)
        return base

    # ---- happy path --------------------------------------------------------

    def test_valid_planning_no_errors(self) -> None:
        report = validate_v4_state(self._state(), self.project_dir, check_files=True)
        self.assertEqual([], report.errors)

    def test_valid_done_with_close(self) -> None:
        task = _base_task("T01", "DONE")
        s = self._state(
            status="DONE",
            tasks=[task],
            review={"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        )
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertEqual([], report.errors)

    def test_valid_done_with_review_cycle(self) -> None:
        (self.project_dir / "reviews" / "review_01.md").write_text(
            "# Review\n\n## Feedback\n\nLooks good.\n", encoding="utf-8"
        )
        task = _base_task("T01", "DONE")
        s = self._state(
            status="DONE",
            tasks=[task],
            review={
                "cycle": 1,
                "required": True,
                "status": "accepted",
                "evidence": [{"root": "workspace", "path": "reviews/review_01.md", "anchor": "Review"}],
            },
        )
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertEqual([], report.errors)

    # ---- schema_version -------------------------------------------------------

    def test_schema_version_not_4_rejected(self) -> None:
        s = self._state(schema_version=3)
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("schema_version must be 4", report.errors)

    # ---- execution block structure --------------------------------------------

    def test_execution_not_dict_rejected(self) -> None:
        s = self._state(execution="bad")
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project: execution must be an object", report.errors)

    def test_execution_missing_field_rejected(self) -> None:
        s = self._state(execution={"coordinator_run": None, "ownership_generation": 0, "attempts": {}})
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("protocol_version" in e for e in report.errors))

    def test_execution_unexpected_field_rejected(self) -> None:
        exec_ = self._exec(extra_field="oops")
        s = self._state(execution=exec_)
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("extra_field" in e for e in report.errors))

    # ---- protocol_version ----------------------------------------------------

    def test_protocol_version_zero_rejected(self) -> None:
        s = self._state(execution=self._exec(protocol_version=0))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: protocol_version must be a positive integer", report.errors)

    def test_protocol_version_bool_rejected(self) -> None:
        s = self._state(execution=self._exec(protocol_version=True))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: protocol_version must be a positive integer", report.errors)

    def test_protocol_version_string_rejected(self) -> None:
        s = self._state(execution=self._exec(protocol_version="1"))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: protocol_version must be a positive integer", report.errors)

    # ---- coordinator_run -----------------------------------------------------

    def test_coordinator_run_empty_string_rejected(self) -> None:
        s = self._state(execution=self._exec(coordinator_run=""))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: coordinator_run must be null or a non-empty string", report.errors)

    def test_coordinator_run_null_accepted(self) -> None:
        s = self._state(execution=self._exec(coordinator_run=None))
        report = validate_v4_state(s, self.project_dir)
        self.assertNotIn("execution: coordinator_run must be null or a non-empty string", report.errors)

    def test_coordinator_run_non_empty_accepted(self) -> None:
        s = self._state(execution=self._exec(coordinator_run="run-001"))
        report = validate_v4_state(s, self.project_dir)
        self.assertNotIn("execution: coordinator_run must be null or a non-empty string", report.errors)

    # ---- ownership_generation ------------------------------------------------

    def test_ownership_generation_negative_rejected(self) -> None:
        s = self._state(execution=self._exec(ownership_generation=-1))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: ownership_generation must be a non-negative integer", report.errors)

    def test_ownership_generation_bool_rejected(self) -> None:
        s = self._state(execution=self._exec(ownership_generation=False))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: ownership_generation must be a non-negative integer", report.errors)

    def test_ownership_generation_string_rejected(self) -> None:
        s = self._state(execution=self._exec(ownership_generation="0"))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: ownership_generation must be a non-negative integer", report.errors)

    # ---- attempts ------------------------------------------------------------

    def test_attempts_not_dict_rejected(self) -> None:
        s = self._state(execution=self._exec(attempts=[]))
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("execution: attempts must be an object", report.errors)

    # ---- shared v3 validations in v4 context ---------------------------------

    def test_empty_title_rejected(self) -> None:
        s = self._state(title="")
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("title" in e for e in report.errors))

    def test_project_mismatch_rejected(self) -> None:
        s = self._state()
        s["project"] = "wrong-project-name"
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project field must match the project directory name", report.errors)

    def test_non_canonical_id_generates_warning(self) -> None:
        custom_dir = self.project_dir.parent / "custom-project"
        custom_dir.mkdir(exist_ok=True)
        content = "# fn\n\n## Current specification\n\nContent.\n\n## Decision history\n\nDecision.\n"
        for fn in ("spec.md", "evidence.md", "reflection.md", "briefing.md"):
            (custom_dir / fn).write_text(content, encoding="utf-8")
        for sub in ("reviews", "tasks", "artifacts"):
            (custom_dir / sub).mkdir(exist_ok=True)
        s = _base_v4_state(custom_dir, self.target_dir)
        report = validate_v4_state(s, custom_dir)
        self.assertTrue(any("canonical" in w for w in report.warnings))

    def test_predecessor_empty_rejected(self) -> None:
        s = self._state(predecessor="")
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("predecessor" in e for e in report.errors))

    def test_invalid_created_timestamp_rejected(self) -> None:
        s = self._state(created="not-a-date")
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("created" in e for e in report.errors))

    def test_revision_negative_rejected(self) -> None:
        s = self._state(revision=-1)
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("revision" in e for e in report.errors))

    def test_invalid_status_rejected(self) -> None:
        s = self._state(status="BOGUS")
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_relative_working_directory_rejected(self) -> None:
        s = self._state(working_directory="relative/path")
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project: working_directory must be absolute", report.errors)

    def test_working_directory_nonexistent_rejected(self) -> None:
        s = self._state(working_directory="/nonexistent/xyz/abc")
        report = validate_v4_state(s, self.project_dir, check_files=True)
        self.assertTrue(any("working_directory does not exist" in e for e in report.errors))

    # ---- current_tasks -------------------------------------------------------

    def test_current_tasks_invalid_type_rejected(self) -> None:
        s = self._state(current_tasks="T01")
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("current_tasks" in e for e in report.errors))

    def test_current_tasks_duplicates_rejected(self) -> None:
        task = _base_task("T01", "RUNNING")
        s = self._state(status="EXECUTING", tasks=[task], current_tasks=["T01", "T01"])
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project: current_tasks contains duplicates", report.errors)

    def test_current_tasks_mismatch_rejected(self) -> None:
        task = _base_task("T01", "RUNNING")
        s = self._state(status="EXECUTING", tasks=[task], current_tasks=[])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("does not match RUNNING tasks" in e for e in report.errors))

    def test_current_tasks_unknown_task_rejected(self) -> None:
        s = self._state(current_tasks=["UNKNOWN"])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("unknown tasks" in e for e in report.errors))

    # ---- review block --------------------------------------------------------

    def test_review_not_dict_rejected(self) -> None:
        s = self._state(review="bad")
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project: review must be an object", report.errors)

    def test_review_cycle_invalid_rejected(self) -> None:
        s = self._state(review={"cycle": -1, "required": False, "status": "not_required", "evidence": []})
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("review: cycle must be a non-negative integer", report.errors)

    def test_review_required_not_bool_rejected(self) -> None:
        s = self._state(review={"cycle": 0, "required": "yes", "status": "not_required", "evidence": []})
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("review: required must be a boolean", report.errors)

    def test_review_status_invalid_rejected(self) -> None:
        s = self._state(review={"cycle": 0, "required": False, "status": "BOGUS", "evidence": []})
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_review_required_cannot_use_not_required_rejected(self) -> None:
        s = self._state(review={"cycle": 0, "required": True, "status": "not_required", "evidence": []})
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("review: required review cannot use status 'not_required'", report.errors)

    def test_review_evidence_not_list_rejected(self) -> None:
        s = self._state(review={"cycle": 0, "required": False, "status": "not_required", "evidence": "bad"})
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("review: evidence must be a list", report.errors)

    def test_review_accepted_requires_cycle_and_evidence(self) -> None:
        s = self._state(review={"cycle": 0, "required": False, "status": "accepted", "evidence": []})
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("requires a cycle and evidence" in e for e in report.errors))

    def test_close_review_required_not_accepted_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        s = self._state(
            status="DONE",
            tasks=[task],
            review={"cycle": 0, "required": True, "status": "pending", "evidence": []},
        )
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("required review" in e for e in report.errors))

    def test_close_review_pending_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        s = self._state(
            status="DONE",
            tasks=[task],
            review={"cycle": 0, "required": False, "status": "pending", "evidence": []},
        )
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertIn("project cannot close with a pending review", report.errors)

    # ---- tasks ---------------------------------------------------------------

    def test_tasks_not_list_rejected(self) -> None:
        s = self._state(tasks="bad")
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("project: tasks must be a list", report.errors)

    def test_close_no_tasks_rejected(self) -> None:
        s = self._state(status="DONE", tasks=[])
        report = validate_v4_state(s, self.project_dir, close=True)
        self.assertIn("project cannot close without at least one task", report.errors)

    def test_task_not_dict_rejected(self) -> None:
        s = self._state(tasks=["not-a-dict"])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("task must be an object" in e for e in report.errors))

    def test_task_id_empty_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["id"] = ""
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("id must be a non-empty string" in e for e in report.errors))

    def test_duplicate_task_id_rejected(self) -> None:
        task1 = _base_task("T01", "DONE")
        task2 = _base_task("T01", "DONE")
        s = self._state(tasks=[task1, task2])
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("duplicate task ID: T01", report.errors)

    def test_invalid_task_status_rejected(self) -> None:
        task = _base_task("T01", "BAD")
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("invalid task status" in e for e in report.errors))

    def test_empty_task_name_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["name"] = ""
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("name must be a non-empty string" in e for e in report.errors))

    def test_outputs_not_list_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["outputs"] = "bad"
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("outputs must be a list" in e for e in report.errors))

    def test_evidence_not_list_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["evidence"] = "bad"
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("evidence must be a list" in e for e in report.errors))

    def test_receipts_not_list_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["receipts"] = "bad"
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("receipts must be a list" in e for e in report.errors))

    def test_receipt_loop_covered(self) -> None:
        task = _base_task("T01", "DONE")
        task["receipts"] = [{"kind": "url", "value": "https://example.com/pr/1"}]
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir, check_files=True)
        self.assertIsInstance(report, ValidationReport)

    def test_done_without_evidence_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["evidence"] = []
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("DONE task requires evidence" in e for e in report.errors))

    def test_done_external_without_receipt_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["effect"] = {"kind": "external", "description": "Does something external"}
        task["receipts"] = []
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("durable receipt" in e for e in report.errors))

    def test_skipped_without_reason_rejected(self) -> None:
        task = _base_task("T01", "SKIPPED")
        task["evidence"] = []
        task["skip_reason"] = None
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("skip_reason" in e for e in report.errors))

    def test_non_skipped_with_reason_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["skip_reason"] = "some reason"
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("skip_reason must be null" in e for e in report.errors))

    def test_blocked_without_reason_rejected(self) -> None:
        task = _base_task("T01", "BLOCKED")
        task["block_reason"] = None
        task["evidence"] = []
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("block_reason" in e for e in report.errors))

    def test_non_blocked_with_reason_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        task["block_reason"] = "some reason"
        s = self._state(tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("block_reason must be null" in e for e in report.errors))

    # ---- cancellation --------------------------------------------------------

    def test_cancelled_without_reason_rejected(self) -> None:
        s = self._state(status="CANCELLED", cancellation_reason=None)
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("CANCELLED project requires cancellation_reason", report.errors)

    def test_cancelled_with_running_rejected(self) -> None:
        task = _base_task("T01", "RUNNING")
        s = self._state(
            status="CANCELLED",
            tasks=[task],
            current_tasks=["T01"],
            cancellation_reason="stopped",
        )
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("CANCELLED project cannot have RUNNING tasks", report.errors)

    def test_non_cancelled_with_reason_rejected(self) -> None:
        s = self._state(status="PLANNING", cancellation_reason="oops")
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("cancellation_reason must be null unless project is CANCELLED", report.errors)

    def test_planning_with_running_rejected(self) -> None:
        task = _base_task("T01", "RUNNING")
        s = self._state(status="PLANNING", tasks=[task], current_tasks=["T01"])
        report = validate_v4_state(s, self.project_dir)
        self.assertTrue(any("cannot have RUNNING tasks" in e for e in report.errors))

    def test_blocked_no_blocked_task_rejected(self) -> None:
        task = _base_task("T01", "DONE")
        s = self._state(status="BLOCKED", tasks=[task])
        report = validate_v4_state(s, self.project_dir)
        self.assertIn("BLOCKED project must contain at least one BLOCKED task", report.errors)

    # ---- close: incomplete tasks ---------------------------------------------

    def test_close_incomplete_tasks_rejected(self) -> None:
        task = _base_task("T01", "RUNNING")
        s = self._state(status="EXECUTING", tasks=[task], current_tasks=["T01"])
        report = validate_v4_state(s, self.project_dir, close=True)
        self.assertTrue(any("non-terminal tasks" in e for e in report.errors))

    # ---- close: spec.md section validation (lines 1678, 1680) ---------------

    def test_missing_spec_section_rejected_at_close(self) -> None:
        (self.project_dir / "spec.md").write_text("# spec.md\n\nNo sections.\n", encoding="utf-8")
        task = _base_task("T01", "DONE")
        s = self._state(status="DONE", tasks=[task])
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("missing '## Current specification'" in e for e in report.errors))

    def test_empty_spec_section_rejected_at_close(self) -> None:
        (self.project_dir / "spec.md").write_text(
            "# spec.md\n\n## Current specification\n\n## Decision history\n\nDecision.\n",
            encoding="utf-8",
        )
        task = _base_task("T01", "DONE")
        s = self._state(status="DONE", tasks=[task])
        report = validate_v4_state(s, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("must not be empty" in e for e in report.errors))


# ===========================================================================
# check_candidate — v4 dispatch
# ===========================================================================


class CheckCandidateV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)

    def _write_project(self, state: dict) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _candidate(self, state: dict) -> Path:
        path = Path(self.tmp.name) / "candidate.json"
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return path

    def test_reader_only_refusal_when_attempts_nonempty(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"]["attempts"] = {"run-001": {"status": "running"}}
        self._write_project(current)
        candidate = copy.deepcopy(current)
        report = check_candidate(self.project_dir, self._candidate(candidate))
        self.assertIn(READER_ONLY_REFUSAL, report.errors)

    def test_reader_only_refusal_when_coordinator_run_set(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"]["coordinator_run"] = "run-001"
        self._write_project(current)
        candidate = copy.deepcopy(current)
        report = check_candidate(self.project_dir, self._candidate(candidate))
        self.assertIn(READER_ONLY_REFUSAL, report.errors)

    def test_execution_not_dict_falls_back_and_passes_guard(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"] = "broken"
        self._write_project(current)
        candidate = copy.deepcopy(current)
        candidate["execution"] = {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        }
        report = check_candidate(self.project_dir, self._candidate(candidate))
        self.assertNotIn(READER_ONLY_REFUSAL, report.errors)

    def test_idle_v4_passes_check(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        self._write_project(current)
        candidate = copy.deepcopy(current)
        candidate["title"] = "Updated title"
        report = check_candidate(self.project_dir, self._candidate(candidate))
        self.assertNotIn(READER_ONLY_REFUSAL, report.errors)


# ===========================================================================
# commit_candidate — v4 dispatch
# ===========================================================================


class CommitCandidateV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)

    def _write_project(self, state: dict) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _candidate(self, state: dict) -> Path:
        path = Path(self.tmp.name) / "candidate.json"
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return path

    def test_reader_only_refusal_attempts_raises(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"]["attempts"] = {"run-001": {"status": "running"}}
        self._write_project(current)
        candidate = copy.deepcopy(current)
        with self.assertRaises(WorkspaceError) as ctx:
            commit_candidate(self.project_dir, self._candidate(candidate), expected_revision=0)
        self.assertIn(READER_ONLY_REFUSAL, str(ctx.exception))

    def test_reader_only_refusal_coordinator_run_raises(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"]["coordinator_run"] = "run-001"
        self._write_project(current)
        candidate = copy.deepcopy(current)
        with self.assertRaises(WorkspaceError) as ctx:
            commit_candidate(self.project_dir, self._candidate(candidate), expected_revision=0)
        self.assertIn(READER_ONLY_REFUSAL, str(ctx.exception))

    def test_execution_not_dict_falls_back_and_passes_guard(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        current["execution"] = "broken"
        self._write_project(current)
        candidate = copy.deepcopy(current)
        candidate["execution"] = {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        }
        committed = commit_candidate(self.project_dir, self._candidate(candidate), expected_revision=0)
        self.assertEqual(1, committed["revision"])

    def test_idle_v4_commits_successfully(self) -> None:
        current = _base_v4_state(self.project_dir, self.target_dir)
        self._write_project(current)
        candidate = copy.deepcopy(current)
        candidate["title"] = "Updated title"
        committed = commit_candidate(self.project_dir, self._candidate(candidate), expected_revision=0)
        self.assertEqual(1, committed["revision"])
        self.assertEqual("Updated title", committed["title"])


# ===========================================================================
# enable_execution()
# ===========================================================================


class EnableExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)

    def _write_v3(self, revision: int = 0) -> None:
        state = _base_v3_state(self.project_dir, self.target_dir)
        state["revision"] = revision
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _write_v4(self, revision: int = 1, generation: int = 0) -> None:
        state = _base_v4_state(self.project_dir, self.target_dir)
        state["revision"] = revision
        state["execution"]["ownership_generation"] = generation
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _config_path(self) -> Path:
        return self.project_dir / "execution" / "config.json"

    def test_missing_attestation_rejected(self) -> None:
        self._write_v3()
        with self.assertRaises(WorkspaceError) as ctx:
            enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=False)
        self.assertIn("R-LEGACY-WRITER", str(ctx.exception))

    def test_stale_revision_rejected(self) -> None:
        self._write_v3(revision=5)
        with self.assertRaises(WorkspaceConflict):
            enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)

    def test_already_v4_with_config_idempotent(self) -> None:
        self._write_v4(revision=1, generation=0)
        self._config_path().parent.mkdir(parents=True, exist_ok=True)
        self._config_path().write_text(json.dumps({"stub": True}), encoding="utf-8")
        import io
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            enable_execution(self.project_dir, expected_revision=1, legacy_writers_quiesced=True)
            self.assertIn("already enabled", mock_out.getvalue())

    def test_already_v4_without_config_raises(self) -> None:
        self._write_v4(revision=1, generation=0)
        with self.assertRaises(WorkspaceError) as ctx:
            enable_execution(self.project_dir, expected_revision=1, legacy_writers_quiesced=True)
        self.assertIn("corrupt", str(ctx.exception))

    def test_r_no_atomic_link(self) -> None:
        self._write_v3()

        def failing_link(src: str, dst: str) -> None:
            raise OSError("no hardlink support")

        with patch("os.link", side_effect=failing_link):
            with self.assertRaises(WorkspaceError) as ctx:
                enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        self.assertIn("R-NO-ATOMIC-LINK", str(ctx.exception))

    def test_r_cross_volume(self) -> None:
        self._write_v3()
        import os

        real_stat = os.stat

        def fake_stat(path, **kwargs):
            result = real_stat(path, **kwargs)
            # Return a stat result with a different st_dev for tempfile.gettempdir()
            import tempfile
            if str(path) == tempfile.gettempdir():
                class _FakeStat:
                    def __init__(self, real):
                        self.__dict__.update(real.__class__.__dict__)
                        self._real = real

                    @property
                    def st_dev(self):
                        return self._real.st_dev + 999999

                    def __getattr__(self, name):
                        return getattr(self._real, name)

                return _FakeStat(result)
            return result

        with patch("os.stat", side_effect=fake_stat):
            with self.assertRaises(WorkspaceError) as ctx:
                enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        self.assertIn("R-CROSS-VOLUME", str(ctx.exception))

    def test_config_mismatch_raises(self) -> None:
        self._write_v3()
        self._config_path().parent.mkdir(parents=True, exist_ok=True)
        self._config_path().write_text(json.dumps({"wrong": "config"}), encoding="utf-8")
        with self.assertRaises(WorkspaceError) as ctx:
            enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        self.assertIn("config mismatch", str(ctx.exception))

    def test_scratch_cleanup_rmdir_ignored(self) -> None:
        """os.rmdir silently fails when scratch_dir is non-empty (line 3991 covered)."""
        self._write_v3()
        scratch_dir = self.project_dir / "execution" / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        # Leave a file that survives probe cleanup so rmdir fails silently
        (scratch_dir / "persistent.txt").write_text("keep", encoding="utf-8")
        # enable_execution should still succeed despite the non-empty scratch dir
        enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(4, state["schema_version"])
        # cleanup
        (scratch_dir / "persistent.txt").unlink(missing_ok=True)

    def test_crash_prefix_config_matches(self) -> None:
        """Config written on first run survives; second run sees it and proceeds."""
        self._write_v3()
        enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        # Restore project.json to v3 (simulating a crash after config.json was written)
        self._write_v3(revision=0)
        # Second run: config.json already exists and matches → proceeds to commit
        enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(4, state["schema_version"])

    def test_concurrent_activation_inside_lock(self) -> None:
        """load_json returns v4 state on second call (inside lock) → prints 'already enabled'."""
        self._write_v3()
        v3_state = _base_v3_state(self.project_dir, self.target_dir)
        v4_state = _base_v4_state(self.project_dir, self.target_dir)
        v4_state["revision"] = 1
        call_count = [0]

        def fake_load(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return v3_state
            return v4_state

        import io
        with patch("workspace_lib.load_json", side_effect=fake_load):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
                self.assertIn("already enabled", mock_out.getvalue())

    def test_revision_conflict_inside_lock(self) -> None:
        """load_json returns different revision inside lock → WorkspaceConflict."""
        self._write_v3()
        v3_outer = _base_v3_state(self.project_dir, self.target_dir)
        v3_inner = _base_v3_state(self.project_dir, self.target_dir)
        v3_inner["revision"] = 99  # different revision, not v4
        call_count = [0]

        def fake_load(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return v3_outer
            return v3_inner

        with patch("workspace_lib.load_json", side_effect=fake_load):
            with self.assertRaises(WorkspaceConflict):
                enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)

    def test_validate_v4_fails_raises(self) -> None:
        """validate_v4_state returns errors → WorkspaceError 'v4 state validation failed'."""
        state = _base_v3_state(self.project_dir, self.target_dir)
        state["working_directory"] = "/nonexistent/path/xyz/abc"
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        with self.assertRaises(WorkspaceError) as ctx:
            enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        self.assertIn("v4 state validation failed", str(ctx.exception))

    def test_happy_path_upgrades_to_v4(self) -> None:
        """Full successful activation v3 → v4."""
        self._write_v3()
        enable_execution(self.project_dir, expected_revision=0, legacy_writers_quiesced=True)
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(4, state["schema_version"])
        self.assertEqual(1, state["revision"])
        exec_ = state["execution"]
        self.assertEqual(1, exec_["protocol_version"])
        self.assertIsNone(exec_["coordinator_run"])
        self.assertEqual(0, exec_["ownership_generation"])
        self.assertEqual({}, exec_["attempts"])
        self.assertTrue(self._config_path().exists())


# ===========================================================================
# enable-execution CLI handler
# ===========================================================================


class EnableExecutionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)
        state = _base_v3_state(self.project_dir, self.target_dir)
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def test_enable_execution_cli_succeeds(self) -> None:
        result = _call_manage(
            [
                "enable-execution",
                str(self.project_dir),
                "--expected-revision", "0",
                "--legacy-writers-quiesced",
            ]
        )
        self.assertEqual(0, result)
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(4, state["schema_version"])

    def test_enable_execution_cli_error_returns_1(self) -> None:
        result = _call_manage(
            [
                "enable-execution",
                str(self.project_dir),
                "--expected-revision", "0",
                # no --legacy-writers-quiesced → R-LEGACY-WRITER error
            ]
        )
        self.assertEqual(1, result)


if __name__ == "__main__":
    unittest.main()
