"""Tests for `record-evidence`.

The subcommand exists because four evidence records across three projects claimed a verification
that had not passed. So these tests pin the property that matters: what lands in `evidence.md` is
derived from the completed process, and a failing command can never be recorded as a pass.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import workspace_lib
from workspace_lib import (
    EVIDENCE_HEADING_MAX_CHARS,
    WorkspaceError,
    allocate_project,
    record_evidence,
)

from tests.conftest import MANAGER


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


@contextmanager
def _failing_evidence_write(reason: str) -> Iterator[None]:
    """Make the atomic write of `evidence.md`, and only that write, fail."""
    real = workspace_lib.atomic_write_text

    def write(path: Path, content: str) -> None:
        if path.name == "evidence.md":
            raise OSError(reason)
        real(path, content)

    with patch.object(workspace_lib, "atomic_write_text", side_effect=write):
        yield


@contextmanager
def _interrupted_evidence_replace() -> Iterator[None]:
    """Fail the final rename of `evidence.md`, as a crash between write and replace would."""
    real = workspace_lib.os.replace

    def replace(source: Path, destination: Path) -> None:
        if destination.name == "evidence.md":
            raise OSError("interrupted before the rename")
        real(source, destination)

    with patch.object(workspace_lib.os, "replace", side_effect=replace):
        yield


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
        # Scoped to `evidence.md`: since T06 the append runs under the project lock, whose own
        # owner.json goes through the same writer, and failing that instead would prove nothing
        # about the file this test is named for.
        with _failing_evidence_write("read-only"):
            with self.assertRaises(WorkspaceError) as caught:
                record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass"])
        self.assertIn("cannot write", str(caught.exception))
        self.assertFalse((self.project_dir / ".project.lock").exists(), "the lock must be released")

    def test_an_os_error_starting_the_command_is_actionable(self) -> None:
        with patch("workspace_lib.subprocess.run", side_effect=OSError("exec format error")):
            with self.assertRaises(WorkspaceError) as caught:
                record_evidence(self.project_dir, "T01", ["/bin/sh"])
        self.assertIn("exec format error", str(caught.exception))


class EvidenceHeadingTests(_ProjectFixture):
    """A Markdown heading is one line; a command is not obliged to be."""

    def _heading(self) -> str:
        headings = [
            line for line in self.evidence.read_text(encoding="utf-8").splitlines() if line.startswith("## T01")
        ]
        self.assertEqual(len(headings), 1, "expected exactly one recorded entry")
        return headings[0]

    def test_a_single_line_command_is_left_exactly_as_it_was(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print(1)"])
        self.assertEqual(self._heading(), f"## T01 — {shlex.join([sys.executable, '-c', 'print(1)'])}")
        # Nothing was collapsed or cut, so there is no reason to repeat the command in the body.
        self.assertNotIn("Command:", self.evidence.read_text(encoding="utf-8"))

    def test_a_multi_line_command_becomes_one_heading_line(self) -> None:
        script = "import sys\nfor value in (1, 2):\n    print(value)\n"
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", script])
        text = self.evidence.read_text(encoding="utf-8")
        heading = self._heading()
        self.assertNotIn("\n", heading)
        self.assertIn("for value in (1, 2):", heading)
        # The script's own lines must not have escaped the heading into the document.
        self.assertNotIn("\n    print(value)\n", text.split("- Recorded:")[0])
        self.assertIn("Command:", text)
        self.assertIn(shlex.join([sys.executable, "-c", script]), text)

    def test_a_long_command_is_truncated_with_an_ellipsis(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('x')  #" + " y" * 300])
        heading = self._heading()
        self.assertTrue(heading.endswith("\u2026"), heading)
        self.assertLessEqual(len(heading) - len("## T01 — "), EVIDENCE_HEADING_MAX_CHARS)

    def test_a_command_at_the_limit_is_not_truncated(self) -> None:
        padding = "#" * (EVIDENCE_HEADING_MAX_CHARS - len(shlex.join([sys.executable, "-c", ""])))
        command = [sys.executable, "-c", padding]
        self.assertEqual(len(shlex.join(command)), EVIDENCE_HEADING_MAX_CHARS)
        record_evidence(self.project_dir, "T01", command)
        self.assertEqual(self._heading(), f"## T01 — {shlex.join(command)}")

    def test_a_command_containing_a_fence_does_not_end_the_block_early(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('```')\n# " + "z" * 200])
        text = self.evidence.read_text(encoding="utf-8")
        # Scoped to the command block: the recorded output contains the same fence, and since T20
        # its block is sized too, so counting across the whole entry would count both.
        body = text.split("Command:", 1)[1].split("stdout (tail):", 1)[0]
        fence = body.split("\n")[2]
        self.assertGreaterEqual(len(fence), 4, "the fence must be longer than the backticks inside it")
        self.assertEqual(body.count(fence), 2, "the block must open and close on the sized fence")

    def test_output_containing_a_fence_does_not_close_its_block_early(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('```')\nprint('after')"])
        text = self.evidence.read_text(encoding="utf-8")
        block = text.split("stdout (tail):", 1)[1]
        fence = block.split("\n")[2]
        self.assertEqual(fence, "````", "the fence must outgrow the backticks in the output")
        self.assertEqual(block.count(fence), 2, "the block must open and close on the sized fence")
        self.assertIn("after", block.split(fence)[1], "output after the fence stays inside the block")

    def test_output_without_a_fence_is_written_as_before(self) -> None:
        record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('plain')"])
        block = self.evidence.read_text(encoding="utf-8").split("stdout (tail):", 1)[1]
        self.assertEqual(block.split("\n")[2], "```")


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


class MisrootedPathTests(_ProjectFixture):
    """The most repeated mistake in this workspace: a path relative to the wrong directory.

    Six incidents across five projects, three of them after the lesson was written down and one with
    the reflection entry on screen. Documentation demonstrably did not stop it, so the refusal below
    is the fix, and these tests pin both halves of it: it fires on the real mistake, and it stays
    silent on every invocation that already worked.
    """

    def test_a_project_relative_path_is_refused_before_the_command_runs(self) -> None:
        artifacts = self.project_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "check.sh").write_text("exit 0\n", encoding="utf-8")
        before = self.evidence.read_text(encoding="utf-8") if self.evidence.exists() else ""

        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", ["bash", "artifacts/check.sh"])

        message = str(caught.exception)
        self.assertIn("artifacts/check.sh", message)
        self.assertIn(str(artifacts / "check.sh"), message)
        self.assertIn(str(self.target), message)
        after = self.evidence.read_text(encoding="utf-8") if self.evidence.exists() else ""
        self.assertEqual(after, before, "a refused command must not be recorded")

    def test_a_target_relative_path_still_runs(self) -> None:
        (self.target / "script.py").write_text("print('from target')\n", encoding="utf-8")

        code = record_evidence(self.project_dir, "T01", [sys.executable, "script.py"])

        self.assertEqual(code, 0)
        self.assertIn("from target", self.evidence.read_text(encoding="utf-8"))

    def test_a_path_present_in_both_places_is_not_refused(self) -> None:
        (self.target / "both.py").write_text("print('target copy')\n", encoding="utf-8")
        (self.project_dir / "both.py").write_text("print('project copy')\n", encoding="utf-8")

        code = record_evidence(self.project_dir, "T01", [sys.executable, "both.py"])

        self.assertEqual(code, 0)
        text = self.evidence.read_text(encoding="utf-8")
        self.assertIn("target copy", text)
        self.assertNotIn("project copy", text)

    def test_an_option_that_matches_a_project_file_is_not_treated_as_a_path(self) -> None:
        (self.project_dir / "spec.md").write_text("# spec\n", encoding="utf-8")

        code = record_evidence(
            self.project_dir, "T01", [sys.executable, "-c", "print('ok')", "--spec.md"]
        )

        self.assertEqual(code, 0)

    def test_an_argument_too_long_to_be_a_filename_is_left_to_the_command(self) -> None:
        # Probing this as a path raises OSError rather than returning False. The existing
        # truncation test caught it, which is the argument for negative controls: the guard was
        # correct about the mistake and wrong about everything else.
        code = record_evidence(
            self.project_dir, "T01", [sys.executable, "-c", "print('x')  #" + " y" * 300]
        )

        self.assertEqual(code, 0)

    def test_a_parent_relative_path_is_left_to_the_command(self) -> None:
        code = record_evidence(
            self.project_dir, "T01", [sys.executable, "-c", "pass", "../elsewhere.txt"]
        )

        self.assertEqual(code, 0)


class ShellOperatorTests(_ProjectFixture):
    """A pipeline written into argv, where no shell exists to interpret it.

    `record-evidence` runs with `shell=False`, so `-- pytest -q | tail -2` hands `pytest` the three
    extra arguments `|`, `tail` and `-2`. What the caller then sees is pytest's own complaint about
    an unrecognised path, which reads as the verification failing rather than as the invocation being
    wrong. Both halves are pinned here: the refusal fires on a bare operator, and stays silent for
    the same characters carried inside an argument.
    """

    def test_a_bare_pipe_is_refused_and_names_the_wrapper(self) -> None:
        before = self.evidence.read_text(encoding="utf-8")

        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print(1)", "|", "tail", "-2"])

        message = str(caught.exception)
        self.assertIn("argv element #4: '|'", message)
        self.assertIn("bash -lc", message)
        # The suggestion has to be runnable, not merely indicative: the whole intended pipeline is
        # quoted as one argument to the shell.
        self.assertIn(shlex.quote(f"{sys.executable} -c print(1) | tail -2"), message)
        self.assertEqual(self.evidence.read_text(encoding="utf-8"), before, "nothing may be recorded")

    def test_every_operator_is_refused(self) -> None:
        for operator in ("|", "||", "&&", ";", ">", ">>"):
            with self.subTest(operator=operator):
                with self.assertRaises(WorkspaceError) as caught:
                    record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass", operator, "out"])
                self.assertIn(repr(operator), str(caught.exception))

    def test_the_refusal_precedes_every_other_check(self) -> None:
        # An unknown task and a bare operator are both wrong; the operator is the one the caller can
        # act on, so it must not be masked by the task lookup.
        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(self.project_dir, "T99", [sys.executable, "-c", "pass", "&&", "true"])
        self.assertIn("&&", str(caught.exception))
        self.assertNotIn("unknown task", str(caught.exception))

    def test_an_operator_inside_a_quoted_argument_still_runs(self) -> None:
        # The negative control, and the reason the check looks at whole argv elements: this command
        # is correct as written, and `test_no_shell_is_interposed` already relies on it running.
        code = record_evidence(self.project_dir, "T01", [sys.executable, "-c", "print('a | b > c && d; e')"])

        self.assertEqual(code, 0)
        self.assertIn("a | b > c && d; e", self.evidence.read_text(encoding="utf-8"))

    def test_an_argument_that_merely_contains_an_operator_still_runs(self) -> None:
        code = record_evidence(self.project_dir, "T01", [sys.executable, "-c", "pass", "--filter=a||b"])

        self.assertEqual(code, 0)

    def test_the_cli_refuses_without_a_traceback(self) -> None:
        code = _call_manage(
            [
                "record-evidence",
                str(self.project_dir),
                "--task",
                "T01",
                "--",
                sys.executable,
                "-c",
                "print(1)",
                ">",
                "out.txt",
            ]
        )

        self.assertEqual(code, 1)
        self.assertFalse((self.target / "out.txt").exists())


class EvidenceAppendDurabilityTests(_ProjectFixture):
    """Appending an entry is a read-modify-write of a file that is the project's only record.

    The previous form did it with an unlocked `write_text`, which truncates before writing. Two
    failures follow from that and both have precedent in this workspace: an interruption mid-write
    destroys every earlier entry along with the one being added, and two concurrent appends both read
    the same file and the second silently discards the first.
    """

    def _record(self, marker: str) -> int:
        return record_evidence(self.project_dir, "T01", [sys.executable, "-c", f"print({marker!r})"])

    def test_an_interrupted_write_leaves_the_previous_file_intact(self) -> None:
        self._record("first")
        before = self.evidence.read_bytes()

        with _interrupted_evidence_replace():
            with self.assertRaises(WorkspaceError) as caught:
                self._record("second")

        self.assertIn("cannot write", str(caught.exception))
        self.assertEqual(self.evidence.read_bytes(), before, "the earlier entry must survive")
        self.assertNotIn("second", before.decode("utf-8"))

    def test_a_failed_write_leaves_no_temporary_file_behind(self) -> None:
        with _interrupted_evidence_replace():
            with self.assertRaises(WorkspaceError):
                self._record("first")

        leftovers = [path.name for path in self.project_dir.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_two_serialized_appends_both_survive(self) -> None:
        self._record("first")
        self._record("second")

        text = self.evidence.read_text(encoding="utf-8")
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertLess(text.index("first"), text.index("second"), "entries append in order")
        self.assertEqual(text.count("## T01"), 2)

    def test_the_append_takes_the_project_lock(self) -> None:
        # The lock is what makes "serialized" true rather than hopeful. Held by someone else, the
        # append refuses instead of overwriting whatever that holder is about to write.
        lock = self.project_dir / ".project.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({"pid": 1, "created": "held"}), encoding="utf-8")
        before = self.evidence.read_bytes()

        with self.assertRaises(WorkspaceError) as caught:
            record_evidence(
                self.project_dir, "T01", [sys.executable, "-c", "print('x')"], lock_timeout=0.05
            )

        self.assertIn("lock is busy", str(caught.exception))
        self.assertEqual(self.evidence.read_bytes(), before)

    def test_the_lock_is_released_for_the_next_append(self) -> None:
        self._record("first")
        self.assertFalse((self.project_dir / ".project.lock").exists())
        self._record("second")
        self.assertFalse((self.project_dir / ".project.lock").exists())

    def test_the_recorded_entry_format_is_unchanged(self) -> None:
        # The atomic write replaces how the bytes land, not what they are.
        self._record("hello")

        text = self.evidence.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Evidence\n"))
        self.assertIn("- Working directory: ", text)
        self.assertIn("- Exit code: 0 (passed)", text)
        self.assertIn("stdout (tail):", text)
        self.assertTrue(text.endswith("\n"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
