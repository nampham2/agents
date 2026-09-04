"""Tests for the briefing.md section check.

The briefing step records the user's stated requirements and the facts the agent checked against
them, so that a later session inherits the evidence and not just the conclusion. Nothing enforced it
before, and validation ignores files it has not been taught about, so a briefing could be absent or
be a bare skeleton and no check would notice.

The check warns and never errors: `briefing.md` postdates every project already in a workspace, so
requiring it would retroactively invalidate history and break reopening a closed project. An empty
body warns as well as a missing heading, because the skeleton `init` writes already carries every
heading, and a structured file that says nothing is the failure the step exists to prevent.
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
    BRIEFING_CANONICAL_SECTIONS,
    BRIEFING_PLACEHOLDER_PREFIX,
    BRIEFING_SECTION_PROMPTS,
    allocate_project,
    briefing_section_warnings,
    validate_v3_state,
)

CANONICAL_BRIEFING = "# Title — briefing\n\n" + "".join(
    f"## {canonical}\n\nWritten.\n\n" for canonical, _ in BRIEFING_CANONICAL_SECTIONS
)

SKELETON_BRIEFING = "# Title — briefing\n\n" + "".join(
    f"## {canonical}\n\n{BRIEFING_PLACEHOLDER_PREFIX}: what belongs here._\n\n"
    for canonical, _ in BRIEFING_CANONICAL_SECTIONS
)


class BriefingSectionWarningTests(unittest.TestCase):
    def test_a_written_briefing_warns_about_nothing(self) -> None:
        self.assertEqual(briefing_section_warnings(CANONICAL_BRIEFING), [])

    def test_an_untouched_skeleton_warns_about_every_section(self) -> None:
        warnings = briefing_section_warnings(SKELETON_BRIEFING)
        self.assertEqual(len(warnings), len(BRIEFING_CANONICAL_SECTIONS), warnings)
        for canonical, _ in BRIEFING_CANONICAL_SECTIONS:
            self.assertTrue(
                any(canonical in warning and "unwritten" in warning for warning in warnings),
                f"{canonical} not reported as unwritten: {warnings}",
            )

    def test_a_missing_section_is_named_as_missing_not_unwritten(self) -> None:
        briefing = CANONICAL_BRIEFING.replace("## Background\n\nWritten.\n\n", "")
        warnings = briefing_section_warnings(briefing)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Background", warnings[0])
        self.assertIn("no '## Background' section", warnings[0])

    def test_a_present_but_empty_section_is_named_as_unwritten(self) -> None:
        briefing = CANONICAL_BRIEFING.replace("## Verified facts\n\nWritten.\n\n", "## Verified facts\n\n")
        warnings = briefing_section_warnings(briefing)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Verified facts", warnings[0])
        self.assertIn("unwritten", warnings[0])

    def test_a_partly_written_briefing_reports_only_what_is_unwritten(self) -> None:
        briefing = SKELETON_BRIEFING.replace(
            f"## Stated requirements\n\n{BRIEFING_PLACEHOLDER_PREFIX}: what belongs here._",
            "## Stated requirements\n\nThe user asked for a briefing step.",
        )
        warnings = briefing_section_warnings(briefing)
        self.assertEqual(len(warnings), len(BRIEFING_CANONICAL_SECTIONS) - 1, warnings)
        self.assertNotIn("Stated requirements", " ".join(warnings))

    def test_a_placeholder_left_beside_real_content_does_not_warn(self) -> None:
        # Real content is what matters; leaving the prompt line above it is untidy, not unwritten.
        briefing = CANONICAL_BRIEFING.replace(
            "## Background\n\nWritten.",
            f"## Background\n\n{BRIEFING_PLACEHOLDER_PREFIX}: how it works today._\n\nIt works like this.",
        )
        self.assertEqual(briefing_section_warnings(briefing), [])

    def test_reworded_headings_still_satisfy_the_contract(self) -> None:
        briefing = (
            "# Title — briefing\n\n"
            "## Requirements\n\nx\n\n"
            "## Facts checked\n\nx\n\n"
            "## Assumptions corrected\n\nx\n\n"
            "## Context\n\nx\n\n"
            "## Open questions\n\nx\n"
        )
        self.assertEqual(briefing_section_warnings(briefing), [])

    def test_a_document_with_no_level_two_headings_does_not_raise(self) -> None:
        warnings = briefing_section_warnings("# Title\n\nProse only, no sections.\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("no '##' sections", warnings[0])

    def test_an_empty_document_does_not_raise(self) -> None:
        warnings = briefing_section_warnings("")
        self.assertEqual(len(warnings), 1)
        self.assertIn("no '##' sections", warnings[0])

    def test_deeper_headings_are_not_mistaken_for_sections(self) -> None:
        # `###` must not satisfy the contract, or a spec-shaped document would pass as a briefing.
        briefing = CANONICAL_BRIEFING.replace("## ", "### ")
        warnings = briefing_section_warnings(briefing)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no '##' sections", warnings[0])

    def test_the_last_section_body_is_read_to_the_end_of_the_document(self) -> None:
        briefing = CANONICAL_BRIEFING.rstrip("\n")
        self.assertEqual(briefing_section_warnings(briefing), [])


class BriefingSkeletonTests(unittest.TestCase):
    """`init` writes the contract's headings, so the step's shape is discoverable from the file."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(
            self.workspace, title="Briefing demo", working_directory=self.target
        )
        self.briefing = (self.project_dir / "briefing.md").read_text(encoding="utf-8")

    def test_init_creates_the_briefing_file(self) -> None:
        self.assertTrue((self.project_dir / "briefing.md").is_file())

    def test_the_skeleton_carries_the_project_title(self) -> None:
        self.assertTrue(self.briefing.startswith("# Briefing demo — briefing\n"), self.briefing[:60])

    def test_the_skeleton_carries_every_canonical_heading_in_order(self) -> None:
        found = [line[3:].strip() for line in self.briefing.splitlines() if line.startswith("## ")]
        self.assertEqual(found, [canonical for canonical, _ in BRIEFING_CANONICAL_SECTIONS])

    def test_the_skeleton_satisfies_the_contract_but_is_reported_unwritten(self) -> None:
        warnings = briefing_section_warnings(self.briefing)
        self.assertEqual(len(warnings), len(BRIEFING_CANONICAL_SECTIONS), warnings)
        self.assertNotIn("appears to have no", " ".join(warnings))

    def test_every_canonical_section_has_a_prompt(self) -> None:
        # A heading whose prompt was forgotten would raise a KeyError while rendering, so the
        # mapping and the contract must stay in step.
        self.assertEqual(
            sorted(BRIEFING_SECTION_PROMPTS), sorted(c for c, _ in BRIEFING_CANONICAL_SECTIONS)
        )

    def test_the_existing_skeleton_files_are_still_written(self) -> None:
        for name in ("project.json", "spec.md", "evidence.md"):
            self.assertTrue((self.project_dir / name).is_file(), name)


class BriefingValidationTests(unittest.TestCase):
    """The briefing is warned about, never errored on, and never blocks closure."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace, title="Brief", working_directory=self.target)

    def _state(self, status: str) -> dict[str, Any]:
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state["status"] = status
        return state

    def _briefing_warnings(self, status: str, **kwargs: Any) -> list[str]:
        report = validate_v3_state(self._state(status), self.project_dir, **kwargs)
        self.assertTrue(report.valid, report.errors)
        return [warning for warning in report.warnings if "briefing.md" in warning]

    def test_an_aligning_project_is_not_warned_about_its_skeleton(self) -> None:
        # ALIGNING is where the briefing is being written; warning there would fire on every init.
        self.assertEqual(self._briefing_warnings("ALIGNING"), [])

    def test_a_planning_project_with_a_skeleton_briefing_is_warned(self) -> None:
        warnings = self._briefing_warnings("PLANNING")
        self.assertEqual(len(warnings), len(BRIEFING_CANONICAL_SECTIONS), warnings)

    def test_a_written_briefing_is_not_warned_about(self) -> None:
        (self.project_dir / "briefing.md").write_text(CANONICAL_BRIEFING, encoding="utf-8")
        self.assertEqual(self._briefing_warnings("PLANNING"), [])

    def test_a_missing_briefing_is_silent_rather_than_a_crash_or_an_error(self) -> None:
        # Every project predating the briefing step is in exactly this state.
        (self.project_dir / "briefing.md").unlink()
        self.assertEqual(self._briefing_warnings("PLANNING"), [])

    def test_the_check_is_skipped_when_files_are_not_being_read(self) -> None:
        self.assertEqual(self._briefing_warnings("PLANNING", check_files=False), [])

    def test_a_skeleton_briefing_does_not_block_closure(self) -> None:
        state = self._state("DONE")
        state["tasks"] = [
            {
                "id": "T01",
                "name": "Work",
                "status": "DONE",
                "depends_on": [],
                "outputs": [],
                "success_criteria": "Done",
                "verification": "Checked",
                "evidence": [{"root": "workspace", "path": "evidence.md", "anchor": None}],
                "effect": {"kind": "none", "description": None},
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
        ]
        (self.project_dir / "reflection.md").write_text("# Reflection\n\nWent fine.\n", encoding="utf-8")
        report = validate_v3_state(state, self.project_dir, close=True)
        self.assertTrue(report.valid, report.errors)
        self.assertEqual([error for error in report.errors if "briefing" in error], [])
        self.assertTrue([warning for warning in report.warnings if "briefing.md" in warning])

    def test_an_unreadable_briefing_is_reported_rather_than_raised(self) -> None:
        (self.project_dir / "briefing.md").write_bytes(b"# T\n\n## Stated requirements\n\n\xff\xfe\n")
        with self.assertRaises(Exception) as caught:
            validate_v3_state(self._state("PLANNING"), self.project_dir)
        self.assertIn("briefing.md", str(caught.exception))

    def test_the_warning_reaches_the_cli_without_failing_it(self) -> None:
        state = self._state("PLANNING")
        (self.project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        with patch.object(sys, "argv", ["validate", str(self.project_dir)]):
            self.assertEqual(validate_workspace.main(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
