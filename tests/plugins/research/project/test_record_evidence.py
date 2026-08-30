"""Tests for `record-evidence`.

The subcommand exists because four evidence records across three projects claimed a verification
that had not passed. So these tests pin the property that matters: what lands in `evidence.md` is
derived from the completed process, and a failing command can never be recorded as a pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
from workspace_lib import WorkspaceError, allocate_project, record_evidence

from tests.conftest import MANAGER


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


class _ProjectFixture(unittest.TestCase):
    """A project with a single TODO task and a real working directory."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace_root, title="Recording", working_directory=self.target)
        self.evidence = self.project_dir / "evidence.md"
        state = self._state()
        state["tasks"] = [self._task("T01")]
        self._write(state)

    def _state(self) -> dict[str, Any]:
        return json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))

    def _write(self, state: dict[str, Any]) -> None:
        (self.project_dir / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _task(task_id: str) -> dict[str, Any]:
        return {
            "id": task_id,
            "name": "Do the thing",
            "status": "TODO",
            "depends_on": [],
            "outputs": [],
            "success_criteria": "It is done",
            "verification": "A command says so",
            "evidence": [],
            "effect": {"kind": "local_write", "description": "write"},
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


class RecordEvidenceTests(_ProjectFixture):
    def test_a_passing_command_is_recorded_with_its_real_exit_code(self) -> None:
        code = record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('hello')"])
        self.assertEqual(code, 0)
        text = self.evidence.read_text(encoding="utf-8")
        self.assertIn("## T01", text)
        self.assertIn("Exit code: 0 (passed)", text)
        self.assertIn("hello", text)

    def test_a_failing_command_is_recorded_as_a_failure_not_a_pass(self) -> None:
        code = record_evidence(
            self.project_dir,
            "T01",
            [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        )
        self.assertEqual(code, 3)
        text = self.evidence.read_text(encoding="utf-8")
        self.assertIn("Exit code: 3 (FAILED)", text)
        self.assertIn("boom", text)
        self.assertNotIn("(passed)", text)

    def test_canonical_state_is_never_touched(self) -> None:
        before = (self.project_dir / "project.json").read_bytes()
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "raise SystemExit(1)"])
        self.assertEqual((self.project_dir / "project.json").read_bytes(), before)

    def test_no_shell_is_interposed(self) -> None:
        # If a shell ran this, the metacharacters would redirect and the marker would not appear
        # in the recorded output as literal text.
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('a > b && c')"])
        self.assertIn("a > b && c", self.evidence.read_text(encoding="utf-8"))
        self.assertFalse((self.target / "b").exists())

    def test_an_empty_command_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", [])
        self.assertIn("requires a command", str(caught.exception))

    def test_an_unknown_task_names_the_tasks_that_exist(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T99", [sys.executable, "-c", "pass"])
        message = str(caught.exception)
        self.assertIn("T99", message)
        self.assertIn("T01", message)

    def test_a_missing_executable_is_an_actionable_error(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", ["definitely-not-a-real-binary-xyz"])
        self.assertIn("definitely-not-a-real-binary-xyz", str(caught.exception))

    def test_a_timeout_is_an_actionable_error(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(
                self.project_dir,
                "T01",
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=0.2,
            )
        self.assertIn("timed out", str(caught.exception))

    def test_a_long_output_is_elided_rather_than_embedded_whole(self) -> None:
        record_evidence(
            self.project_dir,
            "T01",
            [sys.executable, "-c", "for i in range(200): print('line', i)"],
            tail_lines=5,
        )
        text = self.evidence.read_text(encoding="utf-8")
        self.assertIn("earlier line(s) elided", text)
        self.assertIn("line 199", text)
        self.assertNotIn("line 0\n", text)

    def test_a_silent_command_says_so(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        self.assertIn("No output.", self.evidence.read_text(encoding="utf-8"))

    def test_a_missing_working_directory_is_refused(self) -> None:
        state = self._state()
        state["working_directory"] = str(self.root / "gone")
        self._write(state)
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        self.assertIn("working_directory", str(caught.exception))

    def test_a_project_without_a_task_list_is_refused(self) -> None:
        state = self._state()
        state["tasks"] = "not a list"
        self._write(state)
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        self.assertIn("task list", str(caught.exception))

    def test_the_skeleton_placeholder_gives_way_to_the_first_entry(self) -> None:
        self.assertIn("No task evidence recorded yet.", self.evidence.read_text(encoding="utf-8"))
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('x')"])
        text = self.evidence.read_text(encoding="utf-8")
        self.assertNotIn("No task evidence recorded yet.", text)
        self.assertIn("## T01", text)

    def test_a_missing_evidence_file_is_created_with_a_heading(self) -> None:
        self.evidence.unlink()
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('x')"])
        text = self.evidence.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Evidence"))
        self.assertIn("## T01", text)

    def test_an_unwritable_evidence_file_is_an_actionable_error(self) -> None:
        with patch.object(Path, "write_text", side_effect=OSError("read-only")):
            with self.assertRaises(WorkspaceError) as caught:
                record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        self.assertIn("cannot write", str(caught.exception))

    def test_an_os_error_starting_the_command_is_actionable(self) -> None:
        with patch("workspace_lib.subprocess.run", side_effect=OSError("exec format error")):
            with self.assertRaises(WorkspaceError) as caught:
                record_evidence(self.project_dir, "T01", ["/bin/sh"])
        self.assertIn("exec format error", str(caught.exception))


class RecordEvidenceCliTests(_ProjectFixture):
    def test_the_cli_returns_zero_and_names_the_evidence_file(self) -> None:
        code = _call_manage(
            [
                "record-evidence",
                str(self.project_dir),
                "--task",
                "T01",
                "--",
                sys.executable,
                "-c",
                "print('ok')",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("Exit code: 0 (passed)", self.evidence.read_text(encoding="utf-8"))

    def test_the_cli_returns_one_for_a_failing_command(self) -> None:
        code = _call_manage(
            [
                "record-evidence",
                str(self.project_dir),
                "--task",
                "T01",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(2)",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("Exit code: 2 (FAILED)", self.evidence.read_text(encoding="utf-8"))

    def test_the_cli_accepts_a_command_carrying_its_own_options(self) -> None:
        # The reason the separator is split off before argparse sees it: a real verification
        # command is full of flags, and one of them may be a bare `--`.
        code = _call_manage(
            [
                "record-evidence",
                str(self.project_dir),
                "--task",
                "T01",
                "--tail-lines",
                "3",
                "--",
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1:])",
                "--flag",
                "--",
                "trailing",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("['--flag', '--', 'trailing']", self.evidence.read_text(encoding="utf-8"))

    def test_the_cli_refuses_a_missing_separator_instead_of_guessing(self) -> None:
        code = _call_manage(["record-evidence", str(self.project_dir), "--task", "T01"])
        self.assertEqual(code, 1)
        self.assertFalse(self.evidence.read_text(encoding="utf-8").strip().endswith("passed)"))

    def test_the_cli_reports_an_unknown_task_without_a_traceback(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "record-evidence",
                str(self.project_dir),
                "--task",
                "T99",
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
