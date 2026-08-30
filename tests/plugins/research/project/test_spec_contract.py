"""Tests for the spec.md section check.

The skill states what `## Current specification` must contain, and nothing checked it, so a project
could leave `ALIGNING` with no recorded constraints and no authorization states at all. The check
warns rather than errors on purpose: three closed projects word these headings three different ways,
and history that was correct when written must not become invalid.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import validate_workspace
from workspace_lib import (
    SPEC_CANONICAL_SECTIONS,
    allocate_project,
    spec_section_warnings,
    validate_v3_state,
)

CANONICAL_SPEC = (
    "# Title\n\n## Current specification\n\n"
    + "".join(f"### {canonical}\n\nSettled.\n\n" for canonical, _ in SPEC_CANONICAL_SECTIONS)
    + "## Decision history\n\n- Decided.\n"
)

# The wordings actually used by projects closed before the canonical set existed.
LEGACY_SPEC = (
    "# Title\n\n## Current specification\n\n"
    "### Objective and audience\n\nx\n\n"
    "### Scope\n\nx\n\n"
    "### Constraints and assumptions\n\nx\n\n"
    "### Deliverables and authorization\n\nx\n\n"
    "### Success and verification criteria\n\nx\n\n"
    "## Decision history\n\n- Decided.\n"
)


class SpecSectionWarningTests(unittest.TestCase):
    def test_a_canonical_spec_warns_about_nothing(self) -> None:
        self.assertEqual(spec_section_warnings(CANONICAL_SPEC), [])

    def test_the_older_heading_styles_still_warn_about_nothing(self) -> None:
        self.assertEqual(spec_section_warnings(LEGACY_SPEC), [])

    def test_each_missing_section_is_named(self) -> None:
        spec = CANONICAL_SPEC.replace("### Constraints and important assumptions\n\nSettled.\n\n", "")
        warnings = spec_section_warnings(spec)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Constraints and important assumptions", warnings[0])

    def test_several_missing_sections_are_all_named(self) -> None:
        spec = "# Title\n\n## Current specification\n\n### Objective and audience\n\nx\n"
        warnings = spec_section_warnings(spec)
        self.assertEqual(len(warnings), len(SPEC_CANONICAL_SECTIONS) - 1)
        self.assertNotIn("Objective", " ".join(warnings))

    def test_a_spec_without_the_current_specification_heading_does_not_raise(self) -> None:
        warnings = spec_section_warnings("# Title\n\nSome prose and no headings at all.\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("no '## Current specification'", warnings[0])

    def test_a_specification_with_no_subsections_does_not_raise(self) -> None:
        warnings = spec_section_warnings("# T\n\n## Current specification\n\nProse only.\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("no '###' sections", warnings[0])

    def test_an_empty_document_does_not_raise(self) -> None:
        self.assertEqual(len(spec_section_warnings("")), 1)

    def test_deeper_headings_count_as_sections(self) -> None:
        # A spec that nests its sections under an extra level is still covering them.
        spec = CANONICAL_SPEC.replace("### ", "#### ")
        self.assertEqual(spec_section_warnings(spec), [])


class SpecSectionValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace, title="Spec", working_directory=self.target)

    def _state(self, status: str) -> dict[str, Any]:
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state["status"] = status
        return state

    def test_an_aligning_project_is_not_warned_about_its_skeleton(self) -> None:
        report = validate_v3_state(self._state("ALIGNING"), self.project_dir)
        self.assertEqual([w for w in report.warnings if "Current specification" in w], [])

    def test_a_planning_project_with_a_skeleton_spec_is_warned(self) -> None:
        report = validate_v3_state(self._state("PLANNING"), self.project_dir)
        self.assertTrue([w for w in report.warnings if "Current specification" in w])
        self.assertTrue(report.valid, report.errors)

    def test_a_planning_project_with_a_complete_spec_is_not_warned(self) -> None:
        (self.project_dir / "spec.md").write_text(CANONICAL_SPEC, encoding="utf-8")
        report = validate_v3_state(self._state("PLANNING"), self.project_dir)
        self.assertEqual([w for w in report.warnings if "Current specification" in w], [])

    def test_a_missing_spec_file_is_not_a_crash(self) -> None:
        (self.project_dir / "spec.md").unlink()
        report = validate_v3_state(self._state("PLANNING"), self.project_dir)
        self.assertEqual([w for w in report.warnings if "Current specification" in w], [])

    def test_the_check_is_skipped_when_files_are_not_being_read(self) -> None:
        report = validate_v3_state(self._state("PLANNING"), self.project_dir, check_files=False)
        self.assertEqual([w for w in report.warnings if "Current specification" in w], [])

    def test_the_warning_reaches_the_cli_without_failing_it(self) -> None:
        with patch.object(sys, "argv", ["validate", str(self.project_dir)]):
            self.assertEqual(validate_workspace.main(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
