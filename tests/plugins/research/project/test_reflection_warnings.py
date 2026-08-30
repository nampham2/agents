"""Tests for the cross-project reflection warnings.

Advisory memory is only useful if it is read, and the file grows by appending. Two conditions are
worth surfacing where sessions already look: a file long enough to skim, and an entry whose source
project is no longer in the workspace, so the evidence behind it can no longer be checked.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from workspace_lib import (
    REFLECTION_MAX_ENTRIES,
    allocate_project,
    reflection_warnings,
    validate_project,
)


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
        self.assertEqual(len([w for w in report.warnings if "reflection.md" in w]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
