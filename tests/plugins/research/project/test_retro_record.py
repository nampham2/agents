"""`retro record`: one run into `retro/<id>.md`, the index refreshed, the next batch moved on.

The ledger is what makes the retro iterative. These tests pin the record's validation, the file and
index shapes, idempotent retries, the conflict on reuse of an id, the warning on a repeated rejected
candidate, and that the next `retro batch` starts after the run.
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
from workspace_lib import WorkspaceConflict, WorkspaceError, allocate_project
from workspace_retro import RETRO_BLOCK, RETRO_DIRECTORY, RETRO_INDEX_FILENAME, record_run, record_schema, retro_batch


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


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "workspace"
        self.root.mkdir()
        target = base / "target"
        target.mkdir()
        self.a = allocate_project(self.root, title="First", working_directory=target)
        self.b = allocate_project(self.root, title="Second", working_directory=target)
        self.c = allocate_project(self.root, title="Open", working_directory=target)
        _set(self.a, status="DONE", updated="2026-10-10T10:00:00+02:00")
        _set(self.b, status="CANCELLED", updated="2026-10-12T10:00:00+02:00")

    def request(self, **overrides: object) -> dict[str, Any]:
        request: dict[str, Any] = {
            "id": "2026-10-15-001",
            "source": "user reply 2026-10-15 via question tool",
            "batch": [self.a.name, self.b.name],
            "candidates": [
                {"id": "c1", "title": "Name the missing heading", "category": "gates", "severity": "high",
                 "proposal": "Return the heading in the error.", "disposition": "accepted", "response": "yes",
                 "evidence": [{"project": self.a.name, "record": "invocations.jsonl", "anchor": "2026-10-10T09:00"}]},
                {"id": "c2", "title": "Shorter resume", "category": "tool-economy", "severity": "low",
                 "proposal": "Drop the task list.", "disposition": "rejected", "response": "no, I read it",
                 "evidence": [{"project": self.b.name, "record": "handoff.md"}]},
                {"id": "c3", "title": "Explain the token", "category": "instructions", "severity": "medium",
                 "proposal": "One sentence in commands.md.", "disposition": "deferred", "response": "later",
                 "evidence": [{"project": self.a.name, "record": "friction.md", "anchor": "entry-1"}]},
            ],
            "notes": "First run.",
        }
        request.update(overrides)
        return request


class RecordTests(_Root):
    def test_a_run_is_written_indexed_and_idempotent(self) -> None:
        result = record_run(self.root, self.request())
        self.assertEqual("retro.record", result["operation"])
        self.assertEqual(("2026-10-15-001", False), (result["id"], result["existing"]))
        self.assertEqual("2026-10-12T10:00:00+02:00", result["through"])
        self.assertEqual({"accepted": 1, "rejected": 1, "deferred": 1}, result["counts"])
        self.assertEqual([], result["warnings"])
        path = self.root / RETRO_DIRECTORY / "2026-10-15-001.md"
        text = path.read_text(encoding="utf-8")
        match = RETRO_BLOCK.search(text)
        assert match is not None
        block = json.loads(match.group("json"))
        self.assertEqual([self.a.name, self.b.name], block["batch"])
        self.assertEqual(3, len(block["candidates"]))
        self.assertEqual(64, len(block["signature"]))
        for heading in ("## Batch", "## Candidates", "### High", "### Medium", "### Low", "## Next cursor", "## Notes"):
            self.assertIn(heading, text)
        self.assertIn(f"{self.a.name}/invocations.jsonl#2026-10-10T09:00", text)
        self.assertIn("First (DONE)", text)
        index = (self.root / RETRO_INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("[2026-10-15-001](retro/2026-10-15-001.md)", index)
        self.assertIn("2 project(s) through 2026-10-12T10:00:00+02:00: 1 accepted, 1 rejected, 1 deferred", index)
        again = record_run(self.root, self.request())
        self.assertTrue(again["existing"])
        with self.assertRaises(WorkspaceConflict):
            record_run(self.root, self.request(notes="Changed."))
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_the_next_batch_starts_after_the_run_and_carries_the_dispositions(self) -> None:
        record_run(self.root, self.request())
        result = retro_batch(self.root, transcripts=False)
        self.assertEqual([], result["projects"])
        self.assertEqual([self.c.name], result["pending"])
        self.assertEqual("2026-10-12T10:00:00+02:00", result["previous"]["through"])
        self.assertEqual(["Shorter resume"], [item["title"] for item in result["previous"]["rejected"]])
        self.assertEqual(["Explain the token"], [item["title"] for item in result["previous"]["deferred"]])
        _set(self.c, status="DONE", updated="2026-10-14T10:00:00+02:00")
        rows = retro_batch(self.root, transcripts=False)["projects"]
        self.assertEqual([self.c.name], [row["project"] for row in rows])

    def test_a_repeated_rejected_candidate_is_written_with_a_warning(self) -> None:
        record_run(self.root, self.request())
        _set(self.c, status="DONE", updated="2026-10-14T10:00:00+02:00")
        repeat = {"id": "r1", "title": "shorter RESUME", "category": "tool-economy", "severity": "low",
                  "proposal": "again", "disposition": "accepted", "response": "fine now",
                  "evidence": [{"project": self.c.name, "record": "handoff.md"}]}
        result = record_run(self.root, {"id": "run-2", "source": "s", "batch": [self.c.name], "candidates": [repeat]})
        self.assertEqual(1, len(result["warnings"]))
        self.assertIn("repeats a rejected one", result["warnings"][0])
        self.assertTrue((self.root / RETRO_DIRECTORY / "run-2.md").exists())
        index = (self.root / RETRO_INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertEqual(2, index.count("](retro/"))

    def test_an_empty_candidate_list_is_a_run_with_no_finding(self) -> None:
        result = record_run(self.root, self.request(candidates=[], notes=""))
        self.assertEqual({"accepted": 0, "rejected": 0, "deferred": 0}, result["counts"])
        text = (self.root / RETRO_DIRECTORY / "2026-10-15-001.md").read_text(encoding="utf-8")
        self.assertIn("No candidate", text)
        self.assertNotIn("## Notes", text)

    def test_refusals_write_nothing(self) -> None:
        good = self.request()
        candidate = dict(good["candidates"][0])
        bad_requests: list[tuple[str, dict[str, Any]]] = [
            ("extra key", self.request(extra=1)),
            ("missing source", {k: v for k, v in good.items() if k != "source"}),
            ("bad id", self.request(id="Bad Id")),
            ("empty batch", self.request(batch=[])),
            ("unknown project", self.request(batch=["2099-01-01-001"])),
            ("open project", self.request(batch=[self.c.name])),
            ("candidates not a list", self.request(candidates={})),
            ("candidate not an object", self.request(candidates=["x"])),
            ("candidate missing field",
             self.request(candidates=[{k: v for k, v in candidate.items() if k != "proposal"}])),
            ("candidate extra field", self.request(candidates=[{**candidate, "extra": 1}])),
            ("bad category", self.request(candidates=[{**candidate, "category": "style"}])),
            ("bad severity", self.request(candidates=[{**candidate, "severity": "urgent"}])),
            ("bad disposition", self.request(candidates=[{**candidate, "disposition": "maybe"}])),
            ("empty response", self.request(candidates=[{**candidate, "response": " "}])),
            ("long title", self.request(candidates=[{**candidate, "title": "t" * 121}])),
            ("no evidence", self.request(candidates=[{**candidate, "evidence": []}])),
            ("evidence shape", self.request(candidates=[{**candidate, "evidence": [{"record": "x"}]}])),
            ("evidence project",
             self.request(candidates=[{**candidate, "evidence": [{"project": "2099-01-01-001", "record": "x"}]}])),
            ("duplicate candidate id", self.request(candidates=[candidate, candidate])),
        ]
        for label, request in bad_requests:
            with self.subTest(label), self.assertRaises(WorkspaceError):
                record_run(self.root, request)
        (self.a / "project.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            record_run(self.root, good)
        self.assertFalse((self.root / RETRO_DIRECTORY).exists() and any((self.root / RETRO_DIRECTORY).iterdir()))
        self.assertFalse((self.root / RETRO_INDEX_FILENAME).exists())

    def test_schema_names_the_vocabularies(self) -> None:
        schema = record_schema()
        self.assertIn("gates", schema["candidate"]["category"])
        self.assertEqual(["accepted", "rejected", "deferred"], schema["candidate"]["disposition"])


class CliTests(_Root):
    def test_record_through_stdin_and_schema(self) -> None:
        code, out = _manage(["retro", "record", self.root, "-"], stdin=json.dumps(self.request()))
        self.assertEqual(0, code)
        self.assertEqual("2026-10-15-001", json.loads(out)["id"])
        code, out = _manage(["retro", "record", "--schema"])
        self.assertEqual(0, code)
        self.assertIn("allowed", json.loads(out))
        self.assertEqual(1, _manage(["retro", "record", self.root])[0])
        self.assertEqual(1, _manage(["retro", "record", self.root, "-"], stdin=json.dumps(self.request(id="x y")))[0])
