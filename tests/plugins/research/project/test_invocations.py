"""The invocation log: one line per project-scoped CLI call, written after the call, never in its way.

A retro needs to know what the tool refused, how often and why. Nothing recorded that before: the
operation journal keeps successful writes only. These tests pin the error classes, the write-after
behaviour under failure, and the bounded summary the retro reads.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import validate_workspace
from workspace_invocations import (
    ERROR_CLASSES,
    INVOCATIONS_FILENAME,
    MESSAGE_CHARS,
    classify,
    summarize_invocations,
)
from workspace_journal import OperationError
from workspace_lib import WorkspaceConflict, WorkspaceError, allocate_project, document_conflict_message

from tests.conftest import MANAGER


def _manage(args: list[Any]) -> int:
    with patch.object(sys, "argv", ["manage", *[str(argument) for argument in args]]):
        return manage_workspace.main()


def _validate(args: list[Any]) -> int:
    with patch.object(sys, "argv", ["validate", *[str(argument) for argument in args]]):
        return validate_workspace.main()


def _lines(project_dir: Path) -> list[dict[str, object]]:
    path = project_dir / INVOCATIONS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project = allocate_project(self.workspace, title="Logged", working_directory=self.target)


class ClassifyTests(unittest.TestCase):
    def test_every_class_is_reachable_and_success_is_none(self) -> None:
        self.assertIsNone(classify(None, 0))
        self.assertEqual("nonzero", classify(None, 1))
        self.assertEqual("conflict", classify(WorkspaceConflict("x"), 1))
        self.assertEqual("operation", classify(OperationError("x", {}), 1))
        self.assertEqual("refused", classify(WorkspaceError("x"), 1))
        self.assertEqual("crash", classify(RuntimeError("x"), 1))
        self.assertEqual({"conflict", "operation", "refused", "nonzero", "crash"}, set(ERROR_CLASSES))


class ManageLogTests(_Fixture):
    def test_a_success_and_each_failure_leave_one_line(self) -> None:
        self.assertEqual(0, _manage(["context", self.project]))
        stale = '{"id":"r","expected_revision":0,"tokens":{"spec":"' + "0" * 64 + '"},"sections":{"In scope":"x"}}'
        with patch("sys.stdin", new=_stdin(stale)):
            self.assertEqual(1, _manage(["workflow", self.project, "round", "-"]))
        self.assertEqual(1, _manage(["append", self.project, "decision", "--task", "T01", "--body", "x"]))
        with patch("sys.stdin", new=_stdin('{"title":"renamed"}')):
            self.assertEqual(1, _manage(["update", self.project, "-", "--expected-revision", "7"]))
        with patch("sys.stdin", new=_stdin("{}")):
            self.assertEqual(1, _manage(["workflow", self.project, "readiness", "-"]))
        lines = _lines(self.project)
        self.assertEqual(["context", "workflow", "append", "update", "workflow"], [line["command"] for line in lines])
        self.assertEqual([0, 1, 1, 1, 1], [line["exit"] for line in lines])
        self.assertEqual([None, "operation", "refused", "conflict", "nonzero"], [line.get("error") for line in lines])
        self.assertEqual(["", "round", "decision", "", "readiness"], [line["action"] for line in lines])
        self.assertTrue(lines[1]["message"])
        self.assertIn("revision conflict", str(lines[3]["message"]))
        self.assertEqual(7, lines[3]["expected_revision"])
        self.assertNotIn("expected_revision", lines[0])
        self.assertNotIn("message", lines[4])
        for line in lines:
            self.assertEqual("research-project", line["tool"])
            self.assertRegex(str(line["at"]), r"^\d{4}-\d{2}-\d{2}T")
            self.assertIsInstance(line["ms"], int)
            self.assertLessEqual(len(str(line.get("message", ""))), 200)
            self.assertRegex(str(line["version"]), r"^\d+\.\d+\.\d+")

    def test_a_crash_is_written_and_then_raised(self) -> None:
        with patch.object(manage_workspace, "_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _manage(["context", self.project])
        (line,) = _lines(self.project)
        self.assertEqual(("crash", "boom", 1), (line["error"], line["message"], line["exit"]))

    def test_an_unwritable_directory_changes_nothing_about_the_command(self) -> None:
        if os.geteuid() == 0:  # pragma: no cover - root writes anywhere
            self.skipTest("root ignores directory modes")
        mode = stat.S_IMODE(self.project.stat().st_mode)
        self.project.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.project.chmod, mode)
        self.assertEqual(0, _manage(["context", self.project]))
        self.assertEqual([], _lines(self.project))

    def test_a_missing_project_directory_gets_no_file(self) -> None:
        missing = self.root / "nope"
        self.assertEqual(1, _manage(["context", missing]))
        self.assertFalse(missing.exists())

    def test_root_level_commands_write_nothing(self) -> None:
        self.assertEqual(0, _manage(["list-projects", self.workspace]))
        self.assertEqual(0, _manage(["search-memory", "anything", "--workspace-root", self.workspace]))
        self.assertEqual([], _lines(self.project))
        self.assertFalse((self.workspace / INVOCATIONS_FILENAME).exists())

    def test_the_launcher_path_writes_the_same_line(self) -> None:
        import subprocess

        subprocess.run([sys.executable, str(MANAGER), "context", str(self.project)], check=True, capture_output=True)
        (line,) = _lines(self.project)
        self.assertEqual(("context", 0), (line["command"], line["exit"]))


class ValidateLogTests(_Fixture):
    def test_validate_logs_as_its_own_tool(self) -> None:
        self.assertEqual(0, _validate([self.project]))
        self.assertEqual(1, _validate([self.project, "--close"]))
        lines = _lines(self.project)
        self.assertEqual(["research-validate", "research-validate"], [line["tool"] for line in lines])
        self.assertEqual([("validate", ""), ("validate", "close")], [(row["command"], row["action"]) for row in lines])
        self.assertEqual([None, "nonzero"], [line.get("error") for line in lines])


class SummaryTests(_Fixture):
    def test_a_missing_file_is_zeros(self) -> None:
        summary = summarize_invocations(self.project)
        self.assertEqual(0, summary["total"])
        self.assertEqual({}, summary["by_command"])
        self.assertEqual({}, summary["failures"])
        self.assertEqual([], summary["failed_examples"])
        self.assertEqual((None, None), (summary["first_at"], summary["last_at"]))
        self.assertEqual((0, 0), (summary["ms_total"], summary["unparsed"]))

    def test_counts_are_bounded_and_bad_lines_are_counted(self) -> None:
        rows = []
        for index in range(15):
            rows.append(
                {"at": f"2026-10-15T10:{index:02d}:00+02:00", "tool": "research-project", "command": "workflow",
                 "action": "round", "exit": 1, "error": "refused", "message": f"no {index}", "ms": 10}
            )
        rows.append({"at": "2026-10-15T11:00:00+02:00", "tool": "research-project", "command": "read",
                     "action": "spec", "exit": 0, "ms": 5})
        text = "\n".join(json.dumps(row) for row in rows) + "\nnot json\n\n"
        (self.project / INVOCATIONS_FILENAME).write_text(text, encoding="utf-8")
        summary = summarize_invocations(self.project, example_limit=3)
        self.assertEqual(16, summary["total"])
        self.assertEqual({"workflow": 15, "read": 1}, summary["by_command"])
        self.assertEqual({"refused": 15}, summary["failures"])
        self.assertEqual(3, len(summary["failed_examples"]))
        self.assertEqual("no 0", summary["failed_examples"][0]["message"])
        self.assertEqual(1, summary["unparsed"])
        self.assertEqual("2026-10-15T10:00:00+02:00", summary["first_at"])
        self.assertEqual("2026-10-15T11:00:00+02:00", summary["last_at"])
        self.assertEqual(155, summary["ms_total"])


class DocumentConflictMessageTests(unittest.TestCase):
    """The log keeps the first 200 characters, so the head must carry the document and both tokens."""

    def test_the_logged_head_names_the_document_and_both_tokens(self) -> None:
        message = document_conflict_message("architecture", "a" * 64, "b" * 64, Path("/w/2026-10-21-001"))
        head = message.splitlines()[0][:MESSAGE_CHARS]
        self.assertIn("document conflict: architecture", head)
        self.assertIn("expected token " + "a" * 64, head)
        self.assertIn("found " + "b" * 64, head)
        self.assertIn("run 'research-project context /w/2026-10-21-001'", message)

    def test_use_and_prefix_change_only_their_own_part(self) -> None:
        message = document_conflict_message(
            "spec", "a" * 64, "missing", Path("/w/2026-10-21-001"),
            use="requirements_sha256", prefix="proposal token changed",
        )
        self.assertTrue(message.startswith("proposal token changed: spec"))
        self.assertIn("supply requirements_sha256 as the current token", message)
        self.assertIn("found missing", message)


def _stdin(text: str) -> io.StringIO:
    return io.StringIO(text)
