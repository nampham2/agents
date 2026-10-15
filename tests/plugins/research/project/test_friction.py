"""Friction entries: what the agent writes the moment an instruction, reference or gate got in its way.

A finding says what the agent saw about the work. A friction entry says what the tool or the skill
cost the agent, in a fixed vocabulary so a retro can count it across projects. These tests pin the
vocabulary, the required pointer, the file shape and the parser the retro reads with.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
from workspace_documents import FRICTION_FILENAME, FRICTION_KINDS, append_record, friction_entries
from workspace_lib import WorkspaceConflict, WorkspaceError, allocate_project


def _manage(args: list[Any], stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    with patch.object(sys, "argv", ["manage", *[str(a) for a in args]]), patch("sys.stdout", out), patch(
        "sys.stdin", io.StringIO(stdin)
    ):
        code = manage_workspace.main()
    return code, out.getvalue()


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "ws").mkdir()
        (root / "target").mkdir()
        self.project = allocate_project(root / "ws", title="Friction", working_directory=root / "target")
        self.path = self.project / FRICTION_FILENAME


class VocabularyTests(unittest.TestCase):
    def test_the_seven_kinds(self) -> None:
        self.assertEqual(
            (
                "unclear-instruction", "missing-information", "gate-refusal", "wasted-command",
                "reference-not-useful", "user-correction", "tool-defect",
            ),
            FRICTION_KINDS,
        )


class AppendTests(_Fixture):
    def test_an_entry_lands_with_kind_and_pointer_and_retries_are_idempotent(self) -> None:
        code, out = _manage(
            ["append", self.project, "friction", "--kind", "gate-refusal", "--pointer", "references/worktrees.md",
             "--entry-id", "wt-1", "--body", "task start refused the first write.\n\nA second paragraph."]
        )
        self.assertEqual(0, code)
        result = json.loads(out)
        self.assertEqual("friction.append", result["operation"])
        self.assertEqual(("wt-1", False), (result["entry_id"], result["existing"]))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Friction\n"))
        self.assertIn("<!-- research-entry: wt-1 -->", text)
        self.assertIn("Kind: gate-refusal\nPointer: references/worktrees.md\n\ntask start refused", text)
        code, out = _manage(
            ["append", self.project, "friction", "--kind", "gate-refusal", "--pointer", "references/worktrees.md",
             "--entry-id", "wt-1", "--body", "task start refused the first write.\n\nA second paragraph."]
        )
        self.assertEqual((0, True), (code, json.loads(out)["existing"]))
        with self.assertRaises(WorkspaceConflict):
            append_record(self.project, "friction", "other text", entry_id="wt-1",
                          friction_kind="gate-refusal", pointer="references/worktrees.md")
        self.assertEqual(1, text.count("research-entry:"))

    def test_refusals_write_nothing(self) -> None:
        cases = [
            (["append", self.project, "friction", "--pointer", "p", "--body", "x"], "needs --kind"),
            (["append", self.project, "friction", "--kind", "tool-defect", "--body", "x"], "needs --pointer"),
            (["append", self.project, "friction", "--kind", "tool-defect", "--pointer", "p", "--body", "  "], "empty"),
            (["append", self.project, "friction", "--kind", "tool-defect", "--pointer", "p", "--body", "x",
              "--task", "T01"], "task"),
            (["append", self.project, "friction", "--kind", "tool-defect", "--pointer", "a\nb", "--body", "x"], "line"),
            (["append", self.project, "friction", "--kind", "tool-defect", "--pointer", "p" * 201, "--body", "x"],
             "200"),
            (["append", self.project, "decision", "--kind", "tool-defect", "--body", "x"], "decision"),
            (["append", self.project, "decision", "--pointer", "p", "--body", "x"], "decision"),
        ]
        for args, label in cases:
            with self.subTest(label):
                self.assertEqual(1, _manage(args)[0])
        with self.assertRaises(SystemExit):
            _manage(["append", self.project, "friction", "--kind", "slow", "--pointer", "p", "--body", "x"])
        with self.assertRaises(WorkspaceError):
            append_record(self.project, "friction", "x", friction_kind="not-a-kind", pointer="p")
        self.assertFalse(self.path.exists())

    def test_read_returns_the_document(self) -> None:
        append_record(self.project, "friction", "the reference did not say which token",
                      friction_kind="missing-information", pointer="references/commands.md")
        code, out = _manage(["read", self.project, "friction"])
        self.assertEqual(0, code)
        self.assertIn("missing-information", json.loads(out)["text"])
        self.assertEqual(1, _manage(["read", self.project, "friction", "--task", "T01"])[0])


class ParserTests(_Fixture):
    def test_entries_come_back_in_order_with_their_fields(self) -> None:
        append_record(self.project, "friction", "first body\n\nwith two paragraphs", entry_id="a",
                      friction_kind="unclear-instruction", pointer="SKILL.md Plan tasks")
        append_record(self.project, "friction", "second body", entry_id="b",
                      friction_kind="wasted-command", pointer="workflow readiness")
        entries = friction_entries(self.path.read_text(encoding="utf-8"))
        self.assertEqual(["a", "b"], [entry["id"] for entry in entries])
        self.assertEqual(["unclear-instruction", "wasted-command"], [entry["kind"] for entry in entries])
        self.assertEqual(["SKILL.md Plan tasks", "workflow readiness"], [entry["pointer"] for entry in entries])
        self.assertEqual("first body\n\nwith two paragraphs", entries[0]["body"])
        self.assertRegex(entries[0]["at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_a_hand_written_entry_without_the_lines_is_kept_with_empty_fields(self) -> None:
        text = (
            "# Friction\n\n<!-- research-entry: odd -->\n<!-- research-body-sha256: " + "0" * 64 + " -->\n"
            '<a id="odd"></a>\n### 2026-10-15T10:00:00+02:00 — odd\n\njust prose\n'
        )
        (entry,) = friction_entries(text)
        self.assertEqual(("odd", "", "", "just prose"), (entry["id"], entry["kind"], entry["pointer"], entry["body"]))
        self.assertEqual([], friction_entries(""))
