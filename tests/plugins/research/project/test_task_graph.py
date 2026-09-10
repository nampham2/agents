"""Tests for the task-graph model and the evidence spans it annotates tasks with.

The model exists because the console summary and the closing report must not be two independent
readings of `project.json`. Both render one graph, so the properties worth pinning here are the ones
both surfaces inherit: a dependency level per task, an order that depends on nothing but the edges
and the ids, and a span per task derived from `evidence.md`.

Two of those properties are tolerances rather than features, and they are tolerances because of what
the workspace actually holds. `evidence.md` is appended to by hand as well as by tool, so a stamp
that is not a readable instant exists and must not crash the parser or drop the task; and a plan
naming a dependency that is not in the plan is a plan whose shape is still worth showing. Both are
tested from the failing side, since a tolerance nobody exercised is an assumption.

Timing is derived, never recorded: no task field holds a start or an end. So a span measures the
first-to-last recorded *verification* of a task, which is a lower bound on the work, and a task whose
verification was never recorded is unmeasured rather than instantaneous. That distinction is the one
these tests protect, because it is the one a reader of the report would otherwise get wrong.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from workspace_lib import (
    TASK_GRAPH_UNLEVELLED,
    EvidenceSpan,
    build_task_graph,
    parse_evidence_spans,
)

UTC = timezone.utc


def _task(task_id: str, **overrides: object) -> "dict[str, object]":
    """A task carrying only what the graph model reads, so a test says what it is about."""
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
    return {"project": project, "status": status, "tasks": list(tasks)}


def _entry(owner: str, command: str = "cmd", recorded: "str | None" = None, exit_code: "int | None" = 0) -> str:
    """One `evidence.md` entry in the form `record_evidence` writes."""
    lines = [f"## {owner} — {command}", ""]
    if recorded is not None:
        lines.append(f"- Recorded: {recorded}")
    lines.append("- Working directory: /tmp")
    if exit_code is not None:
        lines.append(f"- Exit code: {exit_code} ({'passed' if exit_code == 0 else 'failed'})")
    return "\n".join(lines) + "\n\n"


class DependencyLevelTests(unittest.TestCase):
    def test_a_task_with_no_prerequisite_is_level_zero(self) -> None:
        graph = build_task_graph(_state(_task("T01"), _task("T02")))
        self.assertEqual([(node.id, node.level) for node in graph.nodes], [("T01", 0), ("T02", 0)])
        self.assertEqual(graph.levels, 1)

    def test_a_chain_climbs_one_level_per_edge(self) -> None:
        graph = build_task_graph(
            _state(_task("T01"), _task("T02", depends_on=["T01"]), _task("T03", depends_on=["T02"]))
        )
        self.assertEqual([node.level for node in graph.nodes], [0, 1, 2])
        self.assertEqual(graph.levels, 3)

    def test_a_task_sits_one_past_its_deepest_dependency_not_its_first(self) -> None:
        # A diamond: T04 waits on both arms, so it belongs behind the longer one.
        graph = build_task_graph(
            _state(
                _task("T01"),
                _task("T02", depends_on=["T01"]),
                _task("T03", depends_on=["T02"]),
                _task("T04", depends_on=["T01", "T03"]),
            )
        )
        self.assertEqual({node.id: node.level for node in graph.nodes}, {"T01": 0, "T02": 1, "T03": 2, "T04": 3})

    def test_the_order_and_the_levels_do_not_depend_on_the_order_in_the_file(self) -> None:
        # The whole point of layering in code rather than by eye: two renderers over one model cannot
        # disagree, and neither can two orderings of the same plan.
        tasks = [_task("T01"), _task("T02", depends_on=["T01"]), _task("T03", depends_on=["T01"])]
        forwards = build_task_graph(_state(*tasks))
        backwards = build_task_graph(_state(*reversed(tasks)))
        self.assertEqual(
            [(node.id, node.level) for node in forwards.nodes],
            [(node.id, node.level) for node in backwards.nodes],
        )

    def test_every_dependency_precedes_its_dependent(self) -> None:
        graph = build_task_graph(
            _state(_task("T03", depends_on=["T01"]), _task("T01"), _task("T02", depends_on=["T03"]))
        )
        positions = {node.id: index for index, node in enumerate(graph.nodes)}
        for node in graph.nodes:
            for dependency in node.depends_on:
                self.assertLess(positions[dependency], positions[node.id], f"{dependency} after {node.id}")

    def test_a_dependency_that_is_not_in_the_plan_is_named_and_does_not_gate_the_level(self) -> None:
        # `commit` rejects this state, so it can only arrive from a hand-edited file. The shape is
        # still worth showing, next to the fact that one edge points nowhere.
        graph = build_task_graph(_state(_task("T01", depends_on=["T99"])))
        node = graph.nodes[0]
        self.assertEqual(node.level, 0)
        self.assertEqual(node.depends_on, ["T99"])
        self.assertEqual(node.unknown_depends_on, ["T99"])
        self.assertEqual(graph.cycle_members, [])

    def test_a_cycle_is_reported_rather_than_raised_and_its_members_sort_last(self) -> None:
        graph = build_task_graph(
            _state(_task("T01"), _task("T02", depends_on=["T03"]), _task("T03", depends_on=["T02"]))
        )
        self.assertEqual(graph.cycle_members, ["T02", "T03"])
        self.assertEqual([node.id for node in graph.nodes], ["T01", "T02", "T03"])
        self.assertEqual([node.level for node in graph.nodes[1:]], [TASK_GRAPH_UNLEVELLED] * 2)
        # An unplaceable task must not be counted as a level of its own, or a cycle would inflate the
        # depth the summary reports.
        self.assertEqual(graph.levels, 1)

    def test_a_task_behind_a_cycle_is_unplaceable_too(self) -> None:
        graph = build_task_graph(
            _state(_task("T01", depends_on=["T02"]), _task("T02", depends_on=["T03"]), _task("T03", depends_on=["T02"]))
        )
        self.assertEqual(graph.cycle_members, ["T01", "T02", "T03"])
        self.assertEqual(graph.levels, 0)

    def test_a_plan_with_no_tasks_is_a_graph_with_no_nodes(self) -> None:
        graph = build_task_graph(_state())
        self.assertEqual(graph.nodes, [])
        self.assertEqual(graph.levels, 0)
        self.assertEqual(graph.effect_counts(), {})
        self.assertEqual(graph.measured_bounds(), (None, None))
        self.assertFalse(graph.executed)


class MalformedStateTests(unittest.TestCase):
    """What the model does with state that never passed `commit`, since it is a reader of files."""

    def test_a_task_that_is_not_an_object_is_skipped(self) -> None:
        graph = build_task_graph(_state(_task("T01"), "not a task", 7, None))
        self.assertEqual([node.id for node in graph.nodes], ["T01"])

    def test_a_task_with_no_usable_id_is_skipped(self) -> None:
        graph = build_task_graph(_state(_task("T01"), _task(""), {"name": "no id"}, _task("   ")))
        self.assertEqual([node.id for node in graph.nodes], ["T01"])

    def test_missing_and_wrongly_typed_fields_read_as_empty_rather_than_raising(self) -> None:
        graph = build_task_graph(
            _state({"id": "T01", "depends_on": None, "effect": "local_write", "authorization": [], "receipts": 3})
        )
        node = graph.nodes[0]
        self.assertEqual((node.name, node.status, node.effect_kind, node.authorization_status), ("", "", "", ""))
        self.assertEqual((node.depends_on, node.receipts), ([], 0))

    def test_an_empty_dependency_entry_is_dropped_rather_than_becoming_an_unknown_edge(self) -> None:
        graph = build_task_graph(_state(_task("T01"), _task("T02", depends_on=["T01", "", "  ", None])))
        self.assertEqual(graph.nodes[1].depends_on, ["T01"])
        self.assertEqual(graph.nodes[1].unknown_depends_on, [])

    def test_the_project_and_status_come_from_the_state_and_default_to_empty(self) -> None:
        self.assertEqual(build_task_graph({}).project, "")
        self.assertEqual(build_task_graph({}).status, "")
        graph = build_task_graph(_state(project="2026-09-10-003", status="EXECUTING"))
        self.assertEqual((graph.project, graph.status), ("2026-09-10-003", "EXECUTING"))


class GraphSummaryTests(unittest.TestCase):
    def test_effects_are_counted_by_kind(self) -> None:
        graph = build_task_graph(
            _state(
                _task("T01", effect={"kind": "local_write"}),
                _task("T02", effect={"kind": "local_write"}),
                _task("T03", effect={"kind": "external"}),
            )
        )
        self.assertEqual(graph.effect_counts(), {"local_write": 2, "external": 1})

    def test_a_plan_nobody_has_started_has_no_execution_to_show(self) -> None:
        # This is what lets one command serve both moments instead of taking a mode flag.
        self.assertFalse(build_task_graph(_state(_task("T01"), _task("T02"))).executed)

    def test_one_task_off_todo_is_execution_to_show(self) -> None:
        self.assertTrue(build_task_graph(_state(_task("T01"), _task("T02", status="RUNNING"))).executed)
        self.assertTrue(build_task_graph(_state(_task("T01", status="SKIPPED"))).executed)

    def test_authorization_that_cannot_proceed_is_distinguished_from_authorization_that_can(self) -> None:
        graph = build_task_graph(
            _state(
                *(
                    _task(f"T0{index}", authorization={"status": status})
                    for index, status in enumerate(("pending", "denied", "deferred", "explicit", "not_required"))
                )
            )
        )
        self.assertEqual([node.needs_authorization for node in graph.nodes], [True, True, True, False, False])


class EvidenceSpanTests(unittest.TestCase):
    def test_an_empty_file_yields_no_spans(self) -> None:
        self.assertEqual(parse_evidence_spans(""), {})
        self.assertEqual(parse_evidence_spans("# Evidence\n\nProse, no entries.\n"), {})

    def test_entries_for_one_task_collapse_to_its_first_and_last_instant(self) -> None:
        markdown = (
            _entry("T01", recorded="2026-09-10T09:00:00+02:00")
            + _entry("T01", recorded="2026-09-10T11:30:00+02:00")
            + _entry("T01", recorded="2026-09-10T10:00:00+02:00")
        )
        span = parse_evidence_spans(markdown)["T01"]
        self.assertEqual(span.entries, 3)
        self.assertEqual(span.first, datetime(2026, 9, 10, 9, 0, tzinfo=timezone(timedelta(hours=2))))
        self.assertEqual(span.last, datetime(2026, 9, 10, 11, 30, tzinfo=timezone(timedelta(hours=2))))
        self.assertTrue(span.measured)
        self.assertEqual(span.seconds, 2.5 * 3600)

    def test_a_failing_exit_code_is_counted_and_a_passing_one_is_not(self) -> None:
        markdown = (
            _entry("T01", recorded="2026-09-10T09:00:00+00:00", exit_code=0)
            + _entry("T01", recorded="2026-09-10T09:05:00+00:00", exit_code=1)
            + _entry("T01", recorded="2026-09-10T09:10:00+00:00", exit_code=-9)
        )
        span = parse_evidence_spans(markdown)["T01"]
        self.assertEqual((span.entries, span.failed), (3, 2))

    def test_an_entry_with_no_exit_code_counts_as_an_entry_and_not_as_a_failure(self) -> None:
        span = parse_evidence_spans(_entry("T01", recorded="2026-09-10T09:00:00+00:00", exit_code=None))["T01"]
        self.assertEqual((span.entries, span.failed), (1, 0))

    def test_a_stamp_that_cannot_be_placed_leaves_the_entry_counted_and_the_task_unmeasured(self) -> None:
        # The workspace really holds two of these, hand-authored. A parser that dropped the task would
        # report a task nobody ran; one that raised would take the whole summary down with it.
        span = parse_evidence_spans(_entry("T60", recorded="2026-08-28"))["T60"]
        self.assertEqual((span.entries, span.unplaceable), (1, 1))
        self.assertFalse(span.measured)
        self.assertIsNone(span.seconds)

    def test_a_stamp_with_no_offset_is_refused_rather_than_assumed_to_be_local(self) -> None:
        # Mixing a naive instant with the aware ones is a TypeError far from its cause, and guessing
        # an offset places the task at a confidently wrong hour.
        span = parse_evidence_spans(_entry("T01", recorded="2026-09-10T09:00:00"))["T01"]
        self.assertEqual(span.unplaceable, 1)
        self.assertFalse(span.measured)

    def test_an_entry_with_no_recorded_line_at_all_is_unplaceable(self) -> None:
        span = parse_evidence_spans(_entry("T01", recorded=None))["T01"]
        self.assertEqual((span.entries, span.unplaceable), (1, 1))

    def test_a_readable_stamp_survives_an_unreadable_neighbour(self) -> None:
        markdown = _entry("T01", recorded="not a date") + _entry("T01", recorded="2026-09-10T09:00:00+00:00")
        span = parse_evidence_spans(markdown)["T01"]
        self.assertEqual((span.entries, span.unplaceable), (2, 1))
        self.assertTrue(span.measured)
        self.assertEqual(span.seconds, 0.0)

    def test_a_closure_step_is_keyed_by_its_own_name_and_not_mistaken_for_a_task(self) -> None:
        spans = parse_evidence_spans(_entry("report", recorded="2026-09-10T09:00:00+00:00") + _entry("T01"))
        self.assertEqual(sorted(spans), ["T01", "report"])

    def test_an_unrecorded_task_is_unmeasured_rather_than_instantaneous(self) -> None:
        graph = build_task_graph(_state(_task("T01")), _entry("T02", recorded="2026-09-10T09:00:00+00:00"))
        self.assertEqual(graph.nodes[0].span, EvidenceSpan())
        self.assertFalse(graph.nodes[0].span.measured)
        self.assertIsNone(graph.nodes[0].span.seconds)

    def test_the_graph_carries_the_window_every_measured_task_falls_inside(self) -> None:
        markdown = (
            _entry("T01", recorded="2026-09-10T09:00:00+00:00")
            + _entry("T02", recorded="2026-09-10T12:00:00+00:00")
            + _entry("T03", recorded="2026-08-28")
        )
        graph = build_task_graph(_state(_task("T01"), _task("T02"), _task("T03")), markdown)
        self.assertEqual(
            graph.measured_bounds(),
            (datetime(2026, 9, 10, 9, tzinfo=UTC), datetime(2026, 9, 10, 12, tzinfo=UTC)),
        )

    def test_a_graph_whose_tasks_are_all_unmeasured_has_no_window(self) -> None:
        graph = build_task_graph(_state(_task("T01")), _entry("T01", recorded="2026-08-28"))
        self.assertEqual(graph.measured_bounds(), (None, None))

    def test_receipts_are_counted_only_when_they_are_a_list(self) -> None:
        graph = build_task_graph(_state(_task("T01", receipts=[{"kind": "url"}, {"kind": "sha"}])))
        self.assertEqual(graph.nodes[0].receipts, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
