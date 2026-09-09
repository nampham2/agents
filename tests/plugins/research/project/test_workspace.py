from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import workspace_lib
from workspace_lib import (
    EXTERNAL_IDENTIFIER_PREFIXES,
    PLUGIN_SELF_PATH,
    DirectoryLock,
    WorkspaceConflict,
    commit_candidate,
    is_external_reference,
    migrate_v2_state,
    rebuild_index,
    self_location_warnings,
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

    def task(self, task_id: str = "T01", status: str = "DONE") -> dict[str, Any]:
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

    def state(self, status: str = "DONE") -> dict[str, Any]:
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
        task["receipts"] = [{"kind": "publish", "value": "ok", "destination": "example", "timestamp": TIMESTAMP}]
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertTrue(any("publish:" in error for error in report.errors))

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

    def test_a_commit_identifier_is_an_external_reference(self) -> None:
        # Integration is a delivery, and its durable identifier is the SHA. Without this prefix the
        # branch-and-merge tasks a plan is now required to carry had no receipt they could record.
        self.assertTrue(is_external_reference("commit:9013a6e"))
        self.assertFalse(is_external_reference("commit:"))

    def test_every_prefix_in_the_set_is_accepted(self) -> None:
        for prefix in EXTERNAL_IDENTIFIER_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertTrue(is_external_reference(f"{prefix}:abc123"))

    def test_the_prefix_set_stays_closed(self) -> None:
        # The negative control: adding a prefix must not turn the check into "anything before a
        # colon". A plausible-looking invention is still refused.
        for invented in ("commits:abc123", "merge:abc123", "sha:abc123", "receipts:abc123"):
            with self.subTest(invented=invented):
                self.assertFalse(is_external_reference(invented))

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


class SelfLocationWarningTests(unittest.TestCase):
    """A verification that ran against the installed copy is evidence about the wrong file.

    This project's own working tree is the case: `research-project` on PATH resolves into a
    version-keyed plugin cache, so an edit at an unchanged version is never served. The lesson was
    already written down and still cost a session, so the tools now say it themselves.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WorkspaceFixture(Path(self.temporary.name))

    def _make_plugin_tree(self, base: Path) -> Path:
        scripts = base / "plugins" / "research" / "skills" / "project" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        module = scripts / "workspace_lib.py"
        module.write_text("# a copy of the tools\n", encoding="utf-8")
        return module

    def test_a_working_directory_holding_the_tools_warns(self) -> None:
        under_test = self._make_plugin_tree(self.fixture.target_dir)

        report = validate_v3_state(self.fixture.state(), self.fixture.project_dir, close=True)

        matching = [w for w in report.warnings if "not the copy under" in w]
        self.assertEqual(1, len(matching), report.warnings)
        self.assertIn(str(under_test), matching[0])
        self.assertIn(str(Path(workspace_lib.__file__).resolve()), matching[0])
        self.assertEqual([], report.errors, "the guard is advisory and must never fail a project")

    def test_an_ordinary_working_directory_is_silent(self) -> None:
        # The negative control that matters most: every project that is not this plugin.
        report = validate_v3_state(self.fixture.state(), self.fixture.project_dir, close=True)
        self.assertEqual([], [w for w in report.warnings if "not the copy under" in w])

    def test_running_from_inside_the_working_directory_is_silent(self) -> None:
        # The exception the warning exists to point at: the working-tree launcher really is running
        # the copy under test, and warning there would train the reader to ignore it.
        running = Path(workspace_lib.__file__).resolve()
        # The real repository, which already contains the module at the recognised path — writing a
        # fixture copy here would overwrite the file under test.
        working_directory = running.parents[5]
        self.assertTrue((working_directory / PLUGIN_SELF_PATH).is_file(), working_directory)
        state = self.fixture.state()
        state["working_directory"] = str(working_directory)

        report = validate_v3_state(state, self.fixture.project_dir, close=True)

        self.assertEqual([], [w for w in report.warnings if "not the copy under" in w])

    def test_an_unreadable_working_directory_is_not_an_error(self) -> None:
        with patch.object(Path, "resolve", side_effect=OSError("gone")):
            self.assertEqual([], self_location_warnings(self.fixture.target_dir))


class WorkspaceRootReferenceTests(unittest.TestCase):
    """The fourth root exists for records that belong to the workspace rather than to one project.

    `workspace/reflection.md` and `INDEX.md` are outputs of real tasks, but before this root a task
    could only declare them by claiming `workspace` — wrong, it means the project directory — or by
    reaching outside a root with `..`, which resolution refuses. So they went undeclared, and an
    undeclared output is one validation cannot check.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = WorkspaceFixture(Path(self.temporary.name))
        (self.fixture.workspace_root / "reflection.md").write_text(
            "# Reflection\n\n- Shared lesson.\n", encoding="utf-8"
        )

    def _state_with_output(self, path: str) -> dict[str, Any]:
        state = self.fixture.state()
        state["tasks"][0]["outputs"] = [{"root": "workspace_root", "path": path, "required": True}]
        return state

    def test_a_path_resolves_against_the_workspace_root(self) -> None:
        report = validate_v3_state(self._state_with_output("reflection.md"), self.fixture.project_dir, close=True)
        self.assertEqual([], report.errors)

    def test_the_project_directory_is_reachable_through_it(self) -> None:
        # It is the enclosing directory, so a project-relative path still resolves — just spelled
        # with the project name, which is what makes cross-project references expressible.
        report = validate_v3_state(
            self._state_with_output(f"{self.fixture.project_dir.name}/spec.md"),
            self.fixture.project_dir,
            close=True,
        )
        self.assertEqual([], report.errors)

    def test_a_missing_required_output_is_still_an_error(self) -> None:
        report = validate_v3_state(self._state_with_output("nothing-here.md"), self.fixture.project_dir, close=True)
        expected = "required output does not exist: workspace_root:nothing-here.md"
        self.assertTrue(any(expected in error for error in report.errors), report.errors)

    def test_it_cannot_be_used_to_escape_the_workspace_root(self) -> None:
        report = validate_v3_state(self._state_with_output("../outside.md"), self.fixture.project_dir, close=True)
        self.assertTrue(any("cannot contain '..'" in e for e in report.errors))

    def test_an_absolute_path_is_refused(self) -> None:
        report = validate_v3_state(
            self._state_with_output(str(self.fixture.workspace_root / "reflection.md")),
            self.fixture.project_dir,
            close=True,
        )
        self.assertTrue(any("must be relative" in e for e in report.errors))

    def test_a_symlink_cannot_escape_the_workspace_root(self) -> None:
        outside = Path(self.temporary.name) / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.fixture.workspace_root / "escape.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")

        report = validate_v3_state(self._state_with_output("escape.md"), self.fixture.project_dir, close=True)

        self.assertTrue(any("escapes its workspace_root root" in e for e in report.errors))

    def test_evidence_may_be_rooted_there_too(self) -> None:
        state = self.fixture.state()
        state["tasks"][0]["evidence"] = [
            {"root": "workspace_root", "path": "reflection.md", "anchor": None}
        ]
        report = validate_v3_state(state, self.fixture.project_dir, close=True)
        self.assertEqual([], report.errors)

    def test_the_other_roots_keep_their_meaning(self) -> None:
        # The negative control for the whole change: `workspace` must still mean the project
        # directory, not the root that now has a name of its own.
        state = self.fixture.state()
        state["tasks"][0]["outputs"] = [{"root": "workspace", "path": "reflection.md", "required": True}]
        self.assertEqual([], validate_v3_state(state, self.fixture.project_dir, close=True).errors)
        state["tasks"][0]["outputs"] = [{"root": "workspace", "path": "spec.md", "required": True}]
        self.assertEqual([], validate_v3_state(state, self.fixture.project_dir, close=True).errors)
        # `out.txt` exists in the target and nowhere else, so a root confusion would show up here.
        state["tasks"][0]["outputs"] = [{"root": "workspace_root", "path": "out.txt", "required": True}]
        self.assertTrue(validate_v3_state(state, self.fixture.project_dir, close=True).errors)


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
