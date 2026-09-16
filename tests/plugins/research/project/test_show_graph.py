"""Tests for the console form of the task graph and the `show-graph` subcommand that prints it.

This is what a user reads at the step between planning and execution, and the only thing standing
between them and a wrong reading of their own plan. So the assertions here are on exact output, not on
substrings: a changed column, a changed heading, a lost alignment, or a lost blank line has to be a
deliberate edit to a test rather than a silent change in what users see. Substring assertions would
pass through all of those.

One rendering serves two moments. Before execution the table describes a plan; once anything has run
it grows a `Status` column and the summary grows the measured span. That is why the function takes no
mode flag, and both forms are pinned below — along with the fact that every column they share carries
the same value, since the two surfaces the project ships are two renderers over one model and the
whole point of that is that they cannot disagree.

The warnings block gets more attention than its size suggests. A column cannot carry urgency: an
authorization still `pending` on row 12 of 62 is a fact with consequences and a reader scanning a wide
table will miss it. It also speaks when nothing is outstanding, which is deliberate — a block that is
sometimes silent cannot be trusted when it says nothing is waiting — and that silence-is-not-a-signal
property is asserted rather than assumed.

The last class runs the real subcommand against the real closed predecessor project, because a
renderer that only ever meets fixtures has only ever met the shapes its author imagined. That project
holds 15 tasks and 32 evidence entries, including a status the fixtures never produce.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from workspace_lib import (
    DEPENDENCY_DISPLAY_WIDTH,
    TASK_NAME_DISPLAY_WIDTH,
    WorkspaceError,
    build_task_graph,
    project_task_graph,
    render_task_graph,
)

from tests.conftest import MANAGER

PREDECESSOR = Path("/Users/npham/git_tree/research/workspace/2026-09-10-002")


def _task(task_id: str, **overrides: object) -> "dict[str, object]":
    task: "dict[str, object]" = {
        "id": task_id,
        "name": f"Task {task_id}",
        "status": "TODO",
        "depends_on": [],
        "effect": {"kind": "none"},
        "authorization": {"status": "not_required"},
        "receipts": [],
    }
    task.update(overrides)
    return task


def _state(*tasks: object, project: str = "2026-01-01-001", status: str = "PLANNING") -> "dict[str, object]":
    return {"schema_version": 3, "project": project, "status": status, "tasks": list(tasks)}


def _render(*tasks: object, evidence: str = "", **state: str) -> str:
    return render_task_graph(build_task_graph(_state(*tasks, **state), evidence))


def _entry(owner: str, recorded: str, exit_code: int = 0) -> str:
    return (
        f"## {owner} — cmd\n\n- Recorded: {recorded}\n- Working directory: /tmp\n"
        f"- Exit code: {exit_code} ({'passed' if exit_code == 0 else 'failed'})\n\n"
    )


def _table(output: str) -> "tuple[list[str], list[dict[str, str]]]":
    """The rendered table as its headers and one dict of cells per row.

    Cells are split on runs of two-or-more spaces, which is what separates columns. Splitting on all
    whitespace would break on the first task name containing a space — every real one does — and
    cutting at the header's character offsets would mis-read the right-aligned columns, whose header
    text sits at the column's right edge rather than its left. A continuation row carries only a
    dependency fragment, so it is returned as that one cell.

    Only the contiguous run of indented lines counts: the warnings block underneath is indented too,
    and a filter that swallowed it would read "authorization pending, external effect" as table cells.
    """
    lines: "list[str]" = []
    for line in output.splitlines():
        if line.startswith("  "):
            lines.append(line)
        elif lines:
            break
    names = re.split(r" {2,}", lines[0].strip())
    rows = []
    for line in lines[1:]:
        cells = re.split(r" {2,}", line.strip())
        rows.append(dict(zip(names, cells, strict=True)) if len(cells) == len(names) else {"Deps": cells[0]})
    return names, rows


class PlanFormTests(unittest.TestCase):
    """What the step before execution prints, when nothing has run."""

    def test_the_whole_rendering_of_a_small_plan(self) -> None:
        output = _render(
            _task("T01", name="Read the code", effect={"kind": "none"}),
            _task(
                "T02",
                name="Write the change",
                depends_on=["T01"],
                effect={"kind": "local_write"},
            ),
            _task(
                "T03",
                name="Open the pull request",
                depends_on=["T02"],
                effect={"kind": "external"},
                authorization={"status": "pending"},
            ),
        )
        self.assertEqual(
            output,
            "\n".join(
                (
                    "2026-01-01-001 | PLANNING | 3 tasks | 3 levels | effects: external 1, local_write 1, none 1",
                    "",
                    "  #  ID   Task                   Lv  Deps  Effect       Auth",
                    "  1  T01  Read the code           0  -     none         -",
                    "  2  T02  Write the change        1  T01   local_write  -",
                    "  3  T03  Open the pull request   2  T02   external     pending",
                    "",
                    "Authorization outstanding for 1 of 3 tasks:",
                    "  T03  authorization pending, external effect: Open the pull request",
                )
            ),
        )

    def test_a_plan_that_has_not_run_has_no_status_column_and_no_span_line(self) -> None:
        lines = _render(_task("T01")).splitlines()
        self.assertNotIn("Status", lines[2])
        self.assertNotIn("verification spans", lines[0])
        # The summary is one line, then a blank, then the header: two lines before the table.
        self.assertEqual(lines[1], "")

    def test_a_project_with_no_tasks_says_so_in_words_rather_than_printing_a_bare_header(self) -> None:
        # A header row over nothing reads as a rendering fault rather than as a fact about the project.
        output = _render()
        self.assertEqual(
            output,
            "\n".join(
                (
                    "2026-01-01-001 | PLANNING | 0 tasks | 0 levels | effects: none declared",
                    "",
                    "No tasks planned yet.",
                    "",
                    "Authorization: nothing outstanding.",
                )
            ),
        )

    def test_the_output_carries_no_trailing_newline(self) -> None:
        # The caller prints it. A renderer that supplied its own would put a blank line under every
        # summary, and `print(render(...))` is how both the subcommand and the report author call it.
        self.assertFalse(_render(_task("T01")).endswith("\n"))
        self.assertFalse(_render().endswith("\n"))


class ExecutedFormTests(unittest.TestCase):
    """What the same command prints once there is execution to show."""

    def test_the_whole_rendering_of_an_executed_plan(self) -> None:
        evidence = (
            _entry("T01", "2026-09-10T09:00:00+00:00")
            + _entry("T02", "2026-09-10T10:30:00+00:00", exit_code=1)
            + _entry("T02", "2026-09-10T11:00:00+00:00")
        )
        output = _render(
            _task("T01", name="Read the code", status="DONE", effect={"kind": "none"}),
            _task(
                "T02",
                name="Write the change",
                status="DONE",
                depends_on=["T01"],
                effect={"kind": "local_write"},
            ),
            _task("T03", name="Open the pull request", status="RUNNING", depends_on=["T02"]),
            evidence=evidence,
            status="EXECUTING",
        )
        self.assertEqual(
            output,
            "\n".join(
                (
                    "2026-01-01-001 | EXECUTING | 3 tasks | 3 levels | effects: local_write 1, none 2",
                    "verification spans 2026-09-10 09:00 -> 11:00 (2h 00m) | 1 of 3 started tasks unmeasured",
                    "",
                    "  #  ID   Task                   Lv  Deps  Status   Effect       Auth",
                    "  1  T01  Read the code           0  -     DONE     none         -",
                    "  2  T02  Write the change        1  T01   DONE     local_write  -",
                    "  3  T03  Open the pull request   2  T02   RUNNING  none         -",
                    "",
                    "Authorization: nothing outstanding.",
                    "",
                    "Non-zero exit codes recorded:",
                    "  T02  1 of 2 recorded commands failed",
                )
            ),
        )

    def test_one_task_off_todo_is_enough_to_grow_the_status_column(self) -> None:
        self.assertIn("Status", _render(_task("T01", status="RUNNING")).splitlines()[2])

    def test_the_span_line_appears_only_once_something_is_measured(self) -> None:
        # A started task with no recorded command must not produce a span of zero, which would read as
        # work that took no time rather than as work whose verification was never recorded.
        self.assertNotIn("verification spans", _render(_task("T01", status="DONE")))

    def test_unmeasured_started_tasks_are_counted_in_the_summary_and_measured_ones_are_not(self) -> None:
        evidence = _entry("T01", "2026-09-10T09:00:00+00:00") + _entry("T02", "2026-09-10T10:00:00+00:00")
        summary = _render(_task("T01", status="DONE"), _task("T02", status="DONE"), evidence=evidence).splitlines()[1]
        self.assertEqual(summary, "verification spans 2026-09-10 09:00 -> 10:00 (1h 00m)")

    def test_the_plan_and_the_executed_form_agree_on_every_column_they_share(self) -> None:
        # The property that makes one command serve both moments: the second form adds a column, it
        # does not recompute the others. Two renderings that disagreed here would mean the summary a
        # user approved described a different plan from the one that ran.
        tasks = [
            _task("T01", effect={"kind": "local_write"}),
            _task("T02", depends_on=["T01"], authorization={"status": "pending"}, effect={"kind": "external"}),
        ]
        plan = _render(*tasks)
        executed = _render(*[dict(task, status="DONE") for task in tasks], status="EXECUTING")

        def shared(output: str) -> "list[dict[str, str]]":
            _, rows = _table(output)
            return [{name: cell for name, cell in row.items() if name != "Status"} for row in rows]

        self.assertEqual(shared(plan), shared(executed))
        self.assertNotIn("Status", _table(plan)[0])
        self.assertIn("Status", _table(executed)[0])


class ColumnBehaviourTests(unittest.TestCase):
    def test_a_long_task_name_is_truncated_and_marked(self) -> None:
        name = "N" * (TASK_NAME_DISPLAY_WIDTH + 20)
        cell = _render(_task("T01", name=name)).splitlines()[3]
        self.assertIn("N" * (TASK_NAME_DISPLAY_WIDTH - 3) + "...", cell)
        self.assertNotIn("N" * (TASK_NAME_DISPLAY_WIDTH + 1), cell)

    def test_a_name_that_exactly_fills_the_column_is_not_truncated(self) -> None:
        name = "N" * TASK_NAME_DISPLAY_WIDTH
        self.assertIn(name, _render(_task("T01", name=name)))
        self.assertNotIn("...", _render(_task("T01", name=name)))

    def test_an_empty_name_renders_as_a_dash_rather_than_a_blank_cell(self) -> None:
        # A blank cell in the middle of a row reads as a wrapped continuation line.
        _, rows = _table(_render(_task("T01", name="")))
        self.assertEqual(
            rows, [{"#": "1", "ID": "T01", "Task": "-", "Lv": "0", "Deps": "-", "Effect": "none", "Auth": "-"}]
        )

    def test_a_task_with_no_dependencies_shows_a_dash(self) -> None:
        # Never blank: a blank dependency cell is indistinguishable from a continuation row.
        self.assertEqual(_table(_render(_task("T01")))[1][0]["Deps"], "-")

    def test_a_long_dependency_list_wraps_into_continuation_rows(self) -> None:
        dependencies = ["T01", "T02", "T03", "T04", "T05", "T06"]
        output = _render(
            *[_task(dependency) for dependency in dependencies],
            _task("T07", name="Everything", depends_on=dependencies),
        )
        names, rows = _table(output)
        wrapped = rows[6:]
        self.assertGreater(len(wrapped), 1, "a six-id dependency list did not wrap at all")

        # The whole list survives the wrap, in order and with nothing invented.
        self.assertEqual("".join(row["Deps"] for row in wrapped), ",".join(dependencies))
        for row in wrapped:
            self.assertLessEqual(len(row["Deps"]), DEPENDENCY_DISPLAY_WIDTH + 1)

        # A continuation row carries the dependency column and nothing else, so it cannot be misread
        # as a task of its own.
        for continuation in wrapped[1:]:
            self.assertEqual(list(continuation), ["Deps"], continuation)
        self.assertEqual([row["ID"] for row in wrapped if "ID" in row], ["T07"])
        self.assertIn("Effect", names)

    def test_a_dependency_id_wider_than_the_column_is_left_whole_rather_than_cut(self) -> None:
        # A truncated task id is a wrong task id, which is worse than a wide column.
        wide = "T" + "0" * (DEPENDENCY_DISPLAY_WIDTH + 5)
        output = _render(_task(wide), _task("T02", depends_on=[wide]))
        self.assertIn(wide, output)
        self.assertNotIn("...", output)

    def test_the_number_and_level_columns_are_right_aligned(self) -> None:
        # Ten rows, so the `#` column is two characters wide and alignment becomes visible.
        # Ten rows, so the `#` column is two characters wide and alignment becomes observable.
        output = _render(*[_task(f"T{index:02d}") for index in range(1, 11)])
        lines = [line for line in output.splitlines() if line.startswith("  ")][:11]
        header, body = lines[0], lines[1:]
        _, rows = _table(output)
        self.assertEqual([row["#"] for row in rows], [str(index) for index in range(1, 11)])
        for column in ("#", "Lv"):
            edge = header.index(column) + len(column)
            with self.subTest(column=column):
                # Right-aligned means every value's last character sits directly under the header's.
                for line, row in zip(body, rows, strict=True):
                    self.assertTrue(line[:edge].endswith(row[column]), f"{line!r} against {column}")

    def test_not_required_authorization_renders_as_a_dash_and_every_other_status_by_name(self) -> None:
        # `not_required` is the common case and naming it in every row would bury the ones that matter.
        for status, expected in (
            ("not_required", "-"),
            ("pending", "pending"),
            ("explicit", "explicit"),
            ("denied", "denied"),
            ("deferred", "deferred"),
        ):
            with self.subTest(status=status):
                rows = _table(_render(_task("T01", authorization={"status": status})))[1]
                self.assertEqual(rows[0]["Auth"], expected)

    def test_an_unlevelled_task_shows_a_question_mark_rather_than_a_number(self) -> None:
        rows = _table(_render(_task("T01", depends_on=["T02"]), _task("T02", depends_on=["T01"])))[1]
        self.assertEqual([row["Lv"] for row in rows], ["?", "?"])

    def test_a_duration_is_rendered_in_the_coarsest_unit_that_still_says_something(self) -> None:
        for offset, expected in (
            ("2026-09-10T09:00:30+00:00", "30s"),
            ("2026-09-10T09:07:00+00:00", "7m"),
            ("2026-09-10T11:05:00+00:00", "2h 05m"),
            ("2026-09-12T09:00:00+00:00", "48h 00m"),
        ):
            with self.subTest(expected=expected):
                evidence = _entry("T01", "2026-09-10T09:00:00+00:00") + _entry("T01", offset)
                summary = _render(_task("T01", status="DONE"), evidence=evidence).splitlines()[1]
                self.assertTrue(summary.endswith(f"({expected})"), summary)


class WarningsBlockTests(unittest.TestCase):
    def test_the_block_speaks_about_authorization_even_when_nothing_is_outstanding(self) -> None:
        # A block that is sometimes silent cannot be trusted when it says nothing is waiting.
        self.assertTrue(_render(_task("T01")).endswith("\nAuthorization: nothing outstanding."))

    def test_every_status_that_cannot_proceed_is_named_and_the_ones_that_can_are_not(self) -> None:
        output = _render(
            _task("T01", authorization={"status": "pending"}),
            _task("T02", authorization={"status": "denied"}),
            _task("T03", authorization={"status": "deferred"}),
            _task("T04", authorization={"status": "explicit"}),
            _task("T05", authorization={"status": "not_required"}),
        )
        self.assertIn("Authorization outstanding for 3 of 5 tasks:", output)
        block = output.split("Authorization outstanding")[1]
        self.assertEqual(sorted(("T01", "T02", "T03")), sorted(line.split()[0] for line in block.splitlines()[1:]))

    def test_unplaceable_evidence_stamps_are_reported_rather_than_passed_over(self) -> None:
        # The workspace holds two hand-authored date-only stamps. Dropping them silently would report a
        # task as unmeasured with no hint that a stamp existed and could not be read.
        output = _render(_task("T01", status="DONE"), evidence=_entry("T01", "2026-08-28"))
        self.assertIn("Evidence entries that could not be placed on a timeline:", output)
        self.assertIn("  T01  1 of 1 entries carry no timezone-aware stamp", output)

    def test_an_edge_pointing_outside_the_plan_is_named(self) -> None:
        output = _render(_task("T01", depends_on=["T99"]))
        self.assertIn("Dependencies naming tasks that are not in the plan:", output)
        self.assertIn("  T01  depends on T99", output)

    def test_a_cycle_is_named_under_the_table_rather_than_only_shown_as_a_question_mark(self) -> None:
        output = _render(_task("T01", depends_on=["T02"]), _task("T02", depends_on=["T01"]))
        self.assertIn("Not levelled, so inside a dependency cycle or behind one: T01, T02", output)

    def test_a_clean_executed_plan_has_only_the_authorization_line(self) -> None:
        evidence = _entry("T01", "2026-09-10T09:00:00+00:00")
        output = _render(_task("T01", status="DONE"), evidence=evidence)
        for absent in ("Non-zero exit codes", "could not be placed", "not in the plan", "Not levelled"):
            self.assertNotIn(absent, output)


class ProjectReadingTests(unittest.TestCase):
    def _project(self, state: "dict[str, object]", evidence: "str | None" = None) -> Path:
        directory = Path(tempfile.mkdtemp())
        (directory / "project.json").write_text(json.dumps(state), encoding="utf-8")
        if evidence is not None:
            (directory / "evidence.md").write_text(evidence, encoding="utf-8")
        return directory

    def test_a_project_with_no_evidence_file_renders_every_task_as_unmeasured(self) -> None:
        # The pre-execution summary exists for exactly the moment before anything has run.
        graph = project_task_graph(self._project(_state(_task("T01"))))
        self.assertEqual(graph.measured_bounds(), (None, None))
        self.assertNotIn("verification spans", render_task_graph(graph))

    def test_evidence_is_read_when_it_is_there(self) -> None:
        directory = self._project(
            _state(_task("T01", status="DONE")), evidence=_entry("T01", "2026-09-10T09:00:00+00:00")
        )
        self.assertEqual(project_task_graph(directory).nodes[0].span.entries, 1)

    def test_schema_v4_project_uses_the_same_graph_fields(self) -> None:
        state = _state(_task("T01"))
        state["schema_version"] = 4
        self.assertEqual(["T01"], [node.id for node in project_task_graph(self._project(state)).nodes])

    def test_a_missing_directory_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            project_task_graph(Path(tempfile.mkdtemp()) / "absent")
        self.assertIn("does not exist", str(caught.exception))

    def test_a_pre_v3_project_is_refused_rather_than_rendered_as_a_table_of_unset(self) -> None:
        # The columns a reader would trust are v3 fields, so a v2 project would render as a finding.
        state = _state(_task("T01"))
        state["schema_version"] = 2
        with self.assertRaises(WorkspaceError) as caught:
            project_task_graph(self._project(state))
        self.assertIn("needs v3 or v4 fields", str(caught.exception))
        self.assertIn("migrate", str(caught.exception))


class SubcommandTests(unittest.TestCase):
    """The shipped subcommand, run as a process, because that is how a user meets it."""

    def _run(self, *arguments: object, check: bool = True) -> "subprocess.CompletedProcess[str]":
        command = [sys.executable, str(MANAGER), *[str(argument) for argument in arguments]]
        return subprocess.run(command, check=check, capture_output=True, text=True)

    def _project(self, state: "dict[str, object]") -> Path:
        directory = Path(tempfile.mkdtemp())
        (directory / "project.json").write_text(json.dumps(state), encoding="utf-8")
        return directory

    def test_the_subcommand_prints_the_renderer_output_and_exactly_one_newline(self) -> None:
        directory = self._project(_state(_task("T01")))
        result = self._run("show-graph", directory)
        self.assertEqual(result.stdout, render_task_graph(project_task_graph(directory)) + "\n")
        self.assertEqual(result.returncode, 0)

    def test_the_subcommand_writes_nothing_to_the_project(self) -> None:
        # It is read-only, and the step that runs it leaves no trace in the workspace by design.
        directory = self._project(_state(_task("T01")))
        before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
        self._run("show-graph", directory)
        after = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
        self.assertEqual(before, after)

    def test_the_dispatch_prints_in_process_exactly_what_the_launcher_prints(self) -> None:
        # The tests either side of this one run the launcher as a process, which is how a user meets it
        # — but coverage cannot see inside a subprocess, so the dispatch branch would read as untested
        # at `fail_under = 100`. Asserting the two agree makes this more than a coverage errand: it
        # pins that the module's dispatch and the shipped launcher print the same bytes.
        directory = self._project(_state(_task("T01")))
        stdout = io.StringIO()
        argv = ["manage", "show-graph", str(directory)]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            self.assertEqual(manage_workspace.main(), 0)
        self.assertEqual(stdout.getvalue(), self._run("show-graph", directory).stdout)

    def test_a_missing_project_exits_non_zero_with_a_message_and_not_a_traceback(self) -> None:
        result = self._run("show-graph", Path(tempfile.mkdtemp()) / "absent", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(PREDECESSOR.is_dir(), f"{PREDECESSOR} is not present")
class RealProjectTests(unittest.TestCase):
    """Against the closed predecessor: 15 tasks, 32 evidence entries, and a shape nobody invented."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = project_task_graph(PREDECESSOR)
        cls.output = render_task_graph(cls.graph)

    def test_the_summary_line_reports_the_project_as_it_actually_is(self) -> None:
        self.assertEqual(
            self.output.splitlines()[0],
            f"2026-09-10-002 | DONE | {len(self.graph.nodes)} tasks | {self.graph.levels} levels | "
            f"effects: {', '.join(f'{kind} {count}' for kind, count in sorted(self.graph.effect_counts().items()))}",
        )
        self.assertEqual(len(self.graph.nodes), 15)

    def test_a_closed_project_renders_the_executed_form(self) -> None:
        self.assertTrue(self.graph.executed)
        self.assertIn("Status", self.output.splitlines()[3])
        self.assertTrue(self.output.splitlines()[1].startswith("verification spans "))

    def test_every_task_appears_exactly_once_in_topological_order(self) -> None:
        table = [line for line in self.output.splitlines() if line.startswith("  ")][1:]
        ids = [cells[1] for cells in (line.split() for line in table) if len(cells) > 1 and cells[0].isdigit()]
        self.assertEqual(ids, [node.id for node in self.graph.nodes])
        self.assertEqual(len(set(ids)), 15)
        positions = {node.id: index for index, node in enumerate(self.graph.nodes)}
        for node in self.graph.nodes:
            for dependency in node.depends_on:
                self.assertLess(positions[dependency], positions[node.id])

    def test_no_row_is_wider_than_a_terminal_a_reader_would_have(self) -> None:
        # The renderer sizes columns from content, so a real project is the only thing that says
        # whether the result fits. 120 is this repository's own line-length discipline.
        for line in self.output.splitlines():
            self.assertLessEqual(len(line), 120, line)

    def test_the_subcommand_and_the_library_agree_on_the_real_project(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MANAGER), "show-graph", str(PREDECESSOR)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, self.output + "\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
