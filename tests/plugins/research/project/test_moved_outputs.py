"""A renamed deliverable must not make a finished project uncommittable.

`2026-08-29-001` recorded five outputs under `plugins/research/skills/workbench/`. The skill was
later renamed to `project`, so those paths stopped existing. Because `commit_candidate` treated a
missing required output as an error for every `DONE` task, that project could not be committed at
all — and the dated correction `SKILL.md` prescribes for false history is itself a commit. The guard
still has to fire for a task claiming completion right now; it must not fire for history that moved.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from workspace_lib import (
    WorkspaceError,
    allocate_project,
    commit_candidate,
    validate_v3_state,
)


class MovedOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace_root = root / "workspace"
        self.workspace_root.mkdir()
        self.target = root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace_root, title="Moved", working_directory=self.target)
        self.deliverable = self.target / "deliverable.txt"
        self.deliverable.write_text("shipped\n", encoding="utf-8")

    def _state(self) -> dict[str, Any]:
        return json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))

    def _task(self, status: str) -> dict[str, Any]:
        return {
            "id": "T01",
            "name": "Ship the deliverable",
            "status": status,
            "depends_on": [],
            "outputs": [{"root": "target", "path": "deliverable.txt", "required": True}],
            "success_criteria": "The deliverable exists",
            "verification": "A read confirms it",
            "evidence": [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}],
            "effect": {"kind": "local_write", "description": "write it"},
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

    def _commit(self, state: dict[str, Any]) -> dict[str, Any]:
        candidate = Path(self.temporary.name) / "candidate.json"
        candidate.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return commit_candidate(self.project_dir, candidate, expected_revision=state["revision"])

    def _start_executing(self) -> dict[str, Any]:
        """A new project is ALIGNING; EXECUTING is only reachable through PLANNING."""
        state = self._state()
        state["status"] = "PLANNING"
        state["tasks"] = [self._task("TODO")]
        state = self._commit(state)
        state["status"] = "EXECUTING"
        state["tasks"] = [self._task("RUNNING")]
        state["current_tasks"] = ["T01"]
        return self._commit(state)

    def _finish_the_task(self) -> dict[str, Any]:
        """Commit T01 to DONE while its output still exists, as a real project would."""
        state = self._start_executing()
        state["tasks"] = [self._task("DONE")]
        state["current_tasks"] = []
        return self._commit(state)

    def test_a_task_going_done_now_still_needs_its_output(self) -> None:
        state = self._start_executing()
        self.deliverable.unlink()
        state["tasks"] = [self._task("DONE")]
        state["current_tasks"] = []
        with self.assertRaises(WorkspaceError) as caught:
            self._commit(state)
        self.assertIn("required output does not exist", str(caught.exception))

    def test_history_whose_output_moved_no_longer_blocks_a_commit(self) -> None:
        state = self._finish_the_task()
        # Closure needs a reflection; the point of the test is the reopen that comes after it.
        (self.project_dir / "reflection.md").write_text("Shipped it.\n", encoding="utf-8")
        state["status"] = "DONE"
        state = self._commit(state)
        self.deliverable.rename(self.target / "renamed.txt")
        # The correction the skill prescribes: reopen, and append a task explaining the rename.
        state["status"] = "PLANNING"
        committed = self._commit(state)
        self.assertEqual(committed["status"], "PLANNING")
        self.assertEqual(committed["revision"], state["revision"] + 1)

    def test_the_relaxed_case_warns_rather_than_going_quiet(self) -> None:
        self._finish_the_task()
        self.deliverable.rename(self.target / "renamed.txt")
        state = self._state()
        report = validate_v3_state(state, self.project_dir, already_done={"T01"})
        self.assertEqual(report.errors, [])
        self.assertTrue(
            any("already terminal" in warning and "deliverable.txt" in warning for warning in report.warnings),
            report.warnings,
        )

    def test_standalone_validation_still_reports_an_error(self) -> None:
        self._finish_the_task()
        self.deliverable.rename(self.target / "renamed.txt")
        report = validate_v3_state(self._state(), self.project_dir)
        self.assertTrue(
            any("required output does not exist" in error for error in report.errors),
            report.errors,
        )

    def test_an_output_that_is_still_there_says_nothing(self) -> None:
        self._finish_the_task()
        report = validate_v3_state(self._state(), self.project_dir, already_done={"T01"})
        self.assertEqual(report.errors, [])
        self.assertEqual([w for w in report.warnings if "required output" in w], [])

    def test_a_malformed_task_list_does_not_break_the_previous_state_scan(self) -> None:
        from workspace_lib import _done_task_ids

        self.assertEqual(_done_task_ids({"tasks": "not a list"}), set())
        self.assertEqual(_done_task_ids({}), set())
        self.assertEqual(
            _done_task_ids({"tasks": ["nonsense", {"status": "DONE"}, {"id": "", "status": "DONE"}]}), set()
        )
        self.assertEqual(_done_task_ids({"tasks": [{"id": "T01", "status": "DONE"}]}), {"T01"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
