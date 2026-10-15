"""The retro section gate: a reflection must carry `## Retro` before a project created after the cutoff closes.

Five of ninety-four reflections mentioned friction with the skill before this gate; the rest were
written to a shape of their author's choosing. The gate checks shape only: a level-two heading named
Retro with at least a short paragraph under it. Older projects get a warning and close as before.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest
import workspace_lib
from workspace_lib import (
    RETRO_SECTION_FROM,
    RETRO_SECTION_MIN_CHARS,
    RETRO_SECTION_PLACEHOLDER,
    WorkspaceError,
    allocate_project,
    retro_section_findings,
    validate_project,
)
from workspace_workflows import workflow

from tests.conftest import backdate_project

PAST = "2000-01-01T00:00:00+00:00"
OUTCOME = "# Reflection\n\n## Outcome\n\n" + "Delivered as agreed. " * 12
GOOD = OUTCOME + "\n## Retro\n\n" + "The worktree reference sent me to a step the tool refused. " * 3
SHORT = OUTCOME + "\n## Retro\n\nnone\n"
MISSING = OUTCOME


class FindingsTests(unittest.TestCase):
    def test_shape_only(self) -> None:
        self.assertEqual([], retro_section_findings(GOOD))
        (finding,) = retro_section_findings(MISSING)
        self.assertIn("## Retro", finding)
        self.assertIn("retro", finding.lower())
        (finding,) = retro_section_findings(SHORT)
        self.assertIn(str(RETRO_SECTION_MIN_CHARS), finding)
        self.assertEqual(1, len(retro_section_findings("")))
        # A `### Retro` under another section is not the section.
        self.assertEqual(1, len(retro_section_findings("# R\n\n## Outcome\n\n### Retro\n\n" + "x " * 100)))

    def test_the_shipped_cutoff_is_the_placeholder_until_the_release(self) -> None:
        # The release task replaces it; until then the gate is off for every real project.
        self.assertEqual(RETRO_SECTION_PLACEHOLDER, RETRO_SECTION_FROM)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "ws").mkdir()
        (root / "target").mkdir()
        self.project = allocate_project(root / "ws", title="Gated", working_directory=root / "target")


@pytest.mark.usefixtures("retro_cutoff_in_the_past")
class GatedProjectTests(_Fixture):
    def test_finalize_refuses_and_commits_nothing(self) -> None:
        for reflection in (MISSING, SHORT):
            with self.assertRaises(WorkspaceError) as caught:
                workflow(self.project, "finalize", {"id": "f1", "expected_revision": 0, "reflection": reflection})
            self.assertIn("## Retro", str(caught.exception))
        self.assertFalse((self.project / "reflection.md").exists())
        # With the section present the retro check is passed: whatever refuses next is another gate.
        with self.assertRaises(WorkspaceError) as caught:
            workflow(self.project, "finalize", {"id": "f2", "expected_revision": 0, "reflection": GOOD})
        self.assertNotIn("## Retro", str(caught.exception))

    def test_validation_and_readiness_report_it_as_an_error(self) -> None:
        (self.project / "reflection.md").write_text(MISSING, encoding="utf-8")
        report = validate_project(self.project, close=True)
        self.assertTrue(any("## Retro" in finding for finding in report.errors))
        groups = workflow(self.project, "readiness", {})["groups"]
        self.assertIn("retro", groups)
        self.assertEqual("error", groups["retro"][0]["level"])
        (self.project / "reflection.md").write_text(GOOD, encoding="utf-8")
        report = validate_project(self.project, close=True)
        self.assertFalse(any("## Retro" in finding for finding in report.errors + report.warnings))


class LegacyProjectTests(_Fixture):
    def test_a_project_before_the_cutoff_gets_a_warning(self) -> None:
        backdate_project(self.project)
        (self.project / "reflection.md").write_text(MISSING, encoding="utf-8")
        report = validate_project(self.project, close=True)
        self.assertTrue(any("## Retro" in finding for finding in report.warnings))
        self.assertFalse(any("## Retro" in finding for finding in report.errors))
        self.assertEqual("warning", workflow(self.project, "readiness", {})["groups"]["retro"][0]["level"])


@pytest.fixture
def retro_cutoff_in_the_past(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "RETRO_SECTION_FROM", PAST)
