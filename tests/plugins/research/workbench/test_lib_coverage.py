"""Additional tests to achieve 100% coverage for src/agents/workspace/lib.py."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.workspace.lib import (
    DirectoryLock,
    ValidationReport,
    WorkspaceError,
    _fsync_directory,
    _is_timestamp,
    _legacy_title,
    _reference_from_v2,
    _resolve_local_reference,
    _unexpected_fields,
    allocate_project,
    apply_migration,
    atomic_write_text,
    check_state_transition,
    commit_candidate,
    detect_schema,
    is_external_reference,
    load_json,
    migrate_v1_state,
    migrate_v2_state,
    migration_candidate,
    render_index,
    validate_legacy_v1,
    validate_project,
    validate_v2_state,
    validate_v3_state,
)

TIMESTAMP = "2026-08-28T10:00:00+02:00"


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_workspace(root: Path) -> tuple[Path, Path, Path]:
    """Return (workspace_root, project_dir, target_dir) with minimal v3 files."""
    ws = root / "ws"
    project_dir = ws / "2026-08-28-001"
    target_dir = root / "target"
    project_dir.mkdir(parents=True)
    target_dir.mkdir(exist_ok=True)
    for sub in ("reviews", "tasks", "artifacts"):
        (project_dir / sub).mkdir(exist_ok=True)
    for fn in ("spec.md", "evidence.md", "reflection.md"):
        (project_dir / fn).write_text(
            f"# {fn}\n\n## Current specification\n\nContent.\n\n## Decision history\n\nDecision.\n",
            encoding="utf-8",
        )
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


def _base_state(project_dir: Path, target_dir: Path, status: str = "DONE") -> dict:
    return {
        "schema_version": 3,
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
        "tasks": [_base_task()],
    }


# ===========================================================================
# ValidationReport
# ===========================================================================


class ValidationReportTests(unittest.TestCase):
    def test_extend_merges_both_lists(self) -> None:
        r1 = ValidationReport(errors=["e1"], warnings=["w1"])
        r2 = ValidationReport(errors=["e2"], warnings=["w2"])
        r1.extend(r2)
        self.assertEqual(["e1", "e2"], r1.errors)
        self.assertEqual(["w1", "w2"], r1.warnings)

    def test_valid_property_true_when_no_errors(self) -> None:
        self.assertTrue(ValidationReport(errors=[]).valid)

    def test_valid_property_false_when_errors_exist(self) -> None:
        self.assertFalse(ValidationReport(errors=["bad"]).valid)


# ===========================================================================
# Utility functions
# ===========================================================================


class UtilityTests(unittest.TestCase):
    # _is_timestamp
    def test_is_timestamp_rejects_none(self) -> None:
        self.assertFalse(_is_timestamp(None))

    def test_is_timestamp_rejects_integer(self) -> None:
        self.assertFalse(_is_timestamp(123))

    def test_is_timestamp_rejects_bad_format(self) -> None:
        self.assertFalse(_is_timestamp("not-a-date"))

    # is_external_reference whitespace
    def test_is_external_reference_rejects_whitespace(self) -> None:
        self.assertFalse(is_external_reference("https://example.com/ path"))
        self.assertFalse(is_external_reference("receipt:abc def"))

    # load_json error paths
    def test_load_json_raises_on_missing_file(self) -> None:
        with self.assertRaises(WorkspaceError):
            load_json(Path("/nonexistent/path/file.json"))

    def test_load_json_raises_on_non_dict_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            fh.write("[1, 2, 3]")
            path = Path(fh.name)
        try:
            with self.assertRaises(WorkspaceError):
                load_json(path)
        finally:
            path.unlink(missing_ok=True)

    # _fsync_directory OSError path
    def test_fsync_directory_ignores_oserror(self) -> None:
        _fsync_directory(Path("/nonexistent_dir_for_fsync_test"))  # should not raise

    # atomic_write_text exception cleanup
    def test_atomic_write_text_cleans_up_temp_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            with patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "content")
            leftover = list(Path(tmp).glob(".out.txt.*.tmp"))
            self.assertEqual([], leftover)

    # _unexpected_fields non-empty extras
    def test_unexpected_fields_reports_extras(self) -> None:
        report = ValidationReport()
        _unexpected_fields({"a": 1, "extra": 2}, {"a"}, "test", report)
        self.assertTrue(any("unexpected fields: extra" in e for e in report.errors))

    # _resolve_local_reference absolute path rejection
    def test_resolve_local_reference_rejects_absolute_path(self) -> None:
        resolved, error = _resolve_local_reference(Path("/tmp/ws"), Path("/tmp/tgt"), "workspace", "/absolute")
        self.assertIsNone(resolved)
        self.assertIn("must be relative", error)

    def test_resolve_local_reference_rejects_dotdot(self) -> None:
        resolved, error = _resolve_local_reference(Path("/tmp/ws"), Path("/tmp/tgt"), "workspace", "../escape")
        self.assertIsNone(resolved)
        self.assertIn("must be relative", error)


# ===========================================================================
# DirectoryLock error paths
# ===========================================================================


class DirectoryLockErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_owner_json_write_failure_releases_lock_dir(self) -> None:
        lock_path = self.root / ".lock"
        with patch("agents.workspace.lib.atomic_write_json", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                with DirectoryLock(lock_path):
                    pass
        self.assertFalse(lock_path.exists())

    def test_exit_raises_workspace_error_when_rmdir_fails(self) -> None:
        lock_path = self.root / ".lock"
        lock_path.mkdir()
        (lock_path / "unexpected").write_text("blocking rmdir", encoding="utf-8")
        lock = DirectoryLock(lock_path)
        lock.acquired = True
        with self.assertRaises(WorkspaceError):
            lock.__exit__(None, None, None)


# ===========================================================================
# validate_v3_state — error branch coverage
# ===========================================================================


class ValidateV3StateBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, self.project_dir, self.target_dir = _make_workspace(Path(self.tmp.name))

    def _state(self, **overrides) -> dict:
        s = _base_state(self.project_dir, self.target_dir)
        for key, value in overrides.items():
            s[key] = value
        return s

    # --- project-level field errors ---

    def test_predecessor_empty_string_rejected(self) -> None:
        state = self._state()
        state["predecessor"] = ""
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("predecessor" in e for e in report.errors))

    def test_invalid_created_timestamp(self) -> None:
        report = validate_v3_state(self._state(created="bad-date"), self.project_dir)
        self.assertTrue(any("created" in e for e in report.errors))

    def test_revision_as_bool_rejected(self) -> None:
        report = validate_v3_state(self._state(revision=True), self.project_dir)
        self.assertTrue(any("revision" in e for e in report.errors))

    def test_revision_negative_rejected(self) -> None:
        report = validate_v3_state(self._state(revision=-1), self.project_dir)
        self.assertTrue(any("revision" in e for e in report.errors))

    def test_relative_working_directory_rejected(self) -> None:
        report = validate_v3_state(self._state(working_directory="relative/path"), self.project_dir)
        self.assertTrue(any("working_directory must be absolute" in e for e in report.errors))

    def test_working_directory_not_existing(self) -> None:
        report = validate_v3_state(self._state(working_directory="/nonexistent/path"), self.project_dir)
        self.assertTrue(any("working_directory does not exist" in e for e in report.errors))

    def test_duplicate_current_tasks_rejected(self) -> None:
        state = self._state(status="EXECUTING")
        task = _base_task("T01", "RUNNING")
        state["tasks"] = [task]
        state["current_tasks"] = ["T01", "T01"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("current_tasks contains duplicates" in e for e in report.errors))

    # --- review errors ---

    def test_review_not_dict_rejected(self) -> None:
        report = validate_v3_state(self._state(review="bad"), self.project_dir)
        self.assertTrue(any("review must be an object" in e for e in report.errors))

    def test_review_cycle_not_integer(self) -> None:
        state = self._state()
        state["review"] = {"cycle": "x", "required": False, "status": "not_required", "evidence": []}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("cycle must be a non-negative integer" in e for e in report.errors))

    def test_review_required_not_bool(self) -> None:
        state = self._state()
        state["review"] = {"cycle": 0, "required": "yes", "status": "not_required", "evidence": []}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("required must be a boolean" in e for e in report.errors))

    def test_invalid_review_status(self) -> None:
        state = self._state()
        state["review"] = {"cycle": 0, "required": False, "status": "BOGUS", "evidence": []}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_required_review_cannot_be_not_required(self) -> None:
        state = self._state()
        state["review"] = {"cycle": 0, "required": True, "status": "not_required", "evidence": []}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("not_required" in e for e in report.errors))

    def test_review_evidence_not_list(self) -> None:
        state = self._state()
        state["review"] = {"cycle": 0, "required": False, "status": "not_required", "evidence": "bad"}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("evidence must be a list" in e for e in report.errors))

    def test_accepted_review_requires_cycle_and_evidence(self) -> None:
        state = self._state()
        state["review"] = {"cycle": 0, "required": False, "status": "accepted", "evidence": []}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("requires a cycle and evidence" in e for e in report.errors))

    # --- tasks errors ---

    def test_tasks_not_list_rejected(self) -> None:
        report = validate_v3_state(self._state(tasks="bad"), self.project_dir)
        self.assertTrue(any("tasks must be a list" in e for e in report.errors))

    def test_close_without_tasks_rejected(self) -> None:
        state = self._state(tasks=[], status="DONE")
        report = validate_v3_state(state, self.project_dir, close=True, check_files=False)
        self.assertTrue(any("without at least one task" in e for e in report.errors))

    def test_non_dict_task_rejected(self) -> None:
        report = validate_v3_state(self._state(tasks=["not a dict"]), self.project_dir)
        self.assertTrue(any("task must be an object" in e for e in report.errors))

    def test_empty_task_id_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["id"] = ""
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("id must be a non-empty string" in e for e in report.errors))

    def test_duplicate_task_id_rejected(self) -> None:
        t1 = _base_task("T01")
        t2 = _base_task("T01")
        state = self._state(tasks=[t1, t2])
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("duplicate task ID" in e for e in report.errors))

    def test_invalid_task_status_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["status"] = "BOGUS"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_empty_task_name_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["name"] = ""
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("name must be a non-empty string" in e for e in report.errors))

    # --- output reference errors ---

    def test_output_not_dict_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = ["not_a_dict"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("output must be an object" in e for e in report.errors))

    def test_output_invalid_root_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = [{"root": "INVALID", "path": "x.txt", "required": True}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid root" in e for e in report.errors))

    def test_output_empty_path_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = [{"root": "workspace", "path": "", "required": True}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("path must be a non-empty string" in e for e in report.errors))

    def test_output_required_not_bool_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = [{"root": "workspace", "path": "x.txt", "required": "yes"}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("required must be a boolean" in e for e in report.errors))

    # --- evidence reference errors ---

    def test_evidence_not_dict_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = ["not_a_dict"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("evidence must be an object" in e for e in report.errors))

    def test_evidence_invalid_root_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "BAD", "path": "e.md", "anchor": None}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid root" in e for e in report.errors))

    def test_evidence_empty_path_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "workspace", "path": "", "anchor": None}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("path must be a non-empty string" in e for e in report.errors))

    def test_evidence_empty_string_anchor_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "workspace", "path": "evidence.md", "anchor": ""}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("anchor must be null or a non-empty string" in e for e in report.errors))

    def test_external_evidence_invalid_reference_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "external", "path": "not-a-url", "anchor": None}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid external evidence reference" in e for e in report.errors))

    # --- effect errors ---

    def test_effect_not_dict_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["effect"] = "bad"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("effect must be an object" in e for e in report.errors))

    def test_non_none_effect_requires_description(self) -> None:
        state = self._state()
        state["tasks"][0]["effect"] = {"kind": "local_write", "description": None}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("non-none effects require a description" in e for e in report.errors))

    def test_none_effect_must_have_null_description(self) -> None:
        state = self._state()
        state["tasks"][0]["effect"] = {"kind": "none", "description": "oops"}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("none effect must have a null description" in e for e in report.errors))

    # --- authorization errors ---

    def test_authorization_not_dict_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"] = "bad"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("authorization must be an object" in e for e in report.errors))

    def test_authorization_required_not_bool_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"]["required"] = "yes"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("required must be a boolean" in e for e in report.errors))

    def test_authorization_invalid_status_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"]["status"] = "BOGUS"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_not_required_authorization_must_use_not_required_status(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"]["required"] = False
        state["tasks"][0]["authorization"]["status"] = "pending"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("non-required authorization" in e for e in report.errors))

    def test_required_authorization_cannot_use_not_required_status(self) -> None:
        state = self._state()
        state["tasks"][0]["effect"] = {"kind": "external", "description": "publish"}
        state["tasks"][0]["authorization"]["required"] = True
        state["tasks"][0]["authorization"]["status"] = "not_required"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("cannot use status 'not_required'" in e for e in report.errors))

    def test_explicit_authorization_requires_scope_and_source(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"] = {
            "required": True,
            "status": "explicit",
            "scope": None,
            "source": None,
            "authorized_at": TIMESTAMP,
        }
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("explicit authorization requires scope" in e for e in report.errors))

    def test_explicit_authorization_requires_timezone_aware_timestamp(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"] = {
            "required": True,
            "status": "explicit",
            "scope": "Test",
            "source": "User",
            "authorized_at": "2026-01-01",  # no timezone
        }
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("timezone-aware authorized_at" in e for e in report.errors))

    def test_source_set_when_status_not_explicit_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"] = {
            "required": False,
            "status": "not_required",
            "scope": None,
            "source": "somebody",
            "authorized_at": None,
        }
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("source and authorized_at must be null" in e for e in report.errors))

    def test_done_task_requiring_authorization_must_be_explicit(self) -> None:
        state = self._state()
        state["tasks"][0]["authorization"] = {
            "required": True,
            "status": "pending",
            "scope": None,
            "source": None,
            "authorized_at": None,
        }
        state["tasks"][0]["effect"] = {"kind": "external", "description": "publish"}
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("requires explicit authorization" in e for e in report.errors))

    # --- receipt errors ---

    def test_receipt_not_dict_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["receipts"] = ["not_a_dict"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("receipt must be an object" in e for e in report.errors))

    def test_receipt_value_not_durable_reference_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["receipts"] = [
            {"kind": "publish", "value": "not-a-url", "destination": "dest", "timestamp": TIMESTAMP}
        ]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("durable prefixed identifier" in e for e in report.errors))

    def test_receipt_missing_timestamp_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["receipts"] = [
            {"kind": "publish", "value": "https://example.com/r", "destination": "d", "timestamp": "bad"}
        ]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("timezone-aware ISO-8601" in e for e in report.errors))

    # --- dependency errors ---

    def test_unknown_dependency_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["depends_on"] = ["UNKNOWN"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("unknown dependency UNKNOWN" in e for e in report.errors))

    def test_dependency_cycle_rejected(self) -> None:
        t1 = _base_task("T01", "TODO")
        t2 = _base_task("T02", "TODO")
        t1["depends_on"] = ["T02"]
        t2["depends_on"] = ["T01"]
        report = validate_v3_state(self._state(tasks=[t1, t2]), self.project_dir)
        self.assertTrue(any("dependency cycle" in e for e in report.errors))

    # --- skip/block errors ---

    def test_skipped_task_requires_skip_reason(self) -> None:
        state = self._state()
        state["tasks"][0]["status"] = "SKIPPED"
        state["tasks"][0]["skip_reason"] = None
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("SKIPPED task requires skip_reason" in e for e in report.errors))

    def test_skip_reason_present_for_non_skipped_task_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["skip_reason"] = "Unexpected"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("skip_reason must be null" in e for e in report.errors))

    def test_blocked_task_requires_block_reason(self) -> None:
        state = self._state(status="BLOCKED")
        state["tasks"][0]["status"] = "BLOCKED"
        state["tasks"][0]["block_reason"] = None
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("BLOCKED task requires block_reason" in e for e in report.errors))

    def test_block_reason_present_for_non_blocked_task_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["block_reason"] = "Unexpected"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("block_reason must be null" in e for e in report.errors))

    # --- current_tasks mismatch ---

    def test_unknown_current_task_rejected(self) -> None:
        state = self._state()
        state["current_tasks"] = ["UNKNOWN"]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("unknown tasks" in e for e in report.errors))

    # --- cancellation errors ---

    def test_cancelled_project_requires_cancellation_reason(self) -> None:
        state = self._state(status="CANCELLED")
        state["tasks"][0]["status"] = "DONE"
        state["cancellation_reason"] = None
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("CANCELLED project requires cancellation_reason" in e for e in report.errors))

    def test_non_cancelled_project_must_not_have_cancellation_reason(self) -> None:
        state = self._state(status="PLANNING", tasks=[])
        state["cancellation_reason"] = "Unexpected reason"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("cancellation_reason must be null" in e for e in report.errors))

    def test_blocked_project_requires_blocked_task(self) -> None:
        state = self._state(status="BLOCKED")
        state["tasks"][0]["status"] = "TODO"  # no BLOCKED tasks
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("must contain at least one BLOCKED task" in e for e in report.errors))

    def test_done_project_cannot_have_running_tasks(self) -> None:
        state = self._state(status="DONE")
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]
        report = validate_v3_state(state, self.project_dir, close=True)
        self.assertTrue(any("cannot have RUNNING tasks" in e for e in report.errors))

    # --- spec.md section checks at close ---

    def test_missing_spec_section_rejected_at_close(self) -> None:
        (self.project_dir / "spec.md").write_text("# spec.md\n\nNo sections.\n", encoding="utf-8")
        state = _base_state(self.project_dir, self.target_dir)
        report = validate_v3_state(state, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("missing '## Current specification'" in e for e in report.errors))

    def test_empty_spec_section_rejected_at_close(self) -> None:
        (self.project_dir / "spec.md").write_text(
            "# spec.md\n\n## Current specification\n\n## Decision history\n\nDecision.\n",
            encoding="utf-8",
        )
        state = _base_state(self.project_dir, self.target_dir)
        report = validate_v3_state(state, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("must not be empty" in e for e in report.errors))

    def test_done_task_requires_evidence(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = []
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("DONE task requires evidence" in e for e in report.errors))

    def test_external_output_invalid_path_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = [{"root": "external", "path": "not-a-url", "required": True}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("invalid external reference" in e for e in report.errors))

    def test_evidence_dotdot_path_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "workspace", "path": "../escape.md", "anchor": None}]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("must be relative" in e for e in report.errors))

    def test_done_task_evidence_file_not_found(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = [{"root": "workspace", "path": "nonexistent.md", "anchor": None}]
        report = validate_v3_state(state, self.project_dir, check_files=True)
        self.assertTrue(any("evidence file does not exist" in e for e in report.errors))

    def test_receipt_empty_kind_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["receipts"] = [
            {"kind": "", "value": "https://example.com/r", "destination": "d", "timestamp": TIMESTAMP}
        ]
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("kind must be a non-empty string" in e for e in report.errors))

    def test_duplicate_depends_on_rejected(self) -> None:
        t1 = _base_task("T01", "DONE")
        t2 = _base_task("T02", "DONE")
        t2["depends_on"] = ["T01", "T01"]
        state = self._state(tasks=[t1, t2])
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("depends_on contains duplicates" in e for e in report.errors))

    def test_wrong_schema_version_in_v3_state(self) -> None:
        state = self._state(schema_version=2)
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("schema_version must be 3" in e for e in report.errors))

    def test_empty_title_rejected(self) -> None:
        state = self._state(title="")
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("title must be a non-empty string" in e for e in report.errors))

    def test_project_name_mismatch_rejected(self) -> None:
        state = self._state(project="wrong_name")
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("project field must match" in e for e in report.errors))

    def test_review_evidence_item_is_validated(self) -> None:
        state = self._state()
        state["review"] = {
            "cycle": 1, "required": False, "status": "accepted",
            "evidence": [{"root": "workspace", "path": "../escape.md", "anchor": None}],
        }
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("must be relative" in e for e in report.errors))

    def test_task_outputs_not_list_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["outputs"] = 42
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("outputs must be a list" in e for e in report.errors))

    def test_task_evidence_not_list_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["evidence"] = 42
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("evidence must be a list" in e for e in report.errors))

    def test_task_receipts_not_list_rejected(self) -> None:
        state = self._state()
        state["tasks"][0]["receipts"] = 42
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("receipts must be a list" in e for e in report.errors))

    def test_cancelled_project_with_running_task_rejected(self) -> None:
        state = self._state(status="CANCELLED")
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]
        state["cancellation_reason"] = "Abandoned"
        report = validate_v3_state(state, self.project_dir)
        self.assertTrue(any("CANCELLED project cannot have RUNNING tasks" in e for e in report.errors))


# ===========================================================================
# validate_v2_state
# ===========================================================================


class ValidateV2StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, self.project_dir, self.target_dir = _make_workspace(Path(self.tmp.name))

    def _v2(self, **overrides) -> dict:
        base = {
            "schema_version": 2,
            "project": self.project_dir.name,
            "title": "Legacy",
            "status": "PLANNING",
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.target_dir),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [],
        }
        base.update(overrides)
        return base

    def test_valid_v2_has_warning(self) -> None:
        report = validate_v2_state(self._v2(), self.project_dir)
        self.assertFalse(report.errors)
        self.assertTrue(any("schema v2" in w for w in report.warnings))

    def test_wrong_schema_version(self) -> None:
        report = validate_v2_state(self._v2(schema_version=1), self.project_dir)
        self.assertTrue(any("schema_version must be 2" in e for e in report.errors))

    def test_missing_fields(self) -> None:
        report = validate_v2_state({"schema_version": 2}, self.project_dir)
        self.assertTrue(report.errors)

    def test_invalid_status(self) -> None:
        report = validate_v2_state(self._v2(status="BOGUS"), self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_invalid_review_cycle(self) -> None:
        report = validate_v2_state(self._v2(review_cycle="x"), self.project_dir)
        self.assertTrue(any("review_cycle must be a non-negative integer" in e for e in report.errors))

    def test_current_task_as_list_mismatch(self) -> None:
        report = validate_v2_state(self._v2(current_task=["T01"]), self.project_dir)
        self.assertTrue(any("does not match RUNNING tasks" in e for e in report.errors))

    def test_malformed_current_task(self) -> None:
        report = validate_v2_state(self._v2(current_task=12345), self.project_dir)
        self.assertTrue(any("current_task must be null" in e for e in report.errors))

    def test_non_dict_task_rejected(self) -> None:
        report = validate_v2_state(self._v2(tasks=["not_a_dict"]), self.project_dir)
        self.assertTrue(any("task must be an object" in e for e in report.errors))

    def test_invalid_task_id(self) -> None:
        task = {
            "id": None, "name": "T", "status": "TODO",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("id must be a non-empty string" in e for e in report.errors))

    def test_duplicate_task_id_rejected(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "TODO",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task, dict(task)]), self.project_dir)
        self.assertTrue(any("duplicate task ID" in e for e in report.errors))

    def test_invalid_external_effect_type(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "TODO",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": "yes",
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("external_effect must be a boolean" in e for e in report.errors))

    def test_invalid_depends_on_type(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "TODO",
            "depends_on": "not_list", "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("must be a list of non-empty strings" in e for e in report.errors))

    def test_done_task_requires_evidence(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task], status="DONE"), self.project_dir, check_files=False)
        self.assertTrue(any("DONE task requires evidence" in e for e in report.errors))

    def test_external_effect_done_without_explicit_auth(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": ["https://example.com/r"], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": True,
            "authorization": "pending", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task], current_task=None), self.project_dir, check_files=False)
        self.assertTrue(any("lacks explicit authorization" in e for e in report.errors))

    def test_skipped_task_requires_skip_reason(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "SKIPPED",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("SKIPPED task requires skip_reason" in e for e in report.errors))

    def test_close_with_non_terminal_tasks(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "TODO",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(
            self._v2(tasks=[task], status="DONE"), self.project_dir, close=True, check_files=False
        )
        self.assertTrue(any("non-terminal tasks" in e for e in report.errors))

    def test_done_task_output_file_checked(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": ["out.txt"], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        state = self._v2(tasks=[task], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True, check_files=True)
        self.assertFalse(report.errors)

    def test_done_task_missing_output_file(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": ["missing.txt"], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        state = self._v2(tasks=[task], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True, check_files=True)
        self.assertTrue(any("output does not exist" in e for e in report.errors))

    def test_invalid_task_status(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "BOGUS",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("invalid status" in e for e in report.errors))

    def test_task_name_empty_rejected(self) -> None:
        task = {
            "id": "T01", "name": "", "status": "TODO",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        report = validate_v2_state(self._v2(tasks=[task]), self.project_dir)
        self.assertTrue(any("name must be a non-empty string" in e for e in report.errors))

    def test_current_task_string_mismatches_running_ids(self) -> None:
        report = validate_v2_state(self._v2(current_task="T01"), self.project_dir)
        self.assertTrue(any("does not match RUNNING tasks" in e for e in report.errors))

    def test_non_done_task_skipped_in_close_file_loop(self) -> None:
        skipped = {
            "id": "T01", "name": "T", "status": "SKIPPED",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": [], "external_effect": False,
            "authorization": "not_required", "skip_reason": "Not needed",
        }
        done = {
            "id": "T02", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": [], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T02"], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        state = self._v2(tasks=[skipped, done], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True)
        self.assertFalse(report.errors)

    def test_non_list_outputs_skipped_in_close_file_loop(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": 42, "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        state = self._v2(tasks=[task], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True)
        self.assertTrue(any("must be a list of non-empty strings" in e for e in report.errors))

    def test_external_output_skipped_in_close_file_loop(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": ["https://example.com/pub"], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": True,
            "authorization": "explicit", "skip_reason": None,
        }
        state = self._v2(tasks=[task], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True)
        self.assertFalse(report.errors)

    def test_absolute_output_path_in_close_file_loop(self) -> None:
        abs_output = self.project_dir / "spec.md"
        task = {
            "id": "T01", "name": "T", "status": "DONE",
            "depends_on": [], "outputs": [str(abs_output)], "success_criteria": "x",
            "verification": "x", "evidence": ["evidence.md#T01"], "external_effect": False,
            "authorization": "not_required", "skip_reason": None,
        }
        state = self._v2(tasks=[task], status="DONE")
        report = validate_v2_state(state, self.project_dir, close=True)
        self.assertFalse(any("output does not exist" in e for e in report.errors))


# ===========================================================================
# validate_legacy_v1
# ===========================================================================


class ValidateLegacyV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "2026-08-28-001"
        self.project_dir.mkdir()

    def test_empty_meta_yaml_is_error(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("  ", encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        report = validate_legacy_v1(self.project_dir, close=False, allow_legacy_close=False)
        self.assertTrue(any("00_meta.yaml" in e for e in report.errors))

    def test_missing_task_plan_is_error(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        report = validate_legacy_v1(self.project_dir, close=False, allow_legacy_close=False)
        self.assertTrue(any("02_task_plan.md" in e for e in report.errors))

    def test_close_with_allow_legacy_requires_reflection(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        report = validate_legacy_v1(self.project_dir, close=True, allow_legacy_close=True)
        self.assertTrue(any("reflection.md" in e for e in report.errors))

    def test_close_with_allow_legacy_and_empty_reflection_is_error(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        (self.project_dir / "reflection.md").write_text("  ", encoding="utf-8")
        report = validate_legacy_v1(self.project_dir, close=True, allow_legacy_close=True)
        self.assertTrue(any("reflection.md" in e for e in report.errors))


# ===========================================================================
# detect_schema
# ===========================================================================


class DetectSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "project"
        self.project_dir.mkdir()

    def test_detects_v3(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        self.assertEqual(3, detect_schema(self.project_dir))

    def test_detects_v1_from_legacy_files(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        self.assertEqual(1, detect_schema(self.project_dir))

    def test_raises_when_no_schema(self) -> None:
        with self.assertRaises(WorkspaceError):
            detect_schema(self.project_dir)

    def test_raises_when_schema_version_not_integer(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": "v3"}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            detect_schema(self.project_dir)

    def test_raises_when_schema_version_is_bool(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": True}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            detect_schema(self.project_dir)


# ===========================================================================
# validate_project
# ===========================================================================


class ValidateProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_returns_error_when_schema_undetectable(self) -> None:
        project_dir = self.root / "empty"
        project_dir.mkdir()
        self.assertTrue(validate_project(project_dir).errors)

    def test_validates_v1_project(self) -> None:
        project_dir = self.root / "v1"
        project_dir.mkdir()
        (project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        self.assertFalse(validate_project(project_dir).errors)

    def test_validates_v2_project(self) -> None:
        project_dir = self.root / "v2"
        project_dir.mkdir()
        state = {
            "schema_version": 2, "project": project_dir.name, "title": "T",
            "status": "PLANNING", "created": "2026-01-01", "updated": TIMESTAMP,
            "working_directory": str(self.root), "current_task": None, "review_cycle": 0, "tasks": [],
        }
        (project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        report = validate_project(project_dir)
        self.assertFalse(report.errors)
        self.assertTrue(any("schema v2" in w for w in report.warnings))

    def test_unsupported_schema_version_returns_error(self) -> None:
        project_dir = self.root / "v99"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        self.assertTrue(validate_project(project_dir).errors)

    def test_workspace_error_in_inner_validate_returns_error(self) -> None:
        project_dir = self.root / "bad"
        project_dir.mkdir()
        # Invalid v3 project.json triggers WorkspaceError inside validate_v3_state
        (project_dir / "project.json").write_text("not json", encoding="utf-8")
        self.assertTrue(validate_project(project_dir).errors)

    def test_inner_workspace_error_caught_from_load_json(self) -> None:
        project_dir = self.root / "v2inner"
        project_dir.mkdir()
        state = {
            "schema_version": 2, "project": project_dir.name, "title": "T",
            "status": "PLANNING", "created": "2026-01-01", "updated": TIMESTAMP,
            "working_directory": str(self.root), "current_task": None, "review_cycle": 0, "tasks": [],
        }
        (project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        call_count = [0]
        original = load_json

        def raise_on_second(path: Path) -> dict:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise WorkspaceError("simulated load error")
            return original(path)

        with patch("agents.workspace.lib.load_json", side_effect=raise_on_second):
            report = validate_project(project_dir)
        self.assertTrue(any("simulated load error" in e for e in report.errors))


# ===========================================================================
# _legacy_title
# ===========================================================================


class LegacyTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "2026-08-28-001"
        self.project_dir.mkdir()

    def test_extracts_title_from_heading(self) -> None:
        (self.project_dir / "02_task_plan.md").write_text("# Task Plan — My Project\n\n", encoding="utf-8")
        self.assertEqual("My Project", _legacy_title(self.project_dir))

    def test_returns_dir_name_when_file_missing(self) -> None:
        self.assertEqual(self.project_dir.name, _legacy_title(self.project_dir))

    def test_returns_dir_name_when_no_heading(self) -> None:
        (self.project_dir / "02_task_plan.md").write_text("no headings here\n", encoding="utf-8")
        self.assertEqual(self.project_dir.name, _legacy_title(self.project_dir))


# ===========================================================================
# render_index
# ===========================================================================


class RenderIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir()

    def test_unreadable_project_json_shows_invalid(self) -> None:
        project_dir = self.workspace / "2026-01-01-001"
        project_dir.mkdir()
        (project_dir / "project.json").write_text("{{invalid json", encoding="utf-8")
        result = render_index(self.workspace)
        self.assertIn("Unreadable project.json", result)

    def test_legacy_v1_project_shows_legacy_row(self) -> None:
        project_dir = self.workspace / "2025-01-01-001"
        project_dir.mkdir()
        (project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (project_dir / "02_task_plan.md").write_text("# Task Plan - Legacy Title\n", encoding="utf-8")
        result = render_index(self.workspace)
        self.assertIn("LEGACY", result)
        self.assertIn("Legacy Title", result)


# ===========================================================================
# check_state_transition
# ===========================================================================


class CheckStateTransitionTests(unittest.TestCase):
    def _task(self, task_id: str, status: str = "TODO") -> dict:
        return {
            "id": task_id, "name": "T", "status": status, "depends_on": [],
            "outputs": [], "success_criteria": "x", "verification": "x", "evidence": [],
            "effect": {"kind": "none", "description": None},
            "authorization": {
                "required": False, "status": "not_required", "scope": None, "source": None, "authorized_at": None
            },
            "receipts": [], "skip_reason": None, "block_reason": None,
        }

    def test_removed_task_rejected(self) -> None:
        prev = {"status": "EXECUTING", "tasks": [self._task("T01")]}
        cand = {"status": "EXECUTING", "tasks": []}
        self.assertTrue(any("cannot be removed" in e for e in check_state_transition(prev, cand)))

    def test_invalid_task_transition_rejected(self) -> None:
        prev = {"status": "DONE", "tasks": [self._task("T01", "DONE")]}
        cand = {"status": "DONE", "tasks": [self._task("T01", "TODO")]}
        self.assertTrue(any("invalid transition DONE -> TODO" in e for e in check_state_transition(prev, cand)))

    def test_terminal_task_mutation_rejected(self) -> None:
        original = self._task("T01", "DONE")
        mutated = dict(original)
        mutated["name"] = "Changed"
        prev = {"status": "DONE", "tasks": [original]}
        cand = {"status": "DONE", "tasks": [mutated]}
        self.assertTrue(any("terminal task history is immutable" in e for e in check_state_transition(prev, cand)))

    def test_invalid_project_transition_rejected(self) -> None:
        prev = {"status": "DONE", "tasks": []}
        cand = {"status": "CANCELLED", "tasks": []}
        self.assertTrue(any("invalid project transition" in e for e in check_state_transition(prev, cand)))


# ===========================================================================
# commit_candidate error paths
# ===========================================================================


class CommitCandidateErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _, self.project_dir, self.target_dir = _make_workspace(root)
        self.state = {
            "schema_version": 3,
            "project": self.project_dir.name,
            "title": "Test",
            "status": "PLANNING",
            "created": TIMESTAMP,
            "updated": TIMESTAMP,
            "working_directory": str(self.target_dir),
            "revision": 0,
            "current_tasks": [],
            "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
            "cancellation_reason": None,
            "tasks": [],
        }
        (self.project_dir / "project.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _candidate(self, state: dict) -> Path:
        path = Path(self.tmp.name) / "candidate.json"
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return path

    def test_non_v3_project_raises(self) -> None:
        v2_state = dict(self.state, schema_version=2)
        (self.project_dir / "project.json").write_text(json.dumps(v2_state), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            commit_candidate(self.project_dir, self._candidate(self.state), expected_revision=0)

    def test_immutable_field_change_raises(self) -> None:
        cand = copy.deepcopy(self.state)
        cand["project"] = "changed"
        with self.assertRaises(WorkspaceError):
            commit_candidate(self.project_dir, self._candidate(cand), expected_revision=0)

    def test_transition_error_raises(self) -> None:
        cand = copy.deepcopy(self.state)
        cand["status"] = "CANCELLED"  # PLANNING → CANCELLED is invalid
        with self.assertRaises(WorkspaceError):
            commit_candidate(self.project_dir, self._candidate(cand), expected_revision=0)

    def test_validation_failure_raises(self) -> None:
        cand = copy.deepcopy(self.state)
        cand["status"] = "DONE"  # DONE with no tasks fails
        with self.assertRaises(WorkspaceError):
            commit_candidate(self.project_dir, self._candidate(cand), expected_revision=0)


# ===========================================================================
# allocate_project
# ===========================================================================


class AllocateProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "ws"
        self.target = Path(self.tmp.name) / "target"
        self.target.mkdir()

    def test_allocates_project(self) -> None:
        project_dir = allocate_project(self.workspace, title="Test Project", working_directory=self.target)
        self.assertTrue(project_dir.is_dir())
        state = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual("Test Project", state["title"])
        self.assertEqual("ALIGNING", state["status"])
        self.assertEqual(3, state["schema_version"])

    def test_sequential_allocation(self) -> None:
        first = allocate_project(self.workspace, title="First", working_directory=self.target)
        second = allocate_project(self.workspace, title="Second", working_directory=self.target)
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith("-002"))

    def test_creates_workspace_reflection_when_absent(self) -> None:
        allocate_project(self.workspace, title="Test", working_directory=self.target)
        self.assertTrue((self.workspace / "reflection.md").is_file())

    def test_does_not_overwrite_existing_reflection(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "reflection.md").write_text("# Existing\n\nOld.\n", encoding="utf-8")
        allocate_project(self.workspace, title="Test", working_directory=self.target)
        self.assertIn("Old", (self.workspace / "reflection.md").read_text(encoding="utf-8"))

    def test_empty_title_raises(self) -> None:
        with self.assertRaises(WorkspaceError):
            allocate_project(self.workspace, title="", working_directory=self.target)

    def test_nonexistent_working_directory_raises(self) -> None:
        with self.assertRaises(WorkspaceError):
            allocate_project(self.workspace, title="T", working_directory=Path("/nonexistent"))

    def test_base_exception_during_allocation_propagates(self) -> None:
        with patch("agents.workspace.lib.rebuild_index", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                allocate_project(self.workspace, title="Test", working_directory=self.target)


# ===========================================================================
# _reference_from_v2
# ===========================================================================


class ReferenceFromV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project_dir = root / "ws" / "project"
        self.project_dir.mkdir(parents=True)
        self.target_dir = root / "target"
        self.target_dir.mkdir()

    def test_external_reference_stays_external(self) -> None:
        ref = _reference_from_v2("https://example.com/r", self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("external", ref["root"])
        self.assertTrue(ref["required"])

    def test_absolute_path_under_project_becomes_workspace(self) -> None:
        output = self.project_dir / "out.txt"
        output.write_text("x\n", encoding="utf-8")
        ref = _reference_from_v2(str(output), self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("workspace", ref["root"])

    def test_absolute_path_under_target_becomes_target(self) -> None:
        output = self.target_dir / "out.txt"
        output.write_text("x\n", encoding="utf-8")
        ref = _reference_from_v2(str(output), self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("target", ref["root"])

    def test_absolute_path_outside_both_raises(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("x\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            _reference_from_v2(str(outside), self.project_dir, self.target_dir, evidence=False)

    def test_ambiguous_relative_path_raises(self) -> None:
        (self.project_dir / "overlap.txt").write_text("ws\n", encoding="utf-8")
        (self.target_dir / "overlap.txt").write_text("tgt\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            _reference_from_v2("overlap.txt", self.project_dir, self.target_dir, evidence=False)

    def test_relative_path_in_workspace_only(self) -> None:
        (self.project_dir / "ws_only.txt").write_text("x\n", encoding="utf-8")
        ref = _reference_from_v2("ws_only.txt", self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("workspace", ref["root"])

    def test_relative_path_in_target_only(self) -> None:
        (self.target_dir / "tgt_only.txt").write_text("x\n", encoding="utf-8")
        ref = _reference_from_v2("tgt_only.txt", self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("target", ref["root"])

    def test_known_workspace_pattern_becomes_workspace(self) -> None:
        ref = _reference_from_v2("spec.md", self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("workspace", ref["root"])

    def test_unknown_relative_path_defaults_to_target(self) -> None:
        ref = _reference_from_v2("unknown.txt", self.project_dir, self.target_dir, evidence=False)
        self.assertEqual("target", ref["root"])

    def test_evidence_reference_includes_anchor(self) -> None:
        ref = _reference_from_v2("evidence.md#T01", self.project_dir, self.target_dir, evidence=True)
        self.assertEqual("workspace", ref["root"])
        self.assertEqual("T01", ref["anchor"])

    def test_evidence_reference_without_anchor_has_none(self) -> None:
        ref = _reference_from_v2("evidence.md", self.project_dir, self.target_dir, evidence=True)
        self.assertIsNone(ref["anchor"])

    def test_same_project_and_target_dir_defaults_to_workspace(self) -> None:
        # When project_dir == working_directory, relative paths go to workspace
        ref = _reference_from_v2("somefile.txt", self.project_dir, self.project_dir, evidence=False)
        self.assertEqual("workspace", ref["root"])


# ===========================================================================
# migrate_v2_state
# ===========================================================================


class MigrateV2StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project_dir = root / "ws" / "project"
        self.project_dir.mkdir(parents=True)
        self.target_dir = root / "target"
        self.target_dir.mkdir()

    def _v2(self, **overrides) -> dict:
        base = {
            "schema_version": 2, "project": self.project_dir.name, "title": "Legacy",
            "status": "PLANNING", "created": "2026-01-01", "updated": TIMESTAMP,
            "working_directory": str(self.target_dir), "current_task": None, "review_cycle": 0, "tasks": [],
        }
        base.update(overrides)
        return base

    def test_non_object_task_raises(self) -> None:
        with self.assertRaises(WorkspaceError):
            migrate_v2_state(self._v2(tasks=["not_a_dict"]), self.project_dir)

    def test_cancelled_project_gets_cancellation_reason(self) -> None:
        result = migrate_v2_state(self._v2(status="CANCELLED"), self.project_dir)
        self.assertIsNotNone(result["cancellation_reason"])

    def test_review_cycle_adds_evidence(self) -> None:
        result = migrate_v2_state(self._v2(review_cycle=1), self.project_dir)
        self.assertEqual(1, len(result["review"]["evidence"]))

    def test_malformed_current_task_raises(self) -> None:
        with self.assertRaises(WorkspaceError):
            migrate_v2_state(self._v2(current_task=42), self.project_dir)

    def test_current_task_as_list(self) -> None:
        result = migrate_v2_state(self._v2(current_task=["T01"]), self.project_dir)
        self.assertEqual(["T01"], result["current_tasks"])

    def test_non_iso_created_coerced(self) -> None:
        result = migrate_v2_state(self._v2(created="2026-01-01"), self.project_dir)
        self.assertIn("+00:00", result["created"])

    def test_bad_created_uses_now(self) -> None:
        result = migrate_v2_state(self._v2(created="not-a-date"), self.project_dir)
        self.assertTrue(result["created"])

    def test_external_done_task_gets_receipt(self) -> None:
        task = {
            "id": "T01", "name": "T", "status": "DONE", "depends_on": [],
            "outputs": ["https://example.com/r"], "success_criteria": "x", "verification": "x",
            "evidence": ["evidence.md#T01"], "external_effect": True, "authorization": "explicit",
            "skip_reason": None,
        }
        result = migrate_v2_state(self._v2(tasks=[task], current_task=None, status="DONE"), self.project_dir)
        self.assertEqual(1, len(result["tasks"][0]["receipts"]))

    def test_current_task_string_becomes_single_item_list(self) -> None:
        result = migrate_v2_state(self._v2(current_task="T01"), self.project_dir)
        self.assertEqual(["T01"], result["current_tasks"])


# ===========================================================================
# migrate_v1_state
# ===========================================================================


class MigrateV1StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "project"
        self.project_dir.mkdir()

    def _write_v1(self, plan: str, meta: str = "legacy_schema: 1\nproject: project\ncreated: 2026-01-15\n") -> None:
        (self.project_dir / "00_meta.yaml").write_text(meta, encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text(plan, encoding="utf-8")

    def test_migrates_task_plan(self) -> None:
        self._write_v1(
            "# Task Plan — My Project\n\n"
            "## Task 01 — Do something\n\n"
            "- **Outputs:** `out.txt`\n"
            "- **Success criteria:** Output exists.\n"
        )
        result = migrate_v1_state(self.project_dir)
        self.assertEqual(3, result["schema_version"])
        self.assertEqual("ALIGNING", result["status"])
        self.assertEqual(1, len(result["tasks"]))
        self.assertEqual("Do something", result["tasks"][0]["name"])

    def test_created_date_coerced(self) -> None:
        self._write_v1("# Plan\n", meta="legacy_schema: 1\nproject: p\ncreated: 2026-01-15\n")
        result = migrate_v1_state(self.project_dir)
        self.assertIn("+00:00", result["created"])

    def test_bad_created_uses_now(self) -> None:
        self._write_v1("# Plan\n", meta="legacy_schema: 1\nproject: p\ncreated: not-a-date\n")
        result = migrate_v1_state(self.project_dir)
        self.assertTrue(result["created"])

    def test_task_without_outputs(self) -> None:
        self._write_v1("# Plan\n\n## Task 01 — No outputs\n\nSome text.\n")
        result = migrate_v1_state(self.project_dir)
        self.assertEqual([], result["tasks"][0]["outputs"])

    def test_task_without_success_criteria_gets_default(self) -> None:
        self._write_v1("# Plan\n\n## Task 01 — No criteria\n\n- **Outputs:** `out.txt`\n")
        result = migrate_v1_state(self.project_dir)
        self.assertIn("Reconcile", result["tasks"][0]["success_criteria"])


# ===========================================================================
# migration_candidate
# ===========================================================================


class MigrationCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "project"
        self.project_dir.mkdir()

    def test_v1_candidate(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text("legacy_schema: 1\nproject: project\n", encoding="utf-8")
        (self.project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        self.assertEqual(3, migration_candidate(self.project_dir)["schema_version"])

    def test_v2_candidate(self) -> None:
        state = {
            "schema_version": 2, "project": "project", "title": "T",
            "status": "PLANNING", "created": "2026-01-01", "updated": TIMESTAMP,
            "working_directory": str(self.project_dir), "current_task": None, "review_cycle": 0, "tasks": [],
        }
        (self.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(3, migration_candidate(self.project_dir)["schema_version"])

    def test_v3_raises(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            migration_candidate(self.project_dir)

    def test_unsupported_version_raises(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            migration_candidate(self.project_dir)

    def test_invalid_v2_state_raises(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            migration_candidate(self.project_dir)


# ===========================================================================
# apply_migration
# ===========================================================================


class ApplyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.project_dir = self.ws / "project"
        self.project_dir.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def _v2_state(self) -> dict:
        return {
            "schema_version": 2, "project": "project", "title": "T",
            "status": "PLANNING", "created": "2026-01-01", "updated": TIMESTAMP,
            "working_directory": str(self.target), "current_task": None, "review_cycle": 0, "tasks": [],
        }

    def test_apply_v2_migration(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(self._v2_state()), encoding="utf-8")
        result = apply_migration(self.project_dir)
        self.assertEqual((self.project_dir / "project.json").resolve(), result.resolve())
        migrated = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(3, migrated["schema_version"])
        self.assertTrue((self.project_dir / "project.v2.json").is_file())

    def test_apply_v2_fails_if_backup_exists(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(self._v2_state()), encoding="utf-8")
        (self.project_dir / "project.v2.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            apply_migration(self.project_dir)

    def test_apply_v1_migration_creates_spec(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text(
            "legacy_schema: 1\nproject: project\ncreated: 2026-01-01\n", encoding="utf-8"
        )
        (self.project_dir / "02_task_plan.md").write_text("# Task Plan — Legacy\n", encoding="utf-8")
        (self.project_dir / "01_problem_statement.md").write_text("Solve the problem.\n", encoding="utf-8")
        apply_migration(self.project_dir)
        spec = (self.project_dir / "spec.md").read_text(encoding="utf-8")
        self.assertIn("Solve the problem", spec)

    def test_migration_candidate_error_propagates(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            apply_migration(self.project_dir)

    def test_apply_migration_raises_when_candidate_invalid(self) -> None:
        done_state = dict(self._v2_state(), status="DONE")
        (self.project_dir / "project.json").write_text(json.dumps(done_state), encoding="utf-8")
        with self.assertRaises(WorkspaceError) as ctx:
            apply_migration(self.project_dir)
        self.assertIn("migration candidate is not valid", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
