"""Regression tests for the issues found in the 2026-08-29 reviews.

Each class names the finding it pins down, so a future change that reintroduces one fails
against a test that says why the behaviour matters rather than merely that it changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import workspace_lib
from workspace_lib import (
    WorkspaceError,
    apply_migration,
    check_state_transition,
    commit_candidate,
    detect_schema,
    migrate_v2_state,
    render_index,
    validate_project,
    validate_v2_state,
    validate_v3_state,
)

from tests.conftest import MANAGER, REPO_ROOT, VALIDATOR

TIMESTAMP = "2026-08-28T10:00:00+02:00"


class MigrationAuthorizationTests(unittest.TestCase):
    """Finding #1: a coarse v2 marker must not pre-authorize an action that has not run."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_dir = self.root / "ws" / "project"
        self.project_dir.mkdir(parents=True)
        self.target = self.root / "target"
        self.target.mkdir()
        (self.project_dir / "spec.md").write_text(
            "# Release\n\n## Current specification\n\nShip it.\n\n## Decision history\n\n- init\n",
            encoding="utf-8",
        )
        (self.project_dir / "evidence.md").write_text("# Evidence\n\nnone yet\n", encoding="utf-8")

    def _migrate_task(self, **overrides: Any) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": "T1",
            "name": "Publish the release announcement",
            "status": "TODO",
            "depends_on": [],
            "outputs": [],
            "success_criteria": "announcement is live",
            "verification": "check the feed",
            "evidence": ["notes.md"] if overrides.get("status") == "DONE" else [],
            "external_effect": True,
            "authorization": "explicit",
            "skip_reason": None,
        }
        current_task = overrides.pop("current_task", None)
        task.update(overrides)
        self.state: dict[str, Any] = {
            "schema_version": 2,
            "project": "project",
            "title": "Release",
            "status": "EXECUTING",
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.target),
            "current_task": current_task,
            "review_cycle": 0,
            "tasks": [task],
        }
        # The fixture must be a *valid* v2 state, or the finding would be about invalid input.
        self.assertEqual([], validate_v2_state(self.state, self.project_dir, check_files=False).errors)
        self.migrated = migrate_v2_state(self.state, self.project_dir)
        return self.migrated["tasks"][0]

    def _migrate_authorization(self, **overrides: Any) -> dict[str, Any]:
        return self._migrate_task(**overrides)["authorization"]

    def test_pending_external_task_is_not_pre_authorized(self) -> None:
        # The action has not happened, so the v2 marker cannot stand in for consent to run it.
        authorization = self._migrate_authorization(status="TODO")
        self.assertEqual("pending", authorization["status"])
        self.assertIsNone(authorization["scope"])
        self.assertIsNone(authorization["source"])
        self.assertIsNone(authorization["authorized_at"])

    def test_blocked_external_task_is_not_pre_authorized(self) -> None:
        self.assertEqual("pending", self._migrate_authorization(status="BLOCKED")["status"])

    def test_skipped_external_task_is_not_pre_authorized(self) -> None:
        self.assertEqual(
            "pending", self._migrate_authorization(status="SKIPPED", skip_reason="dropped from scope")["status"]
        )

    def test_running_external_task_is_not_pre_authorized(self) -> None:
        # SKILL.md commits a task to RUNNING *before* performing the action, so RUNNING does not
        # establish that the external effect happened. Only DONE does.
        authorization = self._migrate_authorization(status="RUNNING", current_task="T1")
        self.assertEqual("pending", authorization["status"])
        self.assertIsNone(authorization["scope"])
        self.assertIsNone(authorization["source"])
        self.assertIsNone(authorization["authorized_at"])

    def test_done_external_task_keeps_its_authorization(self) -> None:
        self.assertEqual("explicit", self._migrate_authorization(status="DONE")["status"])

    def test_external_task_without_a_legacy_marker_stays_pending(self) -> None:
        self.assertEqual("pending", self._migrate_authorization(status="TODO", authorization="pending")["status"])

    def test_non_external_task_requires_no_authorization(self) -> None:
        authorization = self._migrate_authorization(external_effect=False, authorization="not_required")
        self.assertEqual("not_required", authorization["status"])
        self.assertFalse(authorization["required"])

    def test_running_external_task_is_parked_for_reconciliation(self) -> None:
        # v3 requires explicit authorization on a RUNNING task, so the task cannot simply stay
        # RUNNING with `pending`: it parks as BLOCKED, which is what forces a human to decide
        # whether the effect ran before anything resumes.
        task = self._migrate_task(status="RUNNING", current_task="T1")
        self.assertEqual("BLOCKED", task["status"])
        self.assertIn("re-authorize", task["block_reason"])
        # current_tasks must match the RUNNING set exactly, so the parked task drops out of it.
        self.assertEqual([], self.migrated["current_tasks"])
        report = validate_v3_state(self.migrated, self.project_dir, check_files=True)
        self.assertEqual([], report.errors)

    def test_running_external_task_without_a_marker_stays_migratable(self) -> None:
        # This state is valid v2 — v2 only demanded the marker for DONE tasks — so migration must
        # produce a valid v3 state rather than refusing the project outright.
        task = self._migrate_task(status="RUNNING", authorization="pending", current_task="T1")
        self.assertEqual("BLOCKED", task["status"])
        self.assertEqual("pending", task["authorization"]["status"])
        self.assertEqual([], validate_v3_state(self.migrated, self.project_dir, check_files=True).errors)

    def test_a_running_local_task_is_left_running(self) -> None:
        # Only *external* effects need per-action consent; local work is not parked.
        task = self._migrate_task(
            status="RUNNING", external_effect=False, authorization="not_required", current_task="T1"
        )
        self.assertEqual("RUNNING", task["status"])
        self.assertIsNone(task["block_reason"])
        self.assertEqual(["T1"], self.migrated["current_tasks"])
        self.assertEqual([], validate_v3_state(self.migrated, self.project_dir, check_files=True).errors)

    def test_a_blocked_task_keeps_its_own_reconciliation_note(self) -> None:
        task = self._migrate_task(status="BLOCKED")
        self.assertEqual("BLOCKED", task["status"])
        self.assertIn("original blocker", task["block_reason"])

    def test_downgraded_task_still_produces_a_valid_v3_state(self) -> None:
        state: dict[str, Any] = {
            "schema_version": 2,
            "project": "project",
            "title": "Release",
            "status": "EXECUTING",
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.target),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [
                {
                    "id": "T1",
                    "name": "Publish",
                    "status": "TODO",
                    "depends_on": [],
                    "outputs": [],
                    "success_criteria": "live",
                    "verification": "check",
                    "evidence": [],
                    "external_effect": True,
                    "authorization": "explicit",
                    "skip_reason": None,
                }
            ],
        }
        report = validate_v3_state(migrate_v2_state(state, self.project_dir), self.project_dir)
        self.assertEqual([], report.errors)


class MigrationAtomicityTests(unittest.TestCase):
    """Finding #4: an interrupted migration must stay retryable."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_dir = self.root / "ws" / "project"
        self.project_dir.mkdir(parents=True)

    def _write_v1(self) -> None:
        (self.project_dir / "00_meta.yaml").write_text(
            "legacy_schema: 1\nproject: project\ncreated: 2026-01-01\n", encoding="utf-8"
        )
        (self.project_dir / "02_task_plan.md").write_text("# Task Plan — Legacy\n", encoding="utf-8")
        (self.project_dir / "reflection.md").write_text("# Reflection\n\nnotes\n", encoding="utf-8")

    def _v2_state(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "project": "project",
            "title": "T",
            "status": "PLANNING",
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.root),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [],
        }

    def test_failure_while_writing_v3_files_leaves_the_project_migratable(self) -> None:
        self._write_v1()
        real = workspace_lib.atomic_write_text

        def fail_on_spec(path: Path, text: str) -> None:
            if path.name == "spec.md":
                raise OSError("disk full")
            real(path, text)

        with patch.object(workspace_lib, "atomic_write_text", side_effect=fail_on_spec):
            with self.assertRaises(OSError):
                apply_migration(self.project_dir)

        # The state file is the commit point: it must not exist yet.
        self.assertFalse((self.project_dir / "project.json").exists())
        self.assertEqual(1, detect_schema(self.project_dir))
        apply_migration(self.project_dir)
        self.assertEqual(3, detect_schema(self.project_dir))

    def test_retry_after_an_interrupted_v2_migration_succeeds(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(self._v2_state()), encoding="utf-8")
        real = workspace_lib.atomic_write_json

        def fail_on_state(path: Path, payload: dict[str, Any]) -> None:
            if path.name == "project.json":
                raise OSError("disk full")
            real(path, payload)

        with patch.object(workspace_lib, "atomic_write_json", side_effect=fail_on_state):
            with self.assertRaises(OSError):
                apply_migration(self.project_dir)

        # The v2 backup landed, so the retry must recognise it as its own and proceed.
        self.assertTrue((self.project_dir / "project.v2.json").is_file())
        self.assertEqual(2, detect_schema(self.project_dir))
        apply_migration(self.project_dir)
        self.assertEqual(3, detect_schema(self.project_dir))

    def test_a_foreign_backup_is_never_clobbered(self) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(self._v2_state()), encoding="utf-8")
        (self.project_dir / "project.v2.json").write_text('{"someone": "else"}', encoding="utf-8")
        with self.assertRaises(WorkspaceError) as caught:
            apply_migration(self.project_dir)
        self.assertIn("migration backup already exists", str(caught.exception))
        self.assertEqual('{"someone": "else"}', (self.project_dir / "project.v2.json").read_text(encoding="utf-8"))


class MalformedCandidateTests(unittest.TestCase):
    """Finding #5: the transition check runs before validation, so it must not raise."""

    def test_unhashable_task_id_does_not_crash_the_checker(self) -> None:
        previous: dict[str, Any] = {"status": "EXECUTING", "tasks": []}
        candidate: dict[str, Any] = {"status": "EXECUTING", "tasks": [{"id": [], "status": "TODO"}]}
        self.assertEqual([], check_state_transition(previous, candidate))

    def test_unusable_task_ids_are_left_to_validation(self) -> None:
        previous: dict[str, Any] = {"status": "EXECUTING", "tasks": []}
        for task_id in ([], {}, None, 7, "", "   ", True):
            with self.subTest(task_id=task_id):
                candidate: dict[str, Any] = {
                    "status": "EXECUTING",
                    "tasks": [{"id": task_id, "status": "TODO"}],
                }
                self.assertEqual([], check_state_transition(previous, candidate))

    def test_non_list_tasks_does_not_crash_the_checker(self) -> None:
        previous: dict[str, Any] = {"status": "EXECUTING", "tasks": []}
        for tasks in ("not a list", 7, {"id": "T1"}, None):
            with self.subTest(tasks=tasks):
                self.assertEqual([], check_state_transition(previous, {"status": "EXECUTING", "tasks": tasks}))

    def test_valid_transitions_are_still_checked(self) -> None:
        previous: dict[str, Any] = {"status": "EXECUTING", "tasks": [{"id": "T1", "status": "DONE"}]}
        candidate: dict[str, Any] = {"status": "EXECUTING", "tasks": [{"id": "T1", "status": "TODO"}]}
        self.assertTrue(check_state_transition(previous, candidate))


class MalformedCandidateCommitTests(unittest.TestCase):
    """Finding #5, through the public entry point: the user must get a WorkspaceError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = workspace_lib.allocate_project(
            self.workspace, title="Malformed", working_directory=self.target
        )

    def test_commit_rejects_an_unhashable_task_id_without_a_traceback(self) -> None:
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        candidate = dict(state)
        candidate["tasks"] = [{"id": [], "status": "TODO"}]
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaises(WorkspaceError) as caught:
            commit_candidate(self.project_dir, candidate_path, expected_revision=state["revision"])
        self.assertIn("id must be a non-empty string", str(caught.exception))


class MalformedExternalReferenceTests(unittest.TestCase):
    """Follow-up #6: malformed URLs must be reported as validation errors, not raised."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.project_dir = workspace_lib.allocate_project(
            self.workspace, title="Malformed URL", working_directory=self.root
        )
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state["status"] = "PLANNING"
        state["tasks"] = [
            {
                "id": "T1",
                "name": "Publish",
                "status": "TODO",
                "depends_on": [],
                "outputs": [{"root": "external", "path": "http://[", "required": True}],
                "success_criteria": "published",
                "verification": "check",
                "evidence": [],
                "effect": {"kind": "external", "description": "Publish"},
                "authorization": {
                    "required": True,
                    "status": "pending",
                    "scope": None,
                    "source": None,
                    "authorized_at": None,
                },
                "receipts": [],
                "skip_reason": None,
                "block_reason": None,
            }
        ]
        (self.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")

    def test_malformed_url_is_a_validation_error(self) -> None:
        report = validate_project(self.project_dir)
        self.assertTrue(any("invalid external reference" in error for error in report.errors))

    def test_malformed_url_reaches_the_cli_without_a_traceback(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(VALIDATOR), str(self.project_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("invalid external reference", completed.stderr)


class InvalidUtf8Tests(unittest.TestCase):
    """Finding #6: UnicodeDecodeError is a ValueError, so `except OSError` never caught it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.project_dir = self.workspace / "project"
        self.project_dir.mkdir()

    def test_invalid_utf8_project_json_is_reported_not_raised(self) -> None:
        (self.project_dir / "project.json").write_bytes(b'{"schema_version": 3, "t": "\xff"}')
        report = validate_project(self.project_dir)
        self.assertTrue(report.errors)
        self.assertIn("cannot read", report.errors[0])

    def test_invalid_utf8_legacy_markdown_does_not_break_the_index(self) -> None:
        (self.project_dir / "00_meta.yaml").write_bytes(b"legacy_schema: 1\n")
        (self.project_dir / "02_task_plan.md").write_bytes(b"# Plan \xff\xfe\n")
        rendered = render_index(self.workspace)
        self.assertIn("project", rendered)

    def test_invalid_utf8_required_legacy_file_is_reported(self) -> None:
        (self.project_dir / "00_meta.yaml").write_bytes(b"legacy_schema: 1\n")
        (self.project_dir / "02_task_plan.md").write_bytes(b"\xff\xfe\n")
        report = validate_project(self.project_dir)
        self.assertTrue(any("missing readable content" in error for error in report.errors))

    def test_invalid_utf8_reflection_blocks_legacy_closure(self) -> None:
        (self.project_dir / "00_meta.yaml").write_bytes(b"legacy_schema: 1\ntitle: T\n")
        (self.project_dir / "02_task_plan.md").write_bytes(b"# Task Plan - T\n")
        (self.project_dir / "reflection.md").write_bytes(b"\xff\xfe\n")
        report = validate_project(self.project_dir, close=True, allow_legacy_close=True)
        self.assertTrue(any("reflection.md" in error for error in report.errors))

    def test_invalid_utf8_index_is_reported(self) -> None:
        project_dir = workspace_lib.allocate_project(self.workspace, title="Utf8", working_directory=self.root)
        workspace_lib.rebuild_index(self.workspace)
        (self.workspace / "INDEX.md").write_bytes(b"# Agentic workspace projects \xff\n")
        report = validate_project(project_dir, check_index=True)
        self.assertTrue(any("derived index is unreadable" in error for error in report.errors))


class MigrationIndexRecoveryTests(unittest.TestCase):
    """Follow-up #3: a failed index rebuild must not read as a failed migration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.project_dir = self.workspace / "project"
        self.project_dir.mkdir(parents=True)
        (self.project_dir / "project.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project": "project",
                    "title": "T",
                    "status": "PLANNING",
                    "created": "2026-01-01",
                    "updated": TIMESTAMP,
                    "working_directory": str(self.root),
                    "current_task": None,
                    "review_cycle": 0,
                    "tasks": [],
                }
            ),
            encoding="utf-8",
        )

    def _hold_index_lock(self) -> Path:
        lock = self.workspace / ".index.lock"
        lock.mkdir()
        return lock

    def test_index_lock_failure_says_the_migration_committed(self) -> None:
        lock = self._hold_index_lock()
        with self.assertRaises(WorkspaceError) as caught:
            apply_migration(self.project_dir, lock_timeout=0.1)
        message = str(caught.exception)
        self.assertIn("migration is committed", message)
        self.assertIn("rebuild-index", message)
        # The state really did land, which is exactly why the message must not read as a failure.
        self.assertEqual(3, detect_schema(self.project_dir))
        lock.rmdir()

    def test_the_named_recovery_command_finishes_the_job(self) -> None:
        # The message names rebuild-index rather than a re-run because *that* is the idempotent
        # step: re-running the migration itself would refuse, the project already being v3.
        lock = self._hold_index_lock()
        with self.assertRaises(WorkspaceError):
            apply_migration(self.project_dir, lock_timeout=0.1)
        lock.rmdir()
        workspace_lib.rebuild_index(self.workspace)
        self.assertEqual([], validate_project(self.project_dir, check_index=True).errors)
        self.assertIn("project", (self.workspace / "INDEX.md").read_text(encoding="utf-8"))

    def test_migrating_an_already_migrated_project_still_refuses(self) -> None:
        apply_migration(self.project_dir)
        with self.assertRaises(WorkspaceError) as caught:
            apply_migration(self.project_dir)
        self.assertIn("already uses schema v3", str(caught.exception))


class InitializationOrderTests(unittest.TestCase):
    """Follow-up #4: init must obey the same state-last rule as migration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def _allocate(self) -> Path:
        return workspace_lib.allocate_project(self.workspace, title="Ordered", working_directory=self.root)

    def _fail_on(self, name: str) -> Any:
        real = workspace_lib.atomic_write_text

        def guard(path: Path, text: str) -> None:
            if path.name == name:
                raise OSError("disk full")
            real(path, text)

        return patch.object(workspace_lib, "atomic_write_text", side_effect=guard)

    def test_failure_while_writing_the_skeleton_leaves_no_detectable_project(self) -> None:
        with self._fail_on("spec.md"), self.assertRaises(OSError):
            self._allocate()
        partial = next(child for child in self.workspace.iterdir() if child.is_dir())
        self.assertFalse((partial / "project.json").exists())
        # Nothing claims to be a v3 project, so nothing can be validated or closed as one.
        with self.assertRaises(WorkspaceError):
            detect_schema(partial)
        self.assertNotIn(partial.name, render_index(self.workspace))

    def test_retry_after_an_interrupted_initialization_succeeds(self) -> None:
        with self._fail_on("spec.md"), self.assertRaises(OSError):
            self._allocate()
        project_dir = self._allocate()
        self.assertEqual([], validate_project(project_dir, check_index=True).errors)

    def test_a_committed_project_is_never_missing_its_files(self) -> None:
        project_dir = self._allocate()
        for name in ("project.json", "spec.md", "evidence.md"):
            with self.subTest(name=name):
                self.assertTrue((project_dir / name).is_file())

    def test_index_lock_failure_names_the_created_project(self) -> None:
        (self.workspace / ".index.lock").mkdir()
        with self.assertRaises(WorkspaceError) as caught:
            workspace_lib.allocate_project(
                self.workspace, title="Ordered", working_directory=self.root, lock_timeout=0.1
            )
        message = str(caught.exception)
        self.assertIn("is initialized", message)
        self.assertIn("rebuild-index", message)


class PostCommitFilesystemFailureTests(unittest.TestCase):
    """Follow-up #5: a filesystem failure after the commit must report, not traceback.

    The wrapper used to catch only WorkspaceError, so an OSError from the rebuild escaped it
    entirely: the CLIs translate WorkspaceError alone, so the operator saw a traceback for an
    operation that had in fact committed — the exact shape that invites a retry of finished work.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def _fail_on_index(self) -> Any:
        real = workspace_lib.atomic_write_text

        def guard(path: Path, text: str) -> None:
            if path.name == "INDEX.md":
                raise OSError("disk full")
            real(path, text)

        return patch.object(workspace_lib, "atomic_write_text", side_effect=guard)

    def _legacy_project(self) -> Path:
        project_dir = self.workspace / "legacy"
        project_dir.mkdir(parents=True)
        (project_dir / "project.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project": "legacy",
                    "title": "T",
                    "status": "PLANNING",
                    "created": "2026-01-01",
                    "updated": TIMESTAMP,
                    "working_directory": str(self.root),
                    "current_task": None,
                    "review_cycle": 0,
                    "tasks": [],
                }
            ),
            encoding="utf-8",
        )
        return project_dir

    def _assert_committed_not_failed(self, error: WorkspaceError, expected: str) -> None:
        message = str(error)
        self.assertIn(expected, message)
        self.assertIn("disk full", message)
        self.assertIn("rebuild-index", message)

    def test_migration_reports_the_commit_when_the_rebuild_hits_oserror(self) -> None:
        project_dir = self._legacy_project()
        with self._fail_on_index(), self.assertRaises(WorkspaceError) as caught:
            apply_migration(project_dir)
        self._assert_committed_not_failed(caught.exception, "migration is committed")
        self.assertEqual(3, detect_schema(project_dir))

    def test_initialization_reports_the_commit_when_the_rebuild_hits_oserror(self) -> None:
        with self._fail_on_index(), self.assertRaises(WorkspaceError) as caught:
            workspace_lib.allocate_project(self.workspace, title="Ordered", working_directory=self.root)
        self._assert_committed_not_failed(caught.exception, "is initialized")
        project_dir = next(child for child in self.workspace.iterdir() if child.is_dir())
        self.assertEqual(3, detect_schema(project_dir))

    def test_commit_reports_the_revision_when_the_rebuild_hits_oserror(self) -> None:
        project_dir = workspace_lib.allocate_project(self.workspace, title="Ordered", working_directory=self.root)
        candidate = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        candidate["status"] = "PLANNING"
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        with self._fail_on_index(), self.assertRaises(WorkspaceError) as caught:
            commit_candidate(project_dir, candidate_path, expected_revision=candidate["revision"])
        self._assert_committed_not_failed(caught.exception, "revision 1 is committed")
        committed = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(1, committed["revision"])

    def test_the_recovery_command_itself_reports_filesystem_failures(self) -> None:
        # rebuild-index is the command the post-commit error names, so it must not be the one
        # command that answers a filesystem failure with a traceback.
        with self._fail_on_index(), self.assertRaises(WorkspaceError) as caught:
            workspace_lib.rebuild_index(self.workspace)
        message = str(caught.exception)
        self.assertIn("cannot rebuild", message)
        self.assertIn("INDEX.md", message)

    def test_a_real_filesystem_failure_reaches_the_cli_as_an_error(self) -> None:
        # No patching: INDEX.md as a directory makes the atomic rename fail for real, which is
        # what proves the CLI reports rather than traces.
        project_dir = self._legacy_project()
        (self.workspace / "INDEX.md").mkdir()
        completed = subprocess.run(
            [sys.executable, str(MANAGER), "migrate", str(project_dir), "--apply"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("ERROR: migration is committed", completed.stderr)
        self.assertIn("rebuild-index", completed.stderr)
        self.assertEqual(3, detect_schema(project_dir))

    def test_unpaired_surrogate_after_commit_is_reported_without_a_traceback(self) -> None:
        project_dir = workspace_lib.allocate_project(self.workspace, title="Ordered", working_directory=self.root)
        malformed_dir = self.workspace / "malformed"
        malformed_dir.mkdir()
        malformed_state = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        malformed_state["project"] = malformed_dir.name
        malformed_state["title"] = "unpaired surrogate: \udcff"
        # json.dumps escapes the surrogate, so project.json itself remains valid UTF-8 and the
        # failure occurs only when the derived Markdown index is encoded.
        (malformed_dir / "project.json").write_text(json.dumps(malformed_state), encoding="utf-8")

        candidate = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        candidate["status"] = "PLANNING"
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "commit",
                str(project_dir),
                str(candidate_path),
                "--expected-revision",
                "0",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("revision 1 is committed", completed.stderr)
        self.assertIn("rebuild-index", completed.stderr)
        committed = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(1, committed["revision"])


class PluginCacheTests(unittest.TestCase):
    """Follow-up #2: installs are copied into a version-keyed cache, so the version is load-bearing."""

    def _read(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def test_plugin_manifest_pins_a_semver_version(self) -> None:
        # Two things depend on it: `claude plugin validate --strict` fails on a missing version,
        # and the install cache is keyed on it — an unchanged version suppresses `plugin update`.
        manifest = json.loads(self._read("plugins/research/.claude-plugin/plugin.json"))
        version = manifest["version"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_the_docs_do_not_claim_marketplace_update_refreshes_the_cache(self) -> None:
        # It reports success and changes nothing about the installed copy; saying otherwise sends
        # the reader back to a stale plugin believing they had updated it.
        for relative in ("README.md", "bin/install-plugin.sh"):
            with self.subTest(document=relative):
                text = self._read(relative)
                self.assertNotIn("picks up local edits", text)
                self.assertIn("--plugin-dir", text)
                self.assertIn("plugin update", text)


class PluginManifestTests(unittest.TestCase):
    """Findings #2 and #3: host-specific manifests must expose the same plugin correctly."""

    def test_plugin_manifest_declares_no_skills_key(self) -> None:
        # Skills are discovered from skills/<name>/SKILL.md. A `skills` key is not part of the
        # schema and makes `claude plugin validate` fail with "skills: Invalid input".
        manifest = json.loads((REPO_ROOT / "plugins/research/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("skills", manifest)
        self.assertEqual("research", manifest["name"])
        self.assertIn("version", manifest)

    def test_kimi_manifest_exposes_the_shared_skills_directory(self) -> None:
        manifest = json.loads((REPO_ROOT / "plugins/research/.kimi-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("research", manifest["name"])
        self.assertEqual("./skills", manifest["skills"])
        self.assertIn("version", manifest)

    def test_every_skill_directory_carries_a_skill_md(self) -> None:
        skills = sorted((REPO_ROOT / "plugins/research/skills").iterdir())
        self.assertTrue(skills)
        for skill in skills:
            with self.subTest(skill=skill.name):
                self.assertTrue((skill / "SKILL.md").is_file())

    def test_marketplace_publishes_every_plugin_directory(self) -> None:
        # Registration goes through this marketplace; a plugin missing from it is uninstallable.
        marketplace = json.loads((REPO_ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        published = {entry["name"]: entry["source"] for entry in marketplace["plugins"]}
        on_disk = {
            child.name
            for child in (REPO_ROOT / "plugins").iterdir()
            if (child / ".claude-plugin/plugin.json").is_file()
        }
        self.assertEqual(on_disk, set(published))
        for name, source in published.items():
            with self.subTest(plugin=name):
                self.assertEqual(f"./plugins/{name}", source)
                self.assertTrue((REPO_ROOT / source[2:] / ".claude-plugin/plugin.json").is_file())


def _write_claude_stub(directory: Path) -> None:
    """A `claude` that logs every invocation and answers the two listings the installer reads.

    `plugin marketplace list --json` answers from CLAUDE_STUB_MARKETPLACES when it is set and
    otherwise prints nothing, which the installer treats as an unreadable listing and proceeds —
    the fail-open path, and the default so tests that do not care about the source guard are
    unaffected by it.
    """
    stub = directory / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$CLAUDE_STUB_LOG"\n'
        'if [[ "$1 $2 $3" == "plugin list --json" ]]; then\n'
        '  cat "$CLAUDE_STUB_LISTING"\n'
        '  exit "${CLAUDE_STUB_LIST_STATUS:-0}"\n'
        "fi\n"
        'if [[ "$1 $2 $3" == "plugin marketplace list" ]]; then\n'
        '  [[ -n ${CLAUDE_STUB_MARKETPLACES:-} ]] && printf "%s" "$CLAUDE_STUB_MARKETPLACES"\n'
        '  exit "${CLAUDE_STUB_MARKETPLACE_STATUS:-0}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


class InstallerRefreshTests(unittest.TestCase):
    """The installer used to run `plugin install` unconditionally, which no-ops on an installed plugin.

    A stubbed `claude` on PATH records the exact command sequence, because the sequence is the
    behaviour: which of install, marketplace update, and plugin update runs decides whether the
    installed copy actually changes, and the previous script reported success either way.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub_dir = Path(self.tmp.name)
        self.log = self.stub_dir / "commands.log"
        self.listing = self.stub_dir / "listing.json"
        _write_claude_stub(self.stub_dir)
        self.manifest_version = json.loads(
            (REPO_ROOT / "plugins/research/.claude-plugin/plugin.json").read_text(encoding="utf-8")
        )["version"]

    def _run(self, listing: str, *, list_status: int = 0) -> "tuple[subprocess.CompletedProcess[str], list[str]]":
        self.listing.write_text(listing, encoding="utf-8")
        environment = dict(os.environ)
        environment["PATH"] = f"{self.stub_dir}{os.pathsep}{environment['PATH']}"
        environment["CLAUDE_STUB_LOG"] = str(self.log)
        environment["CLAUDE_STUB_LISTING"] = str(self.listing)
        environment["CLAUDE_STUB_LIST_STATUS"] = str(list_status)
        completed = subprocess.run(
            ["bash", str(REPO_ROOT / "bin/install-plugin.sh"), "research"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        recorded = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return completed, recorded

    def _entry(self, version: str) -> str:
        return json.dumps([{"id": "research@agents", "version": version, "enabled": True}])

    def test_a_plugin_that_is_not_installed_is_installed(self) -> None:
        completed, recorded = self._run("[]")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [
                f"plugin validate --strict {REPO_ROOT}",
                f"plugin validate --strict {REPO_ROOT}/plugins/research",
                "plugin marketplace list --json",
                f"plugin marketplace add {REPO_ROOT}",
                "plugin list --json",
                "plugin install research@agents",
            ],
            recorded,
        )
        self.assertIn("is not installed", completed.stdout)

    def test_an_installed_older_version_is_refreshed_not_reinstalled(self) -> None:
        completed, recorded = self._run(self._entry("0.0.1"))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [
                "plugin marketplace update agents",
                "plugin update research@agents",
            ],
            recorded[-2:],
        )
        self.assertNotIn("plugin install research@agents", recorded)
        self.assertIn(f"refreshing to {self.manifest_version}", completed.stdout)

    def test_the_same_version_is_refused_rather_than_reported_as_a_refresh(self) -> None:
        # The install cache is keyed on the version, so an update here copies nothing. Exiting 0
        # with a success message is the exact defect this replaces.
        completed, recorded = self._run(self._entry(self.manifest_version))
        self.assertEqual(1, completed.returncode)
        self.assertIn("already installed", completed.stderr)
        self.assertIn('bump "version"', completed.stderr)
        self.assertIn("--plugin-dir", completed.stderr)
        self.assertNotIn("plugin install research@agents", recorded)
        self.assertNotIn("plugin update research@agents", recorded)

    def test_an_unreadable_listing_is_treated_as_not_installed(self) -> None:
        for listing, status in (("not json at all", 0), ("[]", 1), ('{"unexpected": true}', 0)):
            with self.subTest(listing=listing, status=status):
                self.log.unlink(missing_ok=True)
                completed, recorded = self._run(listing, list_status=status)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("plugin install research@agents", recorded)

    def test_an_unknown_plugin_never_reaches_the_cli(self) -> None:
        self.listing.write_text("[]", encoding="utf-8")
        environment = dict(os.environ)
        environment["PATH"] = f"{self.stub_dir}{os.pathsep}{environment['PATH']}"
        environment["CLAUDE_STUB_LOG"] = str(self.log)
        environment["CLAUDE_STUB_LISTING"] = str(self.listing)
        completed = subprocess.run(
            ["bash", str(REPO_ROOT / "bin/install-plugin.sh"), "nosuchplugin"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        self.assertEqual(1, completed.returncode)
        self.assertIn("not published by this marketplace", completed.stderr)
        self.assertFalse(self.log.exists(), "the CLI ran for a plugin the marketplace does not publish")


class InstallerArgumentTests(unittest.TestCase):
    """Finding #7: the plugin name is a path component, so only a bare name is acceptable."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(REPO_ROOT / "bin/install-plugin.sh"), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_path_components_are_rejected(self) -> None:
        for name in ("../tests", "../../../../etc", "research/../../evil", "/absolute", "a/b"):
            with self.subTest(name=name):
                result = self._run(name)
                self.assertEqual(1, result.returncode)
                self.assertIn("not a valid plugin name", result.stderr)

    def test_missing_argument_prints_usage(self) -> None:
        result = self._run()
        self.assertEqual(2, result.returncode)
        self.assertIn("usage:", result.stderr)

    def test_unknown_options_are_rejected(self) -> None:
        # The option loop must not fall through to treating a flag as the plugin name.
        result = self._run("--nope", "research")
        self.assertEqual(2, result.returncode)
        self.assertIn("unknown option", result.stderr)

    def test_a_second_operand_is_rejected(self) -> None:
        result = self._run("research", "extra")
        self.assertEqual(2, result.returncode)
        self.assertIn("unexpected argument", result.stderr)

    def test_help_is_available_and_advertises_force(self) -> None:
        # --force changes the owner's marketplace declaration, so it has to be discoverable.
        result = self._run("--help")
        self.assertEqual(0, result.returncode)
        self.assertIn("--force", result.stdout)


class MarketplaceGuardTests(unittest.TestCase):
    """The CLI does not guard a directory source landing on a GitHub declaration: it replaces it.

    Adding a *network* source over a differing declaration fails loudly; adding a *directory* source
    over a GitHub one succeeds and silently repoints everyone installing from GitHub at this clone.
    The installer therefore checks for itself, and these tests drive that check through the same
    stubbed `claude` the refresh tests use, so the behaviour is verified where CI can run it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub_dir = Path(self.tmp.name)
        self.log = self.stub_dir / "commands.log"
        self.listing = self.stub_dir / "listing.json"
        self.listing.write_text("[]", encoding="utf-8")
        _write_claude_stub(self.stub_dir)

    def _run(
        self, marketplaces: str, *args: str, status: int = 0
    ) -> "tuple[subprocess.CompletedProcess[str], list[str]]":
        environment = dict(os.environ)
        environment["PATH"] = f"{self.stub_dir}{os.pathsep}{environment['PATH']}"
        environment["CLAUDE_STUB_LOG"] = str(self.log)
        environment["CLAUDE_STUB_LISTING"] = str(self.listing)
        environment["CLAUDE_STUB_MARKETPLACES"] = marketplaces
        environment["CLAUDE_STUB_MARKETPLACE_STATUS"] = str(status)
        completed = subprocess.run(
            ["bash", str(REPO_ROOT / "bin/install-plugin.sh"), *args, "research"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        recorded = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return completed, recorded

    @staticmethod
    def _declared(**fields: str) -> str:
        return json.dumps([{"name": "agents", **fields}])

    def test_a_github_declaration_is_refused_rather_than_replaced(self) -> None:
        completed, recorded = self._run(self._declared(source="github", repo="nampham2/agents"))
        self.assertEqual(1, completed.returncode)
        self.assertIn("already declared as GitHub (nampham2/agents)", completed.stderr)
        self.assertIn("--force", completed.stderr)
        # The point of refusing is that nothing is written, so the write must not have been attempted.
        self.assertNotIn(f"plugin marketplace add {REPO_ROOT}", recorded)

    def test_force_replaces_the_declaration_and_says_so(self) -> None:
        completed, recorded = self._run(
            self._declared(source="github", repo="nampham2/agents"), "--force"
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("replacing the 'agents' marketplace declaration", completed.stderr)
        self.assertIn(f"plugin marketplace add {REPO_ROOT}", recorded)

    def test_a_declaration_of_this_clone_is_not_a_conflict(self) -> None:
        # The normal case: re-running the installer on the machine it already configured.
        completed, _ = self._run(self._declared(source="directory", path=str(REPO_ROOT)))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("already declared as", completed.stderr)

    def test_a_declaration_of_another_directory_is_refused(self) -> None:
        completed, _ = self._run(self._declared(source="directory", path="/somewhere/else"))
        self.assertEqual(1, completed.returncode)
        self.assertIn("a different directory (/somewhere/else)", completed.stderr)

    def test_an_undeclared_marketplace_is_not_a_conflict(self) -> None:
        completed, _ = self._run(json.dumps([{"name": "unrelated", "source": "github", "repo": "x/y"}]))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("already declared as", completed.stderr)

    def test_an_unrecognisable_listing_fails_open(self) -> None:
        # A future format change must cost this guard, not every install. Each of these proceeds.
        cases = (("not json at all", 0), ('{"unexpected": true}', 0), ("[]", 3), ("", 0))
        for marketplaces, status in cases:
            with self.subTest(marketplaces=marketplaces, status=status):
                self.log.unlink(missing_ok=True)
                completed, recorded = self._run(marketplaces, status=status)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertNotIn("already declared as", completed.stderr)
                self.assertIn(f"plugin marketplace add {REPO_ROOT}", recorded)

    def test_an_unknown_source_kind_is_reported_and_refused(self) -> None:
        # A kind this script has never heard of is still plainly not this clone, so it is a conflict.
        # Fail-open covers listings it cannot read, not declarations it can read and does not like.
        completed, _ = self._run(self._declared(source="somethingnew", url="https://example.invalid"))
        self.assertEqual(1, completed.returncode)
        self.assertIn("somethingnew (https://example.invalid)", completed.stderr)

    def test_an_entry_with_no_source_fails_open(self) -> None:
        completed, _ = self._run(json.dumps([{"name": "agents"}]))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("already declared as", completed.stderr)


class MarketplaceCollisionTests(unittest.TestCase):
    """A marketplace name holds exactly one source, so the two install routes are exclusive."""

    def _read(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def test_the_docs_document_the_marketplace_name_collision(self) -> None:
        # Following the local-install section and then the GitHub one fails outright. The remedy is
        # the part a reader actually needs, and the remove step cascades into an uninstall, so both
        # halves of the sequence are pinned here.
        text = self._read("README.md")
        self.assertIn("marketplace remove agents", text)
        self.assertIn("marketplace add nampham2/agents", text)
        self.assertIn("plugin install research@agents", text)

    def test_the_installer_refuses_a_silent_source_switch(self) -> None:
        # The CLI does not guard this direction: a directory source replaces a GitHub declaration
        # and reports success. The script has to check for itself, and only --force may override.
        script = self._read("bin/install-plugin.sh")
        self.assertIn("marketplace list --json", script)
        self.assertIn("already declared as", script)
        self.assertIn("--force", script)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
