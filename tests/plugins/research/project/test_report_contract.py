"""Tests for the closing report: its section contract, its trigger, and its mechanical check.

The step exists because a finished project left its findings scattered across `evidence.md`, a task
plan, and a reflection, with no single document a reader could open. Two files carry it — Markdown
for the terminal and HTML for presentation — authored rather than derived, so the HTML can hold
charts that no Markdown-to-HTML conversion would produce.

Two properties are worth stating up front, because they are what these tests mostly pin.

The close-time warning is a warning and never an error, on the same reasoning as `briefing.md`: the
report postdates every project already in a workspace. Its *trigger* differs though. A briefing is
checked from the moment a project leaves `ALIGNING`, because that is when it is written; a report
cannot exist before the work it reports on, so it is checked at close and at no earlier status. Both
directions of that rule are tested, because a check that fires too early is as wrong as one that
never fires.

The `--report` check is the opposite: errors, not warnings, because the step runs it through
`record-evidence` so its exit code becomes a record rather than a claim. Every property it asserts
has a fixture below that makes it fail. An assertion whose failing state is unreachable proves
nothing, and one of these fixtures — a `<figcaption>` holding a single space — caught exactly that:
the first version of the caption check accepted a blank caption, because the `<` of the closing tag
satisfied its "some non-whitespace follows" test.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import validate_workspace
import workspace_lib
from workspace_lib import (
    CLOSURE_STEPS,
    REPORT_CANONICAL_SECTIONS,
    REPORT_DIRECTORY,
    REPORT_GRAPH_PARENT,
    REPORT_GRAPH_SUBSECTION,
    REPORT_HTML_FILENAME,
    REPORT_MARKDOWN_FILENAME,
    REPORT_THEME_BLOCKS,
    WorkspaceError,
    allocate_project,
    report_findings,
    report_warnings,
    validate_v4_state,
)

from tests.conftest import REPO_ROOT, VALIDATOR

GRAPH_HEADING = REPORT_GRAPH_SUBSECTION[0]

# The subsection is written into the conforming fixtures from the constants rather than spelled out,
# so a change to either name reaches these fixtures instead of leaving them quietly non-conforming.
GOOD_MARKDOWN = "# Title — report\n\n" + "".join(
    f"## {canonical}\n\nWritten.\n\n"
    + (f"### {GRAPH_HEADING}\n\nWritten.\n\n" if canonical == REPORT_GRAPH_PARENT else "")
    for canonical, _ in REPORT_CANONICAL_SECTIONS
)

GOOD_HTML = (
    '<!doctype html>\n<html lang="en"><head><title>Title</title>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n'
    "<style>\n"
    "/* comment */\n"
    ":root { --fg: #111111; --bar: #2563eb; }\n"
    '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg: #eeeeee; } }\n'
    ':root[data-theme="dark"] { --fg: #eeeeee; }\n'
    "body { color: var(--fg); font-family: Georgia, serif; }\n"
    "</style></head><body>\n<h1>Title</h1>\n"
    + "".join(
        f"<h2>{canonical}</h2>\n<p>Written.</p>\n"
        + (f"<h3>{GRAPH_HEADING}</h3>\n<p>Written.</p>\n" if canonical == REPORT_GRAPH_PARENT else "")
        for canonical, _ in REPORT_CANONICAL_SECTIONS
    )
    + '<figure>\n<svg viewBox="0 0 100 50" role="img" aria-label="Bar chart of two runs">\n'
    '<rect x="0" y="0" width="40" height="10" fill="var(--bar)"/>\n</svg>\n'
    "<figcaption>The second run is four times the first.</figcaption>\n</figure>\n"
    "</body></html>\n"
)


def _project(markdown: "str | None" = GOOD_MARKDOWN, html: "str | None" = GOOD_HTML) -> Path:
    """A directory holding whichever of the two report files was asked for."""
    project_dir = Path(tempfile.mkdtemp())
    (project_dir / REPORT_DIRECTORY).mkdir()
    if markdown is not None:
        (project_dir / REPORT_DIRECTORY / REPORT_MARKDOWN_FILENAME).write_text(markdown, encoding="utf-8")
    if html is not None:
        (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(html, encoding="utf-8")
    return project_dir


def _errors(**overrides: "str | None") -> "list[str]":
    return report_findings(_project(**overrides)).errors


class ReportSectionContractTests(unittest.TestCase):
    def test_a_written_pair_warns_about_nothing(self) -> None:
        self.assertEqual(report_warnings(_project()), [])

    def test_an_absent_optional_pair_is_silent(self) -> None:
        self.assertEqual(report_warnings(_project(markdown=None, html=None)), [])

    def test_a_partial_pair_still_names_the_missing_file(self) -> None:
        self.assertEqual(report_warnings(_project(markdown=None)), ["no closing report at artifacts/report.md"])
        self.assertEqual(report_warnings(_project(html=None)), ["no closing report at artifacts/report.html"])

    def test_a_missing_markdown_section_is_named_as_missing(self) -> None:
        warnings = report_warnings(_project(markdown=GOOD_MARKDOWN.replace("## Open work\n\nWritten.\n\n", "")))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no '## Open work' section", warnings[0])

    def test_an_unwritten_section_is_named_as_unwritten(self) -> None:
        markdown = GOOD_MARKDOWN.replace("## Summary\n\nWritten.", "## Summary\n\n_Not yet written: the summary._")
        warnings = report_warnings(_project(markdown=markdown))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("'## Summary' is still unwritten", warnings[0])

    def test_reworded_headings_still_satisfy_the_contract(self) -> None:
        markdown = (
            "## Abstract\n\nx\n\n## Method\n\nx\n\n### Dependency graph\n\nx\n\n"
            "## Results\n\nx\n\n## Caveats\n\nx\n\n## Next steps\n\nx\n"
        )
        self.assertEqual(report_warnings(_project(markdown=markdown)), [])

    def test_a_document_with_no_level_two_headings_says_so_once(self) -> None:
        warnings = report_warnings(_project(markdown="# Title\n\nProse only.\n"))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no '##' sections", warnings[0])

    def test_the_html_is_read_through_its_h2_headings(self) -> None:
        self.assertEqual(report_warnings(_project(html=GOOD_HTML)), [])

    def test_a_demoted_html_heading_does_not_satisfy_the_section_contract(self) -> None:
        # The contract is `##` in Markdown and `<h2>` in HTML. A demoted heading is a missing
        # section, or a report could satisfy the contract with headings nobody scans for. `<h3>` is
        # translated to `###` for the subsection check, which is why this is worth pinning: a
        # subsection heading must not be able to stand in for the section it sits under.
        warnings = report_warnings(_project(html=GOOD_HTML.replace("<h2>Open work</h2>", "<h3>Open work</h3>")))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no '## Open work' section", warnings[0])

    def test_stylesheet_text_cannot_make_a_section_look_written(self) -> None:
        # `<style>` bodies are removed whole. Left in, a stylesheet between two headings would count
        # as the first heading's content.
        html = GOOD_HTML.replace(
            "<h2>Summary</h2>\n<p>Written.</p>",
            "<h2>Summary</h2>\n<style>.x { color: var(--fg); }</style>",
        )
        warnings = report_warnings(_project(html=html))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("'## Summary' is still unwritten", warnings[0])

    def test_an_unreadable_report_is_reported_rather_than_raised(self) -> None:
        project_dir = _project()

        def refuse(path: Path) -> str:
            raise WorkspaceError(f"cannot read {path}: simulated")

        with patch.object(workspace_lib, "read_text", side_effect=refuse):
            warnings = report_warnings(project_dir)

        self.assertEqual(len(warnings), 2, warnings)
        for warning in warnings:
            self.assertIn("is unreadable", warning)


def _task(status: str) -> "dict[str, Any]":
    return {
        "id": "T01",
        "name": "Work",
        "status": status,
        "depends_on": [],
        "outputs": [],
        "success_criteria": "Done",
        "verification": "Checked",
        "evidence": [{"root": "workspace", "path": "evidence.md", "anchor": None}] if status == "DONE" else [],
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
        "block_reason": "Waiting on an answer." if status == "BLOCKED" else None,
    }


class ReportTriggerTests(unittest.TestCase):
    """When the warning fires, which is the half of this rule that is easy to get wrong."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "ws"
        workspace.mkdir()
        target = root / "target"
        target.mkdir()
        self.project_dir = allocate_project(workspace, title="Report", working_directory=target)
        (self.project_dir / "reflection.md").write_text("# Reflection\n\nWent fine.\n", encoding="utf-8")

    def _state(self, status: str) -> "dict[str, Any]":
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state["status"] = status
        if status == "CANCELLED":
            state["cancellation_reason"] = "Superseded by a later project."
        if status == "BLOCKED":
            state["tasks"] = [_task("BLOCKED")]
        elif status not in ("ALIGNING", "PLANNING"):
            state["tasks"] = [_task("DONE")]
        return state

    def _report_warnings(self, status: str, **kwargs: Any) -> "list[str]":
        report = validate_v4_state(self._state(status), self.project_dir, **kwargs)
        self.assertTrue(report.valid, report.errors)
        return [warning for warning in report.warnings if "report.md" in warning or "report.html" in warning]

    def test_no_status_short_of_close_warns_about_a_missing_report(self) -> None:
        # A report cannot exist before the work it reports on. Warning here would fire on every
        # validation a project runs through its whole working life, about a file it is right not to
        # have — which is how a warning stops being read.
        for status in ("ALIGNING", "PLANNING", "EXECUTING", "REVIEW", "BLOCKED"):
            with self.subTest(status=status):
                self.assertEqual(self._report_warnings(status), [])

    def test_closing_without_optional_reports_is_silent(self) -> None:
        self.assertEqual(self._report_warnings("EXECUTING", close=True), [])

    def test_an_already_done_project_needs_no_report(self) -> None:
        self.assertEqual(self._report_warnings("DONE"), [])

    def test_a_cancelled_project_needs_no_report(self) -> None:
        self.assertEqual(self._report_warnings("CANCELLED"), [])

    def test_the_check_is_skipped_when_files_are_not_being_read(self) -> None:
        self.assertEqual(self._report_warnings("DONE", check_files=False), [])

    def test_the_finding_is_never_an_error_and_never_blocks_closure(self) -> None:
        # The whole reason this is a warning: every project that closed before the step existed must
        # stay valid, and stay reopenable for maintenance.
        report = validate_v4_state(self._state("EXECUTING"), self.project_dir, close=True)

        self.assertTrue(report.valid, report.errors)
        self.assertEqual([error for error in report.errors if "report" in error], [])

    def test_a_written_report_warns_about_nothing_at_close(self) -> None:
        artifacts = self.project_dir / REPORT_DIRECTORY
        (artifacts / REPORT_MARKDOWN_FILENAME).write_text(GOOD_MARKDOWN, encoding="utf-8")
        (artifacts / REPORT_HTML_FILENAME).write_text(GOOD_HTML, encoding="utf-8")

        self.assertEqual(self._report_warnings("EXECUTING", close=True), [])


class ReportCheckTests(unittest.TestCase):
    """`--report`: every asserted property, and a fixture that makes each one fail."""

    def test_a_conforming_pair_reports_nothing(self) -> None:
        self.assertEqual(_errors(), [])

    def test_an_absent_pair_names_both_files(self) -> None:
        errors = _errors(markdown=None, html=None)
        self.assertEqual(len(errors), 2, errors)
        for error in errors:
            self.assertIn("no report at", error)

    def test_an_unreadable_report_is_an_error_rather_than_a_crash(self) -> None:
        project_dir = _project()

        def refuse(path: Path) -> str:
            raise WorkspaceError(f"cannot read {path}: simulated")

        with patch.object(workspace_lib, "read_text", side_effect=refuse):
            errors = report_findings(project_dir).errors

        self.assertEqual(len(errors), 2, errors)
        for error in errors:
            self.assertIn("is unreadable", error)

    def test_a_missing_section_is_an_error_in_either_file(self) -> None:
        self.assertIn(
            "no '## Findings and evidence' section",
            " ".join(_errors(markdown=GOOD_MARKDOWN.replace("## Findings and evidence\n\nWritten.\n\n", ""))),
        )
        self.assertIn(
            "no '## Findings and evidence' section",
            " ".join(_errors(html=GOOD_HTML.replace("<h2>Findings and evidence</h2>", "<p>Findings</p>"))),
        )

    def test_an_unclosed_tag_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("</body></html>\n", ""))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("<body> is never closed", errors[0])

    def test_a_close_tag_with_nothing_open_is_named(self) -> None:
        errors = _errors(html="</section>\n" + GOOD_HTML)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("</section> closes a tag that was never opened", errors[0])

    def test_crossed_tags_are_named(self) -> None:
        html = GOOD_HTML.replace("<figcaption>", "<em><figcaption>").replace("</figcaption>", "</em></figcaption>")
        errors = _errors(html=html)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("</em> closes while <figcaption> is still open", errors[0])

    def test_void_elements_and_self_closing_tags_do_not_unbalance_the_document(self) -> None:
        # `<link>` and `<meta>` never close, and `<rect ... />` closes itself. Waiting for either
        # would make a correct document fail.
        html = GOOD_HTML.replace("<body>", '<body>\n<meta name="x" content="y">\n<hr>\n<br/>')
        self.assertEqual(_errors(html=html), [])

    def test_a_comment_cannot_unbalance_the_document(self) -> None:
        self.assertEqual(_errors(html=GOOD_HTML.replace("<body>", "<body><!-- <div> not real --> ")), [])

    def test_an_external_script_is_named(self) -> None:
        html = GOOD_HTML.replace("</head>", '<script src="https://cdn.example/chart.js"></script></head>')
        errors = _errors(html=html)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("loads an external script", errors[0])

    def test_an_inline_script_is_left_alone(self) -> None:
        # The rule is about what the page fetches at render time. An inline script is inert here.
        self.assertEqual(_errors(html=GOOD_HTML.replace("</body>", "<script>void 0;</script></body>")), [])

    def test_a_stylesheet_that_is_not_the_font_link_is_named(self) -> None:
        html = GOOD_HTML.replace("https://fonts.googleapis.com/css2?family=Inter", "https://cdn.example/reset.css")
        errors = _errors(html=html)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("loads an external stylesheet", errors[0])

    def test_a_stylesheet_link_with_no_href_is_named(self) -> None:
        errors = _errors(
            html=GOOD_HTML.replace(
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
                '<link rel="stylesheet">',
            )
        )
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no href", errors[0])

    def test_a_link_that_is_not_a_stylesheet_is_left_alone(self) -> None:
        self.assertEqual(_errors(html=GOOD_HTML.replace("<body>", '<link rel="icon" href="/f.png"><body>')), [])

    def test_an_at_import_is_named_once_however_many_there_are(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("<style>", '<style>@import url("a.css");@import url("b.css");'))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("@import", errors[0])

    def test_a_hex_colour_outside_a_token_definition_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("body { color: var(--fg);", "body { color: #333333;"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("#333333", errors[0])

    def test_a_named_colour_outside_a_token_definition_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("body { color: var(--fg);", "body { background: white;"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("white", errors[0])

    def test_a_functional_colour_outside_a_token_definition_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("body { color: var(--fg);", "body { color: rgb(1 2 3);"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("rgb(", errors[0])

    def test_literals_inside_token_definitions_are_where_they_belong(self) -> None:
        # `--fg: #111111` is the definition the theme blocks exist to redefine, not a violation.
        self.assertEqual(_errors(), [])

    def test_a_colour_word_in_a_non_colour_property_is_not_a_colour(self) -> None:
        # `font-family: Georgia, serif` is already in the fixture; this pins the harder case.
        html = GOOD_HTML.replace("font-family: Georgia, serif", "font-family: Coral, serif")
        self.assertEqual(_errors(html=html), [])

    def test_each_missing_theme_block_is_named(self) -> None:
        for description, replacement in (
            ("prefers-color-scheme: dark", ("prefers-color-scheme: dark", "prefers-contrast: more")),
            ('data-theme="dark"', (':root[data-theme="dark"]', ".dark-mode")),
        ):
            with self.subTest(description=description):
                errors = _errors(html=GOOD_HTML.replace(*replacement))
                self.assertTrue(any(description in error for error in errors), errors)

    def test_a_document_with_no_style_block_at_all_names_every_theme_block(self) -> None:
        html = GOOD_HTML[: GOOD_HTML.index("<style>")] + GOOD_HTML[GOOD_HTML.index("</style>") + len("</style>") :]
        errors = [error for error in _errors(html=html) if "theme" in error or "palette" in error]
        self.assertEqual(len(errors), len(REPORT_THEME_BLOCKS), errors)

    def test_a_chart_without_a_role_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace('role="img" ', ""))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('has no role="img"', errors[0])

    def test_a_chart_with_an_empty_aria_label_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace('aria-label="Bar chart of two runs"', 'aria-label="   "'))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no non-empty aria-label", errors[0])

    def test_a_single_quoted_attribute_is_read_the_same_way(self) -> None:
        self.assertEqual(_errors(html=GOOD_HTML.replace('role="img"', "role='img'")), [])

    def test_a_single_quoted_empty_attribute_is_empty_rather_than_absent(self) -> None:
        # `aria-label=''` is present and says nothing, which is the failure the check is for.
        errors = _errors(html=GOOD_HTML.replace('aria-label="Bar chart of two runs"', "aria-label=''"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no non-empty aria-label", errors[0])

    def test_a_chart_outside_a_figure_is_named(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("<figure>\n", "").replace("</figure>\n", ""))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("outside a <figure>", errors[0])

    def test_a_chart_with_no_figcaption_is_named(self) -> None:
        errors = _errors(
            html=GOOD_HTML.replace("<figcaption>The second run is four times the first.</figcaption>\n", "")
        )
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no non-empty <figcaption>", errors[0])

    def test_a_blank_figcaption_is_named(self) -> None:
        # The fixture that found the defect: `\s*\S` matched the `<` of `</figcaption>`, so a caption
        # holding one space passed as written.
        errors = _errors(html=GOOD_HTML.replace("The second run is four times the first.", " "))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no non-empty <figcaption>", errors[0])

    def test_a_figcaption_of_nothing_but_markup_is_blank(self) -> None:
        errors = _errors(html=GOOD_HTML.replace("The second run is four times the first.", "<span></span>"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no non-empty <figcaption>", errors[0])

    def test_a_figure_holding_no_chart_needs_no_caption(self) -> None:
        html = GOOD_HTML.replace("</body>", "<figure><table><tr><td>1</td></tr></table></figure></body>")
        self.assertEqual(_errors(html=html), [])


class ReportCheckCliTests(unittest.TestCase):
    def _validate(self, project_dir: Path, *flags: str) -> int:
        with patch.object(sys, "argv", ["validate", str(project_dir), *flags]):
            return validate_workspace.main()

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        work = Path(tempfile.mkdtemp())
        self.project_dir = allocate_project(root, title="Report CLI", working_directory=work, create_root=True)

    def _write_reports(self, *, html: str = GOOD_HTML) -> None:
        (self.project_dir / REPORT_DIRECTORY / REPORT_MARKDOWN_FILENAME).write_text(GOOD_MARKDOWN, encoding="utf-8")
        (self.project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(html, encoding="utf-8")

    def test_the_flag_is_opt_in(self) -> None:
        # No report written, and no `--report`: the project is valid. Opt-in is what lets the check
        # be strict without making every project that has not closed yet fail.
        self.assertEqual(self._validate(self.project_dir), 0)

    def test_the_flag_fails_a_project_with_no_report(self) -> None:
        self.assertEqual(self._validate(self.project_dir, "--report"), 1)

    def test_the_flag_passes_a_conforming_pair(self) -> None:
        self._write_reports()
        self.assertEqual(self._validate(self.project_dir, "--report"), 0)

    def test_the_flag_fails_a_broken_pair(self) -> None:
        self._write_reports(html=GOOD_HTML.replace('role="img" ', ""))
        self.assertEqual(self._validate(self.project_dir, "--report"), 1)

    def test_the_launcher_exposes_the_flag(self) -> None:
        self._write_reports()
        completed = subprocess.run(
            [sys.executable, str(VALIDATOR), str(self.project_dir), "--report"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class ReportEvidenceStepTests(unittest.TestCase):
    """`record-evidence --step`: a heading for work that has no task, without loosening `--task`."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.work = Path(tempfile.mkdtemp())
        self.project_dir = allocate_project(root, title="Step", working_directory=self.work, create_root=True)

    def _evidence(self) -> str:
        return (self.project_dir / "evidence.md").read_text(encoding="utf-8")

    def test_a_step_records_against_a_project_with_no_tasks_at_all(self) -> None:
        # The strongest form of "there is no task id to use": at closure every task is terminal, and
        # an empty plan means none of them could serve as the heading either way.
        code = workspace_lib.record_evidence(self.project_dir, None, ["/bin/echo", "checked"], step="report")

        self.assertEqual(code, 0)
        self.assertIn("## report — /bin/echo checked", self._evidence())
        self.assertIn("- Exit code: 0 (passed)", self._evidence())

    def test_a_failing_step_command_is_recorded_and_returned_as_a_failure(self) -> None:
        code = workspace_lib.record_evidence(self.project_dir, None, ["/bin/sh", "-c", "exit 3"], step="report")

        self.assertEqual(code, 3)
        self.assertIn("- Exit code: 3 (FAILED)", self._evidence())

    def test_recording_a_step_does_not_touch_canonical_state(self) -> None:
        before = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        workspace_lib.record_evidence(self.project_dir, None, ["/bin/echo", "x"], step="report")
        after = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))

        self.assertEqual(before, after)

    def test_a_step_outside_the_vocabulary_is_refused_and_the_vocabulary_named(self) -> None:
        with self.assertRaises(WorkspaceError) as raised:
            workspace_lib.record_evidence(self.project_dir, None, ["/bin/echo", "x"], step="summary")

        self.assertIn("unknown closure step 'summary'", str(raised.exception))
        self.assertIn("report", str(raised.exception))

    def test_a_task_and_a_step_together_are_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as raised:
            workspace_lib.record_evidence(self.project_dir, "T01", ["/bin/echo", "x"], step="report")

        self.assertIn("not both", str(raised.exception))

    def test_neither_a_task_nor_a_step_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError) as raised:
            workspace_lib.record_evidence(self.project_dir, None, ["/bin/echo", "x"])

        self.assertIn("needs --task", str(raised.exception))

    def test_the_task_existence_guard_is_unchanged(self) -> None:
        # The reason `--step` exists rather than `--task` simply accepting anything: a typo'd task id
        # must still be refused instead of quietly opening a heading of its own.
        with self.assertRaises(WorkspaceError) as raised:
            workspace_lib.record_evidence(self.project_dir, "T99", ["/bin/echo", "x"])

        self.assertIn("unknown task 'T99'", str(raised.exception))

    def test_the_cli_records_a_step_and_reports_it_by_name(self) -> None:
        argv = ["manage", "record-evidence", str(self.project_dir), "--step", "report", "--", "/bin/echo", "ok"]
        import manage_workspace

        with patch.object(sys, "argv", argv):
            self.assertEqual(manage_workspace.main(), 0)
        self.assertIn("## report — /bin/echo ok", self._evidence())

    def test_the_cli_reports_a_failing_step_by_name_and_exits_non_zero(self) -> None:
        argv = ["manage", "record-evidence", str(self.project_dir), "--step", "report", "--", "/bin/sh", "-c", "exit 4"]
        import manage_workspace

        with patch.object(sys, "argv", argv):
            self.assertEqual(manage_workspace.main(), 1)
        self.assertIn("- Exit code: 4 (FAILED)", self._evidence())

    def test_the_cli_refuses_a_task_and_a_step_together(self) -> None:
        argv = [
            "manage",
            "record-evidence",
            str(self.project_dir),
            "--task",
            "T01",
            "--step",
            "report",
            "--",
            "/bin/echo",
            "x",
        ]
        import manage_workspace

        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            manage_workspace.main()
        self.assertEqual(raised.exception.code, 2)

    def test_the_cli_requires_one_of_them(self) -> None:
        argv = ["manage", "record-evidence", str(self.project_dir), "--", "/bin/echo", "x"]
        import manage_workspace

        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            manage_workspace.main()
        self.assertEqual(raised.exception.code, 2)

    def test_the_cli_refuses_a_step_outside_the_vocabulary(self) -> None:
        argv = ["manage", "record-evidence", str(self.project_dir), "--step", "summary", "--", "/bin/echo", "x"]
        import manage_workspace

        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            manage_workspace.main()
        self.assertEqual(raised.exception.code, 2)


class ReferenceReportTests(unittest.TestCase):
    """The check has to be satisfiable by a report somebody actually hand-authored.

    A rule set nobody can meet is indistinguishable from one nobody applies. This reads the workspace
    report the design was taken from and asserts that everything except the section names — which
    predate the contract — already passes.
    """

    def setUp(self) -> None:
        import os

        root = os.environ.get("RESEARCH_WORKSPACE")
        candidate = Path(root, "2026-09-09-003", "artifacts", "comparison_report.html") if root else None
        if candidate is None or not candidate.is_file():
            self.skipTest("the reference report is not present in this environment")
        self.reference = candidate.read_text(encoding="utf-8")

    def test_the_reference_report_satisfies_every_structural_rule(self) -> None:
        errors = [error for error in _errors(html=self.reference) if "section" not in error]

        self.assertEqual(errors, [], errors)


class DocumentedContractTests(unittest.TestCase):
    """The documented section list has to be the one the code recognises.

    Five names in a skill document and five names in a tuple drift apart silently: the author follows
    the document, the check rejects the report, and the check is what gets edited. So the documented
    list is read back out of every surface that carries one and compared with the code.

    The heading level matters too, and not only for readers. `tests/plugins/research/test_skill_docs.py`
    collects every `### ` heading from every fenced markdown block in every `SKILL.md` and asserts the
    collection equals the *specification* sections exactly. A report contract written at `###` inside
    such a block would fail a test that has nothing to do with reports, so the contract is documented
    at `##` in a `text` block.
    """

    SURFACES = (
        "plugins/research/skills/project/references/report-design.md",
        "plugins/research/skills/project/references/workspace-schema.md",
    )

    def _documented(self, path: Path) -> "list[list[str]]":
        # A fenced block nested in a numbered list is indented, so the headings are too.
        blocks = re.findall(r"```(?:text|markdown)\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
        found = []
        for block in blocks:
            names = [name.strip() for name in re.findall(r"^ *## (.+)$", block, re.MULTILINE)]
            if names and names[0] == REPORT_CANONICAL_SECTIONS[0][0]:
                found.append(names)
        return found

    def test_every_surface_documents_the_contract_exactly_once_and_correctly(self) -> None:
        canonical = [name for name, _ in REPORT_CANONICAL_SECTIONS]
        for relative in self.SURFACES:
            with self.subTest(surface=relative):
                path = REPO_ROOT / relative
                self.assertTrue(path.is_file(), relative)
                blocks = self._documented(path)
                self.assertEqual(len(blocks), 1, f"{relative} documents the contract {len(blocks)} times")
                self.assertEqual(blocks[0], canonical)

    def test_no_surface_documents_the_contract_at_the_wrong_heading_level(self) -> None:
        for relative in self.SURFACES:
            with self.subTest(surface=relative):
                content = (REPO_ROOT / relative).read_text(encoding="utf-8")
                for name, _ in REPORT_CANONICAL_SECTIONS:
                    self.assertNotIn(f"### {name}", content)

    def test_the_documented_invocations_match_the_implemented_flags(self) -> None:
        content = (REPO_ROOT / "plugins/research/skills/project/references/commands.md").read_text(encoding="utf-8")

        for step in CLOSURE_STEPS:
            self.assertIn(f"--step {step}", content)
        self.assertIn("--report", content)
        # record-evidence runs its command from the working directory, not the project directory, so a
        # documented relative project path would be a documented failure.
        documented = [line for line in content.splitlines() if "record-evidence" in line and "--step" in line]
        self.assertTrue(documented)
        for line in documented:
            self.assertIn("<project-dir>", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
