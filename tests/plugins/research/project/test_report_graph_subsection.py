"""Tests for the one `###` subsection the report contract names: the task graph.

The five `##` sections are a closed spine, so the graph presentation is subordinate to a section
rather than a sixth one — and `report-design.md` says which section, because "under `## What was
done`" was a decision, not an accident: that section owns method and scope, and a graph filed under
`## Open work` would be a different claim about the work. So placement is part of the contract and is
tested as such.

Three properties carry most of the weight here.

*Recognition is by heading.* A structural check cannot read a report's meaning, so it finds the
subsection by heading or not at all. The accepted rewordings are tested, and so is the fact that a
heading which does not say graph is not this subsection however well it reads.

*Severity is asymmetric.* Absence is an error under `--report`, which is opt-in and may be strict,
and a warning at close and for a project already `DONE` — because every report written before this
contract must stay valid and its project must stay reopenable. Both polarities are asserted, with
the same wording, since a check that only ever errors would silently invalidate history.

*One contract, two files.* `###` in Markdown and `<h3>` through `<h6>` in HTML must be judged the
same way. That symmetry is here because it was once broken: `_level_three_sections` accepts
`###`-or-deeper, so `#### Task graph` satisfied the Markdown reader while `<h4>` failed the HTML one,
and a report could have passed as one file and failed as the other over a nesting depth neither
contract cares about.

Every assertion below has its failing state exercised somewhere in this file. A check whose failing
state is unreachable proves nothing about the report it is supposed to police.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workspace_lib import (
    REPORT_CANONICAL_SECTIONS,
    REPORT_DIRECTORY,
    REPORT_GRAPH_PARENT,
    REPORT_GRAPH_SUBSECTION,
    REPORT_HTML_FILENAME,
    REPORT_MARKDOWN_FILENAME,
    report_findings,
    report_warnings,
)

CANONICAL, RECOGNISER = REPORT_GRAPH_SUBSECTION


# What the finding says, built from the constants so a rename reaches the assertions rather than
# leaving them passing against a message the check no longer emits.
def _expected(label: str) -> str:
    return f"{label} has no written '### {CANONICAL}' subsection under '## {REPORT_GRAPH_PARENT}'"


def _markdown(subsection: str = f"### {CANONICAL}\n\nWritten.\n\n", *, parent: str = REPORT_GRAPH_PARENT) -> str:
    """A structurally conforming Markdown report, with the subsection placed under `parent`."""
    return "# Title — report\n\n" + "".join(
        f"## {canonical}\n\nWritten.\n\n" + (subsection if canonical == parent else "")
        for canonical, _ in REPORT_CANONICAL_SECTIONS
    )


def _project(markdown: str) -> Path:
    """A project directory whose Markdown report is `markdown` and whose HTML report is conforming."""
    project_dir = Path(tempfile.mkdtemp())
    (project_dir / REPORT_DIRECTORY).mkdir()
    (project_dir / REPORT_DIRECTORY / REPORT_MARKDOWN_FILENAME).write_text(markdown, encoding="utf-8")
    (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(_html(), encoding="utf-8")
    return project_dir


def _html(subsection: str = f"<h3>{CANONICAL}</h3>\n<p>Written.</p>\n", *, parent: str = REPORT_GRAPH_PARENT) -> str:
    """A conforming HTML report: the theme blocks and tokens `--report` also insists on, plus headings."""
    return (
        '<!doctype html>\n<html lang="en"><head><title>Title</title>\n'
        "<style>\n"
        ":root { --fg: #111111; }\n"
        '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg: #eeeeee; } }\n'
        ':root[data-theme="dark"] { --fg: #eeeeee; }\n'
        "body { color: var(--fg); }\n"
        "</style></head><body>\n<h1>Title</h1>\n"
        + "".join(
            f"<h2>{canonical}</h2>\n<p>Written.</p>\n" + (subsection if canonical == parent else "")
            for canonical, _ in REPORT_CANONICAL_SECTIONS
        )
        + "</body></html>\n"
    )


def _html_project(html: str) -> Path:
    project_dir = Path(tempfile.mkdtemp())
    (project_dir / REPORT_DIRECTORY).mkdir()
    (project_dir / REPORT_DIRECTORY / REPORT_MARKDOWN_FILENAME).write_text(_markdown(), encoding="utf-8")
    (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(html, encoding="utf-8")
    return project_dir


class MarkdownRecognitionTests(unittest.TestCase):
    def test_a_conforming_report_has_nothing_to_say_about_the_subsection(self) -> None:
        self.assertEqual(report_findings(_project(_markdown())).errors, [])

    def test_a_report_without_the_subsection_errors_naming_the_file_and_the_parent(self) -> None:
        errors = report_findings(_project(_markdown(subsection=""))).errors
        self.assertEqual(errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}")])

    def test_every_reworded_heading_the_contract_documents_is_accepted(self) -> None:
        # These five are the spellings `report-design.md` promises. If a rewording is removed from the
        # recogniser, the documented promise breaks here rather than in someone's report.
        for heading in (
            "Task graph",
            "The task graph",
            "Dependency graph",
            "Task dependency graph",
            "Plan and execution graph",
        ):
            with self.subTest(heading=heading):
                report = _project(_markdown(f"### {heading}\n\nWritten.\n\n"))
                self.assertEqual(report_findings(report).errors, [])

    def test_a_heading_that_does_not_say_graph_is_not_this_subsection(self) -> None:
        # However well it reads. The check finds the subsection by its heading or not at all, and the
        # alternative — inferring it from a table or an SVG — would accept any figure at all.
        for heading in ("Tasks", "How the work was organised", "Execution timeline"):
            with self.subTest(heading=heading):
                report = _project(_markdown(f"### {heading}\n\nWritten.\n\n"))
                self.assertEqual(
                    report_findings(report).errors,
                    [_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}")],
                )

    def test_the_heading_is_matched_without_regard_to_case(self) -> None:
        self.assertEqual(report_findings(_project(_markdown("### TASK GRAPH\n\nWritten.\n\n"))).errors, [])

    def test_a_heading_deeper_than_three_is_accepted(self) -> None:
        # `_level_three_sections` accepts `###`-or-deeper for the spec's sake, and the HTML twin
        # translates `<h4>` the same way, so accepting it here is what keeps the two files symmetric.
        self.assertEqual(report_findings(_project(_markdown("#### Task graph\n\nWritten.\n\n"))).errors, [])


class PlacementTests(unittest.TestCase):
    def test_the_subsection_must_sit_under_the_method_section(self) -> None:
        # A graph under `## Open work` is a claim about what is left, not about how the work was done.
        other = next(name for name, _ in REPORT_CANONICAL_SECTIONS if name != REPORT_GRAPH_PARENT)
        report = _project(_markdown(parent=other))
        self.assertEqual(report_findings(report).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}")])

    def test_a_subsection_under_a_reworded_parent_heading_still_counts(self) -> None:
        # The parent is recognised by the same pattern the five sections use, so a report that titles
        # its method section `## Method` is not penalised for the wording.
        markdown = (
            "# Title — report\n\n## Summary\n\nWritten.\n\n## Method\n\nWritten.\n\n"
            f"### {CANONICAL}\n\nWritten.\n\n"
            "## Findings and evidence\n\nWritten.\n\n"
            "## Limitations and what was not proven\n\nWritten.\n\n## Open work\n\nWritten.\n"
        )
        self.assertEqual(report_findings(_project(markdown)).errors, [])

    def test_a_missing_parent_section_is_reported_once_as_a_missing_section(self) -> None:
        # Two findings for one missing heading would read as two problems. The section check owns it.
        markdown = "# Title — report\n\n" + "".join(
            f"## {canonical}\n\nWritten.\n\n"
            for canonical, _ in REPORT_CANONICAL_SECTIONS
            if canonical != REPORT_GRAPH_PARENT
        )
        errors = report_findings(_project(markdown)).errors
        self.assertNotIn(_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}"), errors)
        self.assertTrue(any(REPORT_GRAPH_PARENT in error for error in errors), errors)


class WrittenBodyTests(unittest.TestCase):
    def test_a_heading_with_nothing_under_it_counts_as_absent(self) -> None:
        report = _project(_markdown(f"### {CANONICAL}\n\n"))
        self.assertEqual(report_findings(report).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}")])

    def test_a_placeholder_body_counts_as_absent_too(self) -> None:
        report = _project(_markdown(f"### {CANONICAL}\n\n_Not yet written._\n\n"))
        self.assertEqual(report_findings(report).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}")])

    def test_a_written_subsection_alongside_an_empty_one_satisfies_the_contract(self) -> None:
        # Only one of them has to be written: a report may reasonably carry both a graph figure and a
        # graph table under separate headings, and an empty stub among them is not the finding.
        subsection = f"### {CANONICAL}\n\n### Dependency graph\n\nWritten.\n\n"
        self.assertEqual(report_findings(_project(_markdown(subsection))).errors, [])


class HtmlSymmetryTests(unittest.TestCase):
    def test_a_conforming_html_report_passes(self) -> None:
        self.assertEqual(report_findings(_html_project(_html())).errors, [])

    def test_an_html_report_without_the_subsection_errors_naming_the_html_file(self) -> None:
        errors = report_findings(_html_project(_html(subsection=""))).errors
        self.assertEqual(errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}")])

    def test_every_heading_level_the_translation_accepts_satisfies_the_contract(self) -> None:
        # The asymmetry this pins was a real defect: only `<h3>` was translated, so `#### Task graph`
        # passed as Markdown while `<h4>Task graph</h4>` failed as HTML.
        for level in (3, 4, 5, 6):
            with self.subTest(level=level):
                html = _html(f"<h{level}>{CANONICAL}</h{level}>\n<p>Written.</p>\n")
                self.assertEqual(report_findings(_html_project(html)).errors, [])

    def test_a_heading_with_attributes_is_still_a_heading(self) -> None:
        html = _html(f'<h3 id="graph" class="tight">{CANONICAL}</h3>\n<p>Written.</p>\n')
        self.assertEqual(report_findings(_html_project(html)).errors, [])

    def test_inline_markup_inside_the_heading_is_stripped_before_matching(self) -> None:
        html = _html(f"<h3><em>{CANONICAL}</em></h3>\n<p>Written.</p>\n")
        self.assertEqual(report_findings(_html_project(html)).errors, [])

    def test_the_words_in_a_paragraph_do_not_stand_in_for_the_heading(self) -> None:
        # A report that discusses the task graph in prose has not filed the subsection, and the check
        # must not accept the phrase wherever it appears or it would accept almost every report.
        html = _html(f"<p>The {CANONICAL.lower()} is described below.</p>\n")
        self.assertEqual(
            report_findings(_html_project(html)).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}")]
        )

    def test_an_h2_does_not_satisfy_the_subsection(self) -> None:
        # It would be a sixth `##` section, which the spine is closed against.
        html = _html(f"<h2>{CANONICAL}</h2>\n<p>Written.</p>\n")
        self.assertEqual(
            report_findings(_html_project(html)).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}")]
        )

    def test_a_heading_inside_a_style_block_does_not_count(self) -> None:
        # `<style>` bodies are removed whole, so CSS text cannot furnish a heading or a written body.
        html = _html(f"<style>/* <h3>{CANONICAL}</h3> Written. */</style>\n")
        self.assertEqual(
            report_findings(_html_project(html)).errors, [_expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}")]
        )

    def test_both_files_are_judged_and_both_findings_are_reported(self) -> None:
        project_dir = Path(tempfile.mkdtemp())
        (project_dir / REPORT_DIRECTORY).mkdir()
        (project_dir / REPORT_DIRECTORY / REPORT_MARKDOWN_FILENAME).write_text(
            _markdown(subsection=""), encoding="utf-8"
        )
        (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(_html(subsection=""), encoding="utf-8")
        self.assertEqual(
            report_findings(project_dir).errors,
            [
                _expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}"),
                _expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}"),
            ],
        )


class SeverityTests(unittest.TestCase):
    """The same absence, in the two places it is judged, with two different consequences."""

    def test_at_close_the_absence_is_a_warning_with_the_same_wording(self) -> None:
        project_dir = _project(_markdown(subsection=""))
        (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(_html(subsection=""), encoding="utf-8")
        warnings = report_warnings(project_dir)
        self.assertIn(_expected(f"{REPORT_DIRECTORY}/{REPORT_MARKDOWN_FILENAME}"), warnings)
        self.assertIn(_expected(f"{REPORT_DIRECTORY}/{REPORT_HTML_FILENAME}"), warnings)

    def test_a_conforming_report_warns_about_nothing_at_close(self) -> None:
        self.assertEqual(report_warnings(_project(_markdown())), [])

    def test_the_close_time_warning_and_the_report_time_error_disagree_only_in_severity(self) -> None:
        # This is the asymmetry that keeps a report written before the contract from invalidating its
        # project: `--report` refuses it, close notes it, and neither reads it differently.
        project_dir = _project(_markdown(subsection=""))
        (project_dir / REPORT_DIRECTORY / REPORT_HTML_FILENAME).write_text(_html(subsection=""), encoding="utf-8")
        graph_errors = [error for error in report_findings(project_dir).errors if CANONICAL in error]
        graph_warnings = [warning for warning in report_warnings(project_dir) if CANONICAL in warning]
        self.assertEqual(graph_errors, graph_warnings)
        self.assertEqual(len(graph_errors), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
