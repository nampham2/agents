from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents.workspace.lib import (
    DirectoryLock,
    WorkspaceConflict,
    commit_candidate,
    is_external_reference,
    migrate_v2_state,
    rebuild_index,
    validate_legacy_v1,
    validate_project,
    validate_v3_state,
)

TIMESTAMP = "2026-08-28T10:00:00+02:00"


class WorkspaceFixture:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root / "workspace"
        self.project_dir = self.workspace_root / "2026-08-28-001"
        self.target_dir = root / "target"
        self.project_dir.mkdir(parents=True)
        self.target_dir.mkdir()
        (self.project_dir / "reviews").mkdir()
        (self.project_dir / "tasks").mkdir()
        (self.project_dir / "artifacts").mkdir()
        (self.project_dir / "spec.md").write_text(
            "# Test\n\n## Current specification\n\nProduce the output.\n\n"
            "## Decision history\n\n- 2026-08-28 — Initial scope.\n",
            encoding="utf-8",
        )
        (self.project_dir / "evidence.md").write_text("# Evidence\n\n## T01\n\nVerified.\n", encoding="utf-8")
        (self.project_dir / "reflection.md").write_text("# Reflection\n\nThe task completed.\n", encoding="utf-8")
        (self.target_dir / "out.txt").write_text("result\n", encoding="utf-8")

    def task(self, task_id: str = "T01", status: str = "DONE") -> dict[str, object]:
        return {
            "id": task_id,
            "name": "Produce output",
            "status": status,
            "depends_on": [],
            "outputs": [{"root": "target", "path": "out.txt", "required": True}],
            "success_criteria": "The output exists.",
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

    def state(self, status: str = "DONE") -> dict[str, object]:
        return {
            "schema_version": 3,
            "project": self.project_dir.name,
            "title": "Test project",
            "status": status,
            "created": TIMESTAMP,
            "updated": TIMESTAMP,
            "working_directory": str(self.target_dir),
            "revision": 0,
            "current_tasks": [],
            "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
            "cancellation_reason": None,
            "tasks": [self.task()],
        }


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WorkspaceFixture(Path(self.temporary.name))

    def test_valid_v3_project_closes(self) -> None:
        report = validate_v3_state(self.fixture.state(), self.fixture.project_dir, close=True)
        self.assertEqual([], report.errors)

    def test_missing_documented_task_fields_are_rejected(self) -> None:
        state = self.fixture.state()
        del state["tasks"][0]["effect"]
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("missing fields: effect" in error for error in report.errors))

    def test_malformed_current_tasks_is_reported_without_crashing(self) -> None:
        state = self.fixture.state(status="EXECUTING")
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01", 2]
        report = validate_v3_state(state, self.fixture.project_dir)
        self.assertTrue(any("current_tasks must be" in error for error in report.errors))

    def test_unhashable_enum_values_are_reported_without_crashing(self) -> None:
        mutations = [
            lambda state: state.update(status=[]),
            lambda state: state["review"].update(status=[]),
            lambda state: state["tasks"][0].update(status=[]),
            lambda state: state["tasks"][0]["outputs"][0].update(root=[]),
            lambda state: state["tasks"][0]["effect"].update(kind=[]),
            lambda state: state["tasks"][0]["authorization"].update(status=[]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                state = self.fixture.state()
                mutate(state)
                report = validate_v3_state(state, self.fixture.project_dir, close=True)
                self.assertTrue(report.errors)

    def test_external_effect_requires_authorization_and_receipt(self) -> None:
        state = self.fixture.state()
        task = state["tasks"][0]
        task["outputs"] = [{"root": "external", "path": "https://example.com/result", "required": True}]
        task["effect"] = {"kind": "external", "description": "Publish the result"}
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("must require authorization" in error for error in report.errors))
        self.assertTrue(any("durable receipt" in error for error in report.errors))

    def test_external_receipt_value_must_be_durable(self) -> None:
        state = self.fixture.state()
        task = state["tasks"][0]
        task["outputs"] = [{"root": "external", "path": "https://example.com/result", "required": True}]
        task["effect"] = {"kind": "external", "description": "Publish the result"}
        task["authorization"] = {
            "required": True,
            "status": "explicit",
            "scope": "Publish result",
            "source": "User message",
            "authorized_at": TIMESTAMP,
        }
        task["receipts"] = [
            {"kind": "publish", "value": "ok", "destination": "example", "timestamp": TIMESTAMP}
        ]
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("durable prefixed identifier" in error for error in report.errors))

    def test_destructive_effect_requires_scoped_authorization(self) -> None:
        state = self.fixture.state()
        task = state["tasks"][0]
        task["effect"] = {"kind": "destructive", "description": "Delete an obsolete file"}
        task["authorization"] = {
            "required": True,
            "status": "explicit",
            "scope": None,
            "source": "user",
            "authorized_at": TIMESTAMP,
        }
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("requires scope" in error for error in report.errors))

    def test_target_output_cannot_be_satisfied_by_workspace_copy(self) -> None:
        (self.fixture.target_dir / "out.txt").unlink()
        (self.fixture.project_dir / "out.txt").write_text("wrong root\n", encoding="utf-8")
        report = validate_v3_state(self.fixture.state(), self.fixture.project_dir, close=True)
        self.assertTrue(any("target:out.txt" in error for error in report.errors))

    def test_running_task_requires_done_dependencies(self) -> None:
        state = self.fixture.state(status="EXECUTING")
        first = self.fixture.task("T01", "TODO")
        second = self.fixture.task("T02", "RUNNING")
        second["depends_on"] = ["T01"]
        state["tasks"] = [first, second]
        state["current_tasks"] = ["T02"]
        report = validate_v3_state(state, self.fixture.project_dir)
        self.assertTrue(any("unsatisfied dependency T01 (TODO)" in error for error in report.errors))

    def test_required_review_must_be_accepted_and_recorded(self) -> None:
        state = self.fixture.state()
        state["review"] = {"cycle": 1, "required": True, "status": "pending", "evidence": []}
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("required review is accepted" in error for error in report.errors))

    def test_even_optional_review_cannot_remain_pending_at_close(self) -> None:
        state = self.fixture.state()
        state["review"] = {"cycle": 1, "required": False, "status": "pending", "evidence": []}
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("pending review" in error for error in report.errors))

    def test_invalid_external_references_are_rejected(self) -> None:
        self.assertFalse(is_external_reference("https://"))
        self.assertFalse(is_external_reference("receipt:"))
        self.assertTrue(is_external_reference("https://example.com/result"))
        self.assertTrue(is_external_reference("receipt:abc123"))

    def test_empty_completion_files_are_rejected(self) -> None:
        (self.fixture.project_dir / "reflection.md").write_text("", encoding="utf-8")
        report = validate_v3_state(self.fixture.state(), self.fixture.project_dir, close=True)
        self.assertTrue(any("reflection.md must not be empty" in error for error in report.errors))

    def test_symlink_cannot_escape_declared_output_root(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.fixture.target_dir / "escape.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        state = self.fixture.state()
        state["tasks"][0]["outputs"] = [{"root": "target", "path": "escape.txt", "required": True}]
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("escapes its target root" in error for error in report.errors))


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WorkspaceFixture(Path(self.temporary.name))

    def test_stale_revision_is_rejected(self) -> None:
        state = self.fixture.state(status="PLANNING")
        state["tasks"] = []
        (self.fixture.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        first_candidate = copy.deepcopy(state)
        first_candidate["title"] = "First update"
        first_path = Path(self.temporary.name) / "first.json"
        first_path.write_text(json.dumps(first_candidate), encoding="utf-8")
        committed = commit_candidate(self.fixture.project_dir, first_path, expected_revision=0)
        self.assertEqual(1, committed["revision"])

        stale_candidate = copy.deepcopy(state)
        stale_candidate["title"] = "Stale update"
        stale_path = Path(self.temporary.name) / "stale.json"
        stale_path.write_text(json.dumps(stale_candidate), encoding="utf-8")
        with self.assertRaises(WorkspaceConflict):
            commit_candidate(self.fixture.project_dir, stale_path, expected_revision=0)

    def test_candidate_revision_must_match_expected_revision(self) -> None:
        state = self.fixture.state(status="PLANNING")
        state["tasks"] = []
        state["revision"] = 2
        (self.fixture.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        candidate = copy.deepcopy(state)
        candidate["revision"] = 1
        candidate_path = Path(self.temporary.name) / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaises(WorkspaceConflict):
            commit_candidate(self.fixture.project_dir, candidate_path, expected_revision=2)

    def test_concurrent_updates_do_not_lose_state(self) -> None:
        state = self.fixture.state(status="PLANNING")
        state["tasks"] = []
        (self.fixture.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        candidates = []
        for name in ("First", "Second"):
            candidate = copy.deepcopy(state)
            candidate["title"] = name
            path = Path(self.temporary.name) / f"{name}.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            candidates.append(path)

        def commit(path: Path) -> str:
            try:
                commit_candidate(self.fixture.project_dir, path, expected_revision=0)
                return "committed"
            except WorkspaceConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(commit, candidates))
        self.assertEqual(["committed", "conflict"], sorted(outcomes))
        final_state = json.loads((self.fixture.project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(1, final_state["revision"])

    def test_busy_lock_returns_recoverable_conflict(self) -> None:
        lock_path = self.fixture.project_dir / ".project.lock"
        with DirectoryLock(lock_path):
            with self.assertRaises(WorkspaceConflict):
                with DirectoryLock(lock_path, timeout=0):
                    pass

    def test_index_is_deterministic_and_generated(self) -> None:
        state = self.fixture.state()
        (self.fixture.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        first = rebuild_index(self.fixture.workspace_root).read_text(encoding="utf-8")
        second = rebuild_index(self.fixture.workspace_root).read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertIn(self.fixture.project_dir.name, first)

    def test_stale_index_is_detected(self) -> None:
        state = self.fixture.state()
        (self.fixture.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        (self.fixture.workspace_root / "INDEX.md").write_text("stale\n", encoding="utf-8")
        report = validate_project(self.fixture.project_dir, close=True, check_index=True)
        self.assertTrue(any("derived index is stale" in error for error in report.errors))


class CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WorkspaceFixture(Path(self.temporary.name))

    def test_v2_migration_produces_valid_v3_state(self) -> None:
        v2 = {
            "schema_version": 2,
            "project": self.fixture.project_dir.name,
            "title": "Legacy project",
            "status": "DONE",
            "created": "2026-08-28",
            "updated": TIMESTAMP,
            "working_directory": str(self.fixture.target_dir),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [
                {
                    "id": "T01",
                    "name": "Produce output",
                    "status": "DONE",
                    "depends_on": [],
                    "outputs": ["out.txt"],
                    "success_criteria": "Output exists.",
                    "verification": "Inspect output.",
                    "evidence": ["evidence.md#T01"],
                    "external_effect": False,
                    "authorization": "not_required",
                    "skip_reason": None,
                }
            ],
        }
        migrated = migrate_v2_state(v2, self.fixture.project_dir)
        report = validate_v3_state(migrated, self.fixture.project_dir, close=True)
        self.assertEqual([], report.errors)

    def test_v1_close_requires_explicit_limited_validation_acknowledgement(self) -> None:
        (self.fixture.project_dir / "00_meta.yaml").write_text("legacy_schema: 1\n", encoding="utf-8")
        (self.fixture.project_dir / "02_task_plan.md").write_text("# Task Plan\n", encoding="utf-8")
        denied = validate_legacy_v1(self.fixture.project_dir, close=True, allow_legacy_close=False)
        accepted = validate_legacy_v1(self.fixture.project_dir, close=True, allow_legacy_close=True)
        self.assertTrue(any("--allow-legacy-close" in error for error in denied.errors))
        self.assertEqual([], accepted.errors)


if __name__ == "__main__":
    unittest.main()
