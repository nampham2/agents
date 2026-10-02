"""The task graph prefers a task's own stamps and falls back to evidence spans.

A task with one recorded command used to render as a zero-length span, because evidence stamps bound
the checks and not the work. Stamped tasks (new projects only) report their real span; everything
else renders exactly as before. New names are looked up when a test runs, not at import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import workspace_lib as lib

EVIDENCE = (
    "## T01 — python -c pass\n\n- Recorded: 2026-10-02T10:20:00+00:00\n- Exit code: 0 (passed)\n"
    "\n## T02 — python -c pass\n\n- Recorded: 2026-10-02T11:00:00+00:00\n- Exit code: 0 (passed)\n"
)


def state(tasks: list[dict[str, Any]], status: str = "EXECUTING") -> dict[str, Any]:
    base = {"depends_on": [], "evidence": [], "outputs": [], "effect": {"kind": "none"},
            "authorization": {"status": "not_required"}}
    return {"project": "2026-01-01-001", "title": "t", "status": status, "revision": 1,
            "tasks": [{**base, **task} for task in tasks]}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 2, hour, minute, tzinfo=timezone.utc)


def node(graph: lib.TaskGraph, task_id: str) -> lib.TaskGraphNode:
    return next(n for n in graph.nodes if n.id == task_id)


class TestStampsOverEvidence:
    def test_a_fully_stamped_task_spans_its_stamps_not_its_checks(self) -> None:
        tasks = [{"id": "T01", "name": "a", "status": "DONE",
                  "started_at": "2026-10-02T10:00:00+00:00", "finished_at": "2026-10-02T10:45:00+00:00"}]
        span = node(lib.build_task_graph(state(tasks), EVIDENCE), "T01").span
        assert (span.first, span.last, span.measured, span.stamped) == (at(10), at(10, 45), True, True)
        assert span.entries == 1, "the evidence counts are kept"

    def test_a_start_stamp_alone_keeps_the_evidence_end(self) -> None:
        tasks = [{"id": "T01", "name": "a", "status": "RUNNING", "started_at": "2026-10-02T10:00:00+00:00"}]
        span = node(lib.build_task_graph(state(tasks), EVIDENCE), "T01").span
        assert (span.first, span.last, span.measured) == (at(10), at(10, 20), True)

    def test_a_finish_stamp_alone_keeps_the_evidence_start(self) -> None:
        # A backfilled task: finished_at only, and its single check still fixes the start.
        tasks = [{"id": "T01", "name": "a", "status": "DONE", "finished_at": "2026-10-02T10:30:00+00:00"}]
        span = node(lib.build_task_graph(state(tasks), EVIDENCE), "T01").span
        assert (span.first, span.last, span.stamped) == (at(10, 20), at(10, 30), True)

    def test_an_unstamped_task_falls_back_to_its_evidence_unchanged(self) -> None:
        tasks = [{"id": "T02", "name": "b", "status": "DONE"}]
        span = node(lib.build_task_graph(state(tasks), EVIDENCE), "T02").span
        assert (span.first, span.last, span.stamped) == (at(11), at(11), False)

    def test_a_stamp_that_cannot_be_placed_is_ignored(self) -> None:
        tasks = [{"id": "T01", "name": "a", "status": "DONE", "started_at": "2026-10-02T10:00:00", "finished_at": 7}]
        span = node(lib.build_task_graph(state(tasks), EVIDENCE), "T01").span
        assert (span.first, span.last, span.stamped) == (at(10, 20), at(10, 20), False)

    def test_a_stamped_task_with_no_evidence_is_still_measured(self) -> None:
        tasks = [{"id": "T09", "name": "z", "status": "DONE",
                  "started_at": "2026-10-02T09:00:00+00:00", "finished_at": "2026-10-02T09:10:00+00:00"}]
        span = node(lib.build_task_graph(state(tasks), ""), "T09").span
        assert (span.entries, span.measured, span.first, span.last) == (0, True, at(9), at(9, 10))

    def test_the_source_span_is_not_mutated(self) -> None:
        spans = lib.parse_evidence_spans(EVIDENCE)
        before = (spans["T01"].first, spans["T01"].last)
        tasks = [{"id": "T01", "name": "a", "status": "DONE", "started_at": "2026-10-02T08:00:00+00:00"}]
        lib.build_task_graph(state(tasks), EVIDENCE)
        assert (spans["T01"].first, spans["T01"].last) == before


class TestRendering:
    def test_the_summary_says_task_spans_only_when_a_stamp_was_used(self) -> None:
        plain = state([{"id": "T01", "name": "a", "status": "DONE"}])
        unstamped = lib.render_task_graph(lib.build_task_graph(plain, EVIDENCE))
        assert "verification spans" in unstamped and "task spans" not in unstamped
        stamped_tasks = [{"id": "T01", "name": "a", "status": "DONE",
                          "started_at": "2026-10-02T10:00:00+00:00", "finished_at": "2026-10-02T10:45:00+00:00"}]
        stamped = lib.render_task_graph(lib.build_task_graph(state(stamped_tasks), EVIDENCE))
        assert "task spans 2026-10-02 10:00 -> 10:45 (45m)" in stamped

    def test_bounds_mix_stamped_and_evidence_tasks(self) -> None:
        tasks = [
            {"id": "T01", "name": "a", "status": "DONE",
             "started_at": "2026-10-02T09:30:00+00:00", "finished_at": "2026-10-02T10:10:00+00:00"},
            {"id": "T02", "name": "b", "status": "DONE"},
        ]
        graph = lib.build_task_graph(state(tasks), EVIDENCE)
        assert graph.measured_bounds() == (at(9, 30), at(11))
        rendered = lib.render_task_graph(graph)
        assert "task spans 2026-10-02 09:30 -> 11:00" in rendered
        assert "unmeasured" not in rendered
