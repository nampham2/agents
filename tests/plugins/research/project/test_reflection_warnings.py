"""Tests for the cross-project reflection warnings.

Advisory memory is only useful if it is read, and the file grows by appending. Two conditions are
worth surfacing where sessions already look: a file long enough to skim, and an entry whose source
project is no longer in the workspace, so the evidence behind it can no longer be checked.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from workspace_lib import (
    REFLECTION_MAX_ENTRIES,
    allocate_project,
    reflection_entries,
    reflection_warnings,
    validate_project,
)


def _dated(project_id: str | None, index: int) -> str:
    """One entry in the dated-section form, with provenance where that form puts it."""
    source = f"Source: `{project_id}` (cycle 1).\n" if project_id else ""
    return f"## 2026-09-0{index % 9 + 1} — Lesson {index}\n\n{source}Scope: testing.\n\nProse.\n\n"


def _entry(project_id: str, index: int) -> str:
    return f"- [2026-08-30 | source: {project_id} | scope: testing] Lesson {index}.\n"


class ReflectionWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "ws"
        self.workspace.mkdir()
        (self.workspace / "2026-08-30-001").mkdir()

    def _write(self, entries: str) -> None:
        (self.workspace / "reflection.md").write_text(
            f"# Cross-project reflection\n\n## Lessons\n\n{entries}", encoding="utf-8"
        )

    def test_a_missing_reflection_file_warns_about_nothing(self) -> None:
        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_a_short_reflection_with_present_sources_warns_about_nothing(self) -> None:
        self._write("".join(_entry("2026-08-30-001", i) for i in range(3)))
        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_the_threshold_is_a_ceiling_not_a_target(self) -> None:
        self._write("".join(_entry("2026-08-30-001", i) for i in range(REFLECTION_MAX_ENTRIES)))
        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_one_entry_past_the_threshold_warns(self) -> None:
        self._write("".join(_entry("2026-08-30-001", i) for i in range(REFLECTION_MAX_ENTRIES + 1)))
        warnings = reflection_warnings(self.workspace)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"{REFLECTION_MAX_ENTRIES + 1} entries", warnings[0])

    def test_a_source_project_that_is_gone_is_named(self) -> None:
        self._write(_entry("2026-08-30-001", 0) + _entry("2026-01-01-009", 1))
        warnings = reflection_warnings(self.workspace)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-01-01-009", warnings[0])
        self.assertNotIn("2026-08-30-001", warnings[0])

    def test_both_conditions_warn_independently(self) -> None:
        entries = "".join(_entry("2026-08-30-001", i) for i in range(REFLECTION_MAX_ENTRIES + 1))
        self._write(entries + _entry("2026-01-01-009", 99))
        self.assertEqual(len(reflection_warnings(self.workspace)), 2)

    def test_an_entry_citing_several_sources_checks_each_one(self) -> None:
        self._write("- [2026-08-30 | source: 2026-08-30-001, 2026-01-01-009 | scope: x] Lesson.\n")
        warnings = reflection_warnings(self.workspace)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-01-01-009", warnings[0])

    def test_prose_outside_an_entry_is_not_counted(self) -> None:
        # Only bracketed entries count; the file's own preamble and headings are not memory.
        self._write("Some prose mentioning 2026-01-01-009 without being an entry.\n")
        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_an_unreadable_reflection_does_not_fail_validation(self) -> None:
        path = self.workspace / "reflection.md"
        self._write(_entry("2026-08-30-001", 0))
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_an_unlistable_workspace_still_reports_the_scale_warning(self) -> None:
        self._write("".join(_entry("2026-08-30-001", i) for i in range(REFLECTION_MAX_ENTRIES + 1)))
        self.workspace.chmod(0o500)
        self.addCleanup(self.workspace.chmod, 0o700)
        with unittest.mock.patch.object(Path, "iterdir", side_effect=OSError("no listing")):
            warnings = reflection_warnings(self.workspace)
        self.assertEqual(len(warnings), 1)
        self.assertIn("entries", warnings[0])


class DatedSectionEntryTests(unittest.TestCase):
    """The dated `## YYYY-MM-DD` section is the other entry shape, and it was invisible.

    The real cross-project reflection held 47 entries written in both forms while the counter saw 20,
    so the threshold it was compared against could not be reached however long the file grew. Both
    shapes now count, and both are read for provenance where each form records it.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "ws"
        self.workspace.mkdir()
        (self.workspace / "2026-08-30-001").mkdir()

    def _write(self, body: str) -> None:
        (self.workspace / "reflection.md").write_text(f"# Cross-project reflection\n\n{body}", encoding="utf-8")

    def test_dated_sections_alone_can_cross_the_threshold(self) -> None:
        self._write("".join(_dated("2026-08-30-001", i) for i in range(REFLECTION_MAX_ENTRIES + 1)))

        warnings = reflection_warnings(self.workspace)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"{REFLECTION_MAX_ENTRIES + 1} entries", warnings[0])

    def test_the_two_shapes_are_counted_together(self) -> None:
        # Neither half reaches the threshold; the file does. This is the shape of the real file.
        bullets = "".join(_entry("2026-08-30-001", i) for i in range(12))
        dated = "".join(_dated("2026-08-30-001", i) for i in range(12))
        self._write(f"## Lessons\n\n{bullets}\n{dated}")

        warnings = reflection_warnings(self.workspace)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("24 entries", warnings[0])

    def test_an_undated_heading_is_not_an_entry(self) -> None:
        # The file's own structural headings are not memory, however many of them there are.
        self._write("".join(f"## Section {i}\n\nProse.\n\n" for i in range(REFLECTION_MAX_ENTRIES + 1)))

        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_a_dated_section_citing_a_missing_project_is_named(self) -> None:
        self._write(_dated("2026-08-30-001", 0) + _dated("2026-01-01-009", 1))

        warnings = reflection_warnings(self.workspace)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("2026-01-01-009", warnings[0])
        self.assertNotIn("2026-08-30-001", warnings[0])

    def test_a_project_id_in_dated_prose_is_not_read_as_a_source(self) -> None:
        # The same reading the bulleted form has always had: a lesson that mentions a project in
        # passing is not claiming it as the evidence behind the lesson.
        self._write("## 2026-09-01 — Lesson\n\nProse mentioning 2026-01-01-009 in passing.\n")

        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_a_source_under_a_deeper_subheading_belongs_to_its_section(self) -> None:
        self._write("## 2026-09-01 — L\n\nProse.\n\n### Detail\n\nSource: `2026-01-01-009` (cycle 2).\n")

        warnings = reflection_warnings(self.workspace)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("2026-01-01-009", warnings[0])

    def test_a_source_after_the_last_dated_section_is_not_attributed_to_it(self) -> None:
        # The boundary is a heading of the same or shallower level, so an appendix is outside.
        self._write("## 2026-09-01 — L\n\nProse.\n\n## Appendix\n\nSource: `2026-01-01-009` (cycle 2).\n")

        self.assertEqual(reflection_warnings(self.workspace), [])

    def test_entries_are_returned_in_document_order(self) -> None:
        content = _entry("2026-08-30-001", 0) + _dated("2026-01-01-009", 1) + _entry("2026-01-02-003", 2)

        metas = reflection_entries(content)

        self.assertEqual(len(metas), 3)
        self.assertIn("2026-08-30-001", metas[0])
        self.assertIn("2026-01-01-009", metas[1])
        self.assertIn("2026-01-02-003", metas[2])

    def test_a_dated_section_with_no_source_still_counts_as_an_entry(self) -> None:
        self._write("".join(_dated(None, i) for i in range(REFLECTION_MAX_ENTRIES + 1)))

        warnings = reflection_warnings(self.workspace)

        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"{REFLECTION_MAX_ENTRIES + 1} entries", warnings[0])


@unittest.skipUnless(
    os.environ.get("RESEARCH_WORKSPACE") and Path(os.environ.get("RESEARCH_WORKSPACE", ""), "reflection.md").is_file(),
    "no real workspace reflection to check against",
)
class RealWorkspaceReflectionTests(unittest.TestCase):
    """The counter has to be right about the file it exists for, not only about fixtures.

    A fixture proves the code does what the fixture was written to show. This reads the workspace's
    own `reflection.md` — the document whose entries went uncounted — and recounts it independently
    rather than asserting a number that would go stale on the next append.
    """

    def setUp(self) -> None:
        self.workspace = Path(os.environ["RESEARCH_WORKSPACE"])
        self.content = (self.workspace / "reflection.md").read_text(encoding="utf-8")
        self.bullets = len(re.findall(r"^- \[", self.content, re.MULTILINE))
        self.dated = len(re.findall(r"^#{1,6} \d{4}-\d{2}-\d{2}", self.content, re.MULTILINE))

    def test_the_real_file_is_written_in_both_shapes(self) -> None:
        # Without this, the count below could agree while one whole shape went unexercised.
        self.assertGreater(self.bullets, 0)
        self.assertGreater(self.dated, 0)

    def test_every_entry_in_the_real_file_is_counted(self) -> None:
        counted = len(reflection_entries(self.content))

        self.assertEqual(counted, self.bullets + self.dated)
        self.assertGreater(counted, self.bullets, "the dated sections are still invisible")

    def test_the_real_file_is_reported_as_past_the_threshold(self) -> None:
        if self.bullets + self.dated <= REFLECTION_MAX_ENTRIES:
            self.skipTest("the real file has been consolidated below the threshold")

        scale = [warning for warning in reflection_warnings(self.workspace) if "entries" in warning]

        self.assertEqual(len(scale), 1, scale)
        self.assertIn(f"{self.bullets + self.dated} entries", scale[0])


class ReflectionWarningsReachValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace, title="R", working_directory=self.target)

    def test_validating_a_project_surfaces_its_workspace_reflection_warnings(self) -> None:
        (self.workspace / "reflection.md").write_text(
            "# Cross-project reflection\n\n"
            + "".join(_entry("2026-01-01-009", i) for i in range(REFLECTION_MAX_ENTRIES + 1)),
            encoding="utf-8",
        )
        report = validate_project(self.project_dir)
        self.assertTrue(report.valid, report.errors)
        warnings = [warning for warning in report.warnings if "reflection.md" in warning]
        # Three, not two. The scale and dangling-citation warnings are joined by the one saying the
        # flat file is now the legacy layout — which is the point of the memory layer replacing it,
        # so a workspace that still has the file should say so on every validation until it does not.
        self.assertEqual(len(warnings), 3, warnings)
        self.assertEqual(len([w for w in warnings if "holds 21 entries" in w]), 1, warnings)
        self.assertEqual(len([w for w in warnings if "cites source projects" in w]), 1, warnings)
        self.assertEqual(len([w for w in warnings if "legacy flat cross-project" in w]), 1, warnings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
