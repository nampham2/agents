"""`retro batch`: the closed projects since the last run, each folded into one bounded row.

The fixture root holds what a real root holds after a few weeks: a closed project with the new
layers, a closed legacy project, an active one, one with an unreadable `project.json`, the `memory/`
directory, and a ledger with one earlier run. The tests pin the selection rules, the bounds, the
degradation on missing or broken input, and the transcript tally.
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
from workspace_documents import append_record
from workspace_lib import WorkspaceError, allocate_project
from workspace_retro import (
    FRICTION_LIMIT,
    RETRO_DIRECTORY,
    RETRO_SECTION_CHARS,
    next_run_id,
    parse_instant,
    previous_runs,
    retro_batch,
    transcript_signals,
)

from tests.conftest import backdate_project

RETRO = "# Reflection\n\n## Outcome\n\nDone.\n\n## Retro\n\n" + "The gate refused the first write twice. " * 4


def _manage(args: list[Any], stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    with patch.object(sys, "argv", ["manage", *[str(a) for a in args]]), patch("sys.stdout", out), patch(
        "sys.stdin", io.StringIO(stdin)
    ):
        code = manage_workspace.main()
    return code, out.getvalue()


def _set(project: Path, **fields: object) -> None:
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(fields)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _ledger(root: Path, run_id: str, batch: list[str], through: str, candidates: list[dict[str, str]]) -> None:
    block = {"id": run_id, "recorded_at": through, "through": through, "batch": batch, "candidates": candidates}
    (root / RETRO_DIRECTORY).mkdir(exist_ok=True)
    (root / RETRO_DIRECTORY / f"{run_id}.md").write_text(
        f"# Retro {run_id}\n\n<!-- research-retro-v1 {json.dumps(block)} -->\n", encoding="utf-8"
    )


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        # Named `workspace`: the dashboard's session index recognises a project by `workspace/<id>`.
        self.root = base / "workspace"
        self.root.mkdir()
        self.target = base / "target"
        self.target.mkdir()
        self.claude = base / "claude"
        # Allocation numbers projects within the day; the fixture renames nothing and fixes the dates.
        self.layered = allocate_project(self.root, title="Layered", working_directory=self.target)
        self.legacy = allocate_project(self.root, title="Legacy", working_directory=self.target)
        self.active = allocate_project(self.root, title="Active", working_directory=self.target)
        self.broken = allocate_project(self.root, title="Broken", working_directory=self.target)
        self.old = allocate_project(self.root, title="Covered", working_directory=self.target)
        # The layered project: a log with a refusal, two friction entries, a retro section, failed evidence.
        _manage(["context", self.layered])
        _manage(["append", self.layered, "decision", "--task", "T01", "--body", "x"])
        _manage(["append", self.layered, "decision", "--body", "2026-10-12: user chose the short path."])
        append_record(self.layered, "friction", "the worktree reference sent me to a refused step",
                      friction_kind="gate-refusal", pointer="references/worktrees.md")
        append_record(self.layered, "friction", "no reference named the field",
                      friction_kind="missing-information", pointer="commands.md")
        (self.layered / "reflection.md").write_text(RETRO, encoding="utf-8")
        _manage(["record-evidence", self.layered, "--step", "report", "--", "sh", "-c", "exit 3"])
        _manage(["record-evidence", self.layered, "--step", "report", "--", "true"])
        _set(self.layered, status="DONE", updated="2026-10-12T10:00:00+02:00")
        backdate_project(self.legacy)
        _set(self.legacy, status="CANCELLED", updated="2026-10-11T10:00:00+02:00")
        _set(self.active, status="EXECUTING", updated="2026-10-13T10:00:00+02:00")
        (self.broken / "project.json").write_text("{not json", encoding="utf-8")
        _set(self.old, status="DONE", updated="2026-10-01T10:00:00+02:00")
        _ledger(self.root, "2026-10-02-001", [self.old.name], "2026-10-01T10:00:00+02:00", [
            {"id": "c1", "title": "Shorter resume", "category": "tool-economy", "severity": "low",
             "disposition": "rejected"},
            {"id": "c2", "title": "Name the heading", "category": "gates", "severity": "high",
             "disposition": "deferred"},
            {"id": "c3", "title": "Done one", "category": "gates", "severity": "high", "disposition": "accepted"},
        ])


class SelectionTests(_Root):
    def test_default_run_starts_after_the_last_cursor_and_skips_covered_and_active(self) -> None:
        result = retro_batch(self.root, transcripts=False)
        self.assertEqual(
            [self.legacy.name, self.layered.name, self.broken.name], [row["project"] for row in result["projects"]]
        )
        self.assertEqual([self.active.name], result["pending"])
        self.assertEqual(1, result["previous"]["runs"])
        self.assertEqual("2026-10-01T10:00:00+02:00", result["previous"]["through"])
        self.assertEqual(["Shorter resume"], [item["title"] for item in result["previous"]["rejected"]])
        self.assertEqual(["Name the heading"], [item["title"] for item in result["previous"]["deferred"]])
        self.assertEqual(1, result["covered"])
        self.assertEqual((3, 0, None), (result["total"], result["offset"], result["next_offset"]))
        self.assertTrue(result["projects"][-1]["unreadable"])

    def test_since_reaches_back_and_lists_the_unreadable_project(self) -> None:
        result = retro_batch(self.root, since="2026-09-01", transcripts=False)
        names = [row["project"] for row in result["projects"]]
        self.assertIn(self.old.name, names)
        self.assertIn(self.broken.name, names)
        self.assertEqual("2026-09-01T00:00:00", result["selection"]["cursor"][:19])
        broken = next(row for row in result["projects"] if row["project"] == self.broken.name)
        self.assertTrue(broken["unreadable"])
        self.assertIn("project.json", broken["error"])

    def test_explicit_projects_bypass_the_filters(self) -> None:
        result = retro_batch(self.root, projects=[self.old.name], transcripts=False)
        self.assertEqual([self.old.name], [row["project"] for row in result["projects"]])
        self.assertEqual([self.old.name], result["selection"]["projects"])
        with self.assertRaises(WorkspaceError):
            retro_batch(self.root, projects=["nope"])
        with self.assertRaises(WorkspaceError):
            retro_batch(self.root, projects=["2099-01-01-001"])
        with self.assertRaises(WorkspaceError):
            retro_batch(self.root, since="yesterday")
        with self.assertRaises(WorkspaceError):
            retro_batch(self.root, limit=0)

    def test_paging(self) -> None:
        first = retro_batch(self.root, since="2026-09-01", transcripts=False, limit=2)
        second = retro_batch(self.root, since="2026-09-01", transcripts=False, limit=2, offset=first["next_offset"])
        self.assertEqual(2, len(first["projects"]))
        self.assertEqual(first["total"], len(first["projects"]) + len(second["projects"]))
        self.assertIsNone(second["next_offset"])

    def test_a_root_without_a_ledger_has_no_cursor(self) -> None:
        import shutil

        shutil.rmtree(self.root / RETRO_DIRECTORY)
        result = retro_batch(self.root, transcripts=False)
        self.assertEqual((0, None), (result["previous"]["runs"], result["previous"]["run"]))
        self.assertIsNone(result["selection"]["cursor"])
        self.assertEqual(4, result["total"])
        self.assertEqual(f"{__import__('datetime').date.today().isoformat()}-001", result["next_id"])

    def test_an_unreadable_ledger_file_is_named_not_fatal(self) -> None:
        (self.root / RETRO_DIRECTORY / "junk.md").write_text("no block here", encoding="utf-8")
        previous = previous_runs(self.root)
        self.assertEqual(["junk.md"], previous["unreadable"])
        self.assertEqual(1, previous["runs"])
        self.assertEqual("2026-10-02-002", next_run_id(self.root, "2026-10-02"))


class SignalTests(_Root):
    def test_the_layered_row(self) -> None:
        row = self._row(self.layered.name)
        self.assertEqual({"invocations": True, "friction": True, "retro_section": True}, row["layers"])
        self.assertFalse(row["legacy"])
        self.assertGreaterEqual(row["invocations"]["total"], 3)
        self.assertEqual(1, row["invocations"]["failures"]["refused"])
        self.assertEqual(["gate-refusal", "missing-information"], [f["kind"] for f in row["friction"]])
        self.assertEqual(2, row["friction_total"])
        self.assertTrue(row["retro_section"].startswith("The gate refused"))
        self.assertFalse(row["retro_section_truncated"])
        self.assertEqual((2, 1), (row["evidence"]["records"], row["evidence"]["failed"]))
        self.assertIn("exit 3", row["evidence"]["failed_heads"][0])
        self.assertEqual({}, row["tasks"])
        self.assertEqual(("DONE", "not_required"), (row["status"], row["review"]))
        self.assertEqual((0, 0, 1), (row["worktrees"], row["staged_lessons"], row["decisions"]))

    def _row(self, name: str) -> dict[str, Any]:
        return next(r for r in retro_batch(self.root, transcripts=False)["projects"] if r["project"] == name)

    def test_the_legacy_row(self) -> None:
        row = self._row(self.legacy.name)
        self.assertTrue(row["legacy"])
        self.assertIsNone(row["retro_section"])
        self.assertEqual(([], 0, 0), (row["friction"], row["invocations"]["total"], row["evidence"]["records"]))

    def test_bounds(self) -> None:
        for index in range(FRICTION_LIMIT + 5):
            append_record(self.layered, "friction", f"entry {index}", friction_kind="tool-defect", pointer="x")
        text = "# R\n\n## Retro\n\n" + "y" * (RETRO_SECTION_CHARS + 50)
        (self.layered / "reflection.md").write_text(text, encoding="utf-8")
        row = self._row(self.layered.name)
        self.assertEqual(FRICTION_LIMIT, len(row["friction"]))
        self.assertEqual(FRICTION_LIMIT + 7, row["friction_total"])
        self.assertEqual(RETRO_SECTION_CHARS, len(row["retro_section"]))
        self.assertTrue(row["retro_section_truncated"])


class TranscriptTests(_Root):
    def _transcript(self, session: str, body: list[dict[str, object]]) -> Path:
        directory = self.claude / "projects" / "-Users-x"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session}.jsonl"
        mention = f"research-project update {self.root}/{self.layered.name} -"
        lines = [json.dumps({"cwd": str(self.target), "type": "user", "message": {"role": "user", "content": mention}})]
        lines += [json.dumps(entry) for entry in body]
        lines += [json.dumps({"type": "user", "message": {"content": f"see {self.root}/{self.layered.name}"}})] * 3
        lines.append("not json")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_tool_calls_and_errors_are_tallied(self) -> None:
        self._transcript("s1", [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}}, {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "tool_use", "name": "Read", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "x"},
                                                      {"type": "tool_result", "content": "ok"}]}},
        ])
        (transcript,) = transcript_signals(self.layered.name, self.claude)
        self.assertEqual("s1", transcript["id"])
        self.assertEqual({"Bash": 2, "Read": 1}, transcript["tool_calls"])
        self.assertEqual(1, transcript["errors"])
        self.assertEqual(7, transcript["lines"])
        result = retro_batch(self.root, claude_dir=self.claude)
        row = next(r for r in result["projects"] if r["project"] == self.layered.name)
        self.assertEqual(1, len(row["transcripts"]))
        self.assertEqual([], next(r for r in result["projects"] if r["project"] == self.legacy.name)["transcripts"])

    def test_missing_directory_and_disabled_scan(self) -> None:
        self.assertEqual([], transcript_signals(self.layered.name, self.claude / "missing"))
        result = retro_batch(self.root, claude_dir=self.claude / "missing")
        self.assertEqual([], result["projects"][0]["transcripts"])
        result = retro_batch(self.root, transcripts=False, claude_dir=self.claude)
        self.assertNotIn("transcripts", result["projects"][0])


class CliTests(_Root):
    def test_the_subcommand_prints_the_batch(self) -> None:
        code, out = _manage(["retro", "batch", self.root, "--no-transcripts", "--since", "2026-10-10", "--limit", "1"])
        self.assertEqual(0, code)
        result = json.loads(out)
        self.assertEqual(1, len(result["projects"]))
        self.assertEqual(1, result["next_offset"])
        code, out = _manage(["retro", "batch", self.root, "--claude-dir", self.claude, "--project", self.layered.name])
        self.assertEqual([], json.loads(out)["projects"][0]["transcripts"])
        self.assertEqual(1, _manage(["retro", "batch", self.root, "--since", "not-a-date"])[0])


class InstantTests(unittest.TestCase):
    def test_zones(self) -> None:
        self.assertEqual("+00:00", parse_instant("2026-10-15T10:00:00Z").isoformat()[-6:])
        self.assertIsNotNone(parse_instant("2026-10-15").tzinfo)
        self.assertEqual(0, parse_instant("2026-10-15").hour)
