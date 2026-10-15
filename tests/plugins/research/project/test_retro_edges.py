"""The failure paths of the retro reader and the invocation log: each degrades, none raises past its edge."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate_workspace
import workspace_dashboard
import workspace_invocations
import workspace_retro
from workspace_invocations import INVOCATIONS_FILENAME, plugin_version, summarize_invocations
from workspace_lib import WorkspaceError, allocate_project
from workspace_retro import (
    RETRO_DIRECTORY,
    _instant_or_none,
    _tally_transcript,
    project_signals,
    record_run,
    render_retro_index,
    retro_batch,
    transcript_signals,
)


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "workspace"
        self.root.mkdir()
        target = base / "target"
        target.mkdir()
        self.project = allocate_project(self.root, title="Edge", working_directory=target)
        state = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        state.update(status="DONE", updated="2026-10-10T10:00:00+02:00")
        (self.project / "project.json").write_text(json.dumps(state), encoding="utf-8")
        self.state = state


class ReaderEdgeTests(_Root):
    def test_bad_instants_and_unlistable_roots(self) -> None:
        self.assertIsNone(_instant_or_none("not a date"))
        self.assertIsNone(_instant_or_none(42))
        with self.assertRaises(WorkspaceError):
            retro_batch(self.root / self.project.name / "project.json")

    def test_unreadable_records_read_as_empty(self) -> None:
        (self.project / "reflection.md").write_bytes(b"\xff\xfe not utf-8")
        (self.project / "friction.md").write_bytes(b"\xff\xfe")
        row = project_signals(self.root, {**self.state, "tasks": ["not a task", {"status": "DONE"}]}, self.project.name)
        self.assertIsNone(row["retro_section"])
        self.assertEqual([], row["friction"])
        self.assertEqual({"": 1, "DONE": 1}, row["tasks"])
        with patch.object(workspace_retro, "staged_lesson_lines", side_effect=WorkspaceError("no")):
            self.assertEqual(0, project_signals(self.root, self.state, self.project.name)["staged_lessons"])

    def test_transcript_failures_yield_nothing(self) -> None:
        claude = self.root.parent / "claude"
        with patch.object(workspace_dashboard, "SessionIndex", side_effect=RuntimeError("layout changed")):
            self.assertEqual([], transcript_signals(self.project.name, claude))
        directory = claude / "projects" / "-x"
        directory.mkdir(parents=True)
        (directory / "dir.jsonl").mkdir()
        self.assertIsNone(_tally_transcript(directory / "dir.jsonl"))
        path = directory / "odd.jsonl"
        path.write_text(json.dumps({"message": {"content": ["a string block", {"type": "text"}]}}) + "\n", "utf-8")
        tally = _tally_transcript(path)
        assert tally is not None
        self.assertEqual(({}, 0, 1), (tally["tool_calls"], tally["errors"], tally["lines"]))
        with patch.object(workspace_dashboard.SessionIndex, "find", return_value={self.project.name: [{"id": "dir"}]}):
            self.assertEqual([], transcript_signals(self.project.name, claude))


class LedgerEdgeTests(_Root):
    def request(self) -> dict[str, object]:
        return {"id": "r1", "source": "s", "batch": [self.project.name], "candidates": []}

    def test_a_ledger_directory_that_cannot_be_made_is_a_workspace_error(self) -> None:
        (self.root / RETRO_DIRECTORY).write_text("a file where the directory goes", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            record_run(self.root, self.request())

    def test_the_index_names_records_it_cannot_read(self) -> None:
        record_run(self.root, self.request())
        (self.root / RETRO_DIRECTORY / "bytes.md").write_bytes(b"\xff\xfe")
        (self.root / RETRO_DIRECTORY / "block-less.md").write_text("# no block\n", encoding="utf-8")
        (self.root / RETRO_DIRECTORY / "bad-block.md").write_text("<!-- research-retro-v1 {\"id\": 3} -->\n", "utf-8")
        index = render_retro_index(self.root)
        for name in ("bytes.md", "block-less.md", "bad-block.md"):
            self.assertIn(f"- {name} — unreadable record", index)
        self.assertIn("[r1](retro/r1.md)", index)


class InvocationEdgeTests(_Root):
    def test_an_unreadable_manifest_gives_no_version(self) -> None:
        broken = patch("workspace_invocations.json.loads", side_effect=ValueError)
        with patch.object(workspace_invocations, "_VERSION", []), broken:
            self.assertEqual("", plugin_version())
        self.assertTrue(plugin_version())

    def test_a_json_line_that_is_not_an_object_is_unparsed(self) -> None:
        (self.project / INVOCATIONS_FILENAME).write_text("[1, 2]\n{\"command\": \"read\", \"exit\": 0}\n", "utf-8")
        summary = summarize_invocations(self.project)
        self.assertEqual((1, 1), (summary["unparsed"], summary["total"]))

    def test_a_crashing_validation_is_logged_then_raised(self) -> None:
        with patch.object(validate_workspace, "validate_project", side_effect=RuntimeError("boom")), patch.object(
            sys, "argv", ["validate", str(self.project)]
        ), patch("sys.stdout", io.StringIO()):
            with self.assertRaises(RuntimeError):
                validate_workspace.main()
        (line,) = [json.loads(row) for row in (self.project / INVOCATIONS_FILENAME).read_text("utf-8").splitlines()]
        self.assertEqual(("research-validate", "crash", "boom"), (line["tool"], line["error"], line["message"]))
