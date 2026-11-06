"""P2: three project resumes without progress on a RUNNING task stop the resume move.

Design: artifacts/architecture.md (project 2026-11-06-002) §4 "Crash-loop move" and §6 "Progress
and counting". The count is derived from `invocations.jsonl` by line position; resume stays read-only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from workspace_context import compact_context, infer_stop_reason, next_hint, start_project
from workspace_documents import append_record
from workspace_durability import crash_loops, durability_view, durable_move
from workspace_invocations import INVOCATIONS_FILENAME
from workspace_lib import WorkspaceError
from workspace_session import _load_state, next_move, validated_project_context
from workspace_workflows import workflow

from tests.plugins.research.project.test_durability_effects import lines, make_running, manage
from tests.plugins.research.project.test_lifecycle_commands import request

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def resume(at: Any = FUTURE, **extra: Any) -> dict[str, Any]:
    return {"at": at, "tool": "research-project", "command": "workflow", "action": "resume", "exit": 0, **extra}


def task_line(at: str = FUTURE, **extra: Any) -> dict[str, Any]:
    return {"at": at, "tool": "research-project", "command": "task", "action": "start", "exit": 0, **extra}


def log(project: Path, *entries: "dict[str, Any] | str") -> None:
    with (project / INVOCATIONS_FILENAME).open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")


def counts(project: Path, *, pending_resume: bool = False) -> dict[str, int]:
    found = crash_loops(project, _load_state(project), pending_resume=pending_resume)
    return {item["task"]: item["resumes"] for item in found}


def detail(*pairs: tuple[str, int]) -> str:
    named = ", ".join(f"{task} {count} times" for task, count in pairs)
    return (
        f"resumed without progress: {named}. Record a finding or evidence for real progress, or check it is not "
        "looping and run task block then task start; see references/durability.md"
    )


@pytest.fixture
def running(tmp_path: Path) -> Path:
    return make_running(tmp_path)


def test_three_resumes_stop_and_two_do_not(running: Path) -> None:
    log(running, resume())
    assert workflow(running, "resume", {})["continue"] == "T01"  # 1 logged + this one
    log(running, resume())
    resumed = workflow(running, "resume", {})  # 2 logged + this one
    assert resumed["continue"] is None and resumed["stop_reason"] == "blocked"
    assert resumed["move_detail"] == detail(("T01", 3))
    assert resumed["crash_loop"] == [{"task": "T01", "resumes": 3}]
    assert resumed["reference"] == "references/durability.md"
    assert counts(running) == {}  # without the pending resume, two is below the threshold
    assert counts(running, pending_resume=True) == {"T01": 3}


def test_packets_failed_calls_and_worker_packet_lines_do_not_count(running: Path) -> None:
    log(
        running,
        resume(),
        resume(),
        resume(exit=1, error="refused"),
        resume(worker_packet=True),
        {**resume(), "action": "packet"},
        {**resume(), "command": "context", "action": ""},
        "not json",
        "[1]",
        "",
    )
    assert counts(running, pending_resume=True) == {"T01": 3}
    assert counts(running) == {}
    # A packet, and a resume that carries worker keys, never adds the pending +1.
    packet = workflow(running, "packet", {"task": "T01", "worker": "w", "scope": "s", "lessons": []})
    assert packet["continue"] == "T01" and "crash_loop" not in packet
    carried = workflow(running, "resume", {"worker": "w"})
    assert carried["continue"] == "T01" and "crash_loop" not in carried
    assert workflow(running, "resume", {})["stop_reason"] == "blocked"


def test_an_upgrade_with_old_lines_needs_three_resumes_after_the_last_task_line(running: Path) -> None:
    old = {"version": "0.33.0"}
    log(running, *[resume(**old)] * 5, task_line(**old), resume(**old))
    assert counts(running, pending_resume=True) == {}
    assert workflow(running, "resume", {})["continue"] == "T01"
    log(running, resume(**old))
    assert workflow(running, "resume", {})["stop_reason"] == "blocked"


def test_a_successor_started_by_start_next_is_reset(tmp_path: Path) -> None:
    project = make_running(tmp_path, start=("T01", "T02"))
    log(project, resume(), resume(), resume())
    assert counts(project) == {"T01": 3, "T02": 3}
    log(project, task_line(action="finish", task="T09", start_next="T02"))
    assert counts(project) == {"T01": 3}


def test_same_second_lines_are_ordered_by_position(running: Path) -> None:
    same = "2099-01-01T00:00:00+00:00"
    log(running, resume(same), resume(same), resume(same), task_line(same, task="T01"), resume(same))
    assert counts(running, pending_resume=True) == {}
    log(running, resume(same))
    assert counts(running, pending_resume=True) == {"T01": 3}


@pytest.mark.parametrize("source", ["task", "receipt-line", "finding", "evidence", "start-next"])
def test_each_progress_source_resets_the_count(running: Path, source: str) -> None:
    log(running, *[resume(PAST)] * 4)
    assert counts(running) == {"T01": 4}
    if source == "task":
        log(running, task_line(task="T01"))
    elif source == "start-next":
        log(running, task_line(action="finish", task="T00", start_next="T01"))
    elif source == "receipt-line":
        log(running, {**resume(), "action": "receipt", "task": "T01"})
    elif source == "finding":
        append_record(running, "finding", "Found the real cause.", task_id="T01")
    else:
        workflow(
            running,
            "verify",
            {"id": "check-t01", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('ok')"]}]},
        )
    assert counts(running) == {}


def test_progress_lines_for_another_task_or_that_failed_do_not_reset(running: Path) -> None:
    log(running, *[resume()] * 3)
    log(
        running,
        task_line(task="T02"),
        task_line(task="T01", exit=1, error="refused"),
        {**resume(), "action": "receipt", "task": "T02"},
        {**resume(), "action": "effect", "task": "T01"},
    )
    assert counts(running) == {"T01": 3}


def test_a_resume_at_the_instant_of_a_finding_does_not_count(running: Path) -> None:
    append_record(running, "finding", "Recorded now.", task_id="T01")
    stamp = re.search(r"^### (\S+) — ", (running / "tasks/T01.md").read_text(encoding="utf-8"), re.MULTILINE)
    assert stamp is not None
    log(running, *[resume(stamp[1])] * 4, resume("not a time"), resume(5), resume("2099-01-01T00:00:00"))
    assert counts(running) == {}
    log(running, *[resume()] * 3)
    assert counts(running) == {"T01": 3}


def test_marker_only_entries_and_unparseable_stamps_are_not_findings(running: Path) -> None:
    workflow(running, "effect", request(running, "intent", operation="intent", task="T01", key="k", effect="e"))
    path = running / "tasks/T01.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n<!-- research-entry: odd -->\n### yesterday — odd\n\nbody\n"
        + "\n<!-- research-entry: bare -->\nno heading at all\n"
    )
    log(running, *[resume(PAST)] * 3)
    assert counts(running) == {"T01": 3}


def test_with_two_running_tasks_only_the_idle_one_is_named(tmp_path: Path) -> None:
    project = make_running(tmp_path, start=("T01", "T02"))
    log(project, resume(), resume(), task_line(task="T01"), resume())
    resumed = workflow(project, "resume", {})
    assert resumed["crash_loop"] == [{"task": "T02", "resumes": 4}]
    assert resumed["move_detail"] == detail(("T02", 4))
    log(project, task_line(task="T01"), resume(), resume())
    both = durable_move(project, _load_state(project), pending_resume=True)
    assert both["move_detail"] == detail(("T01", 3), ("T02", 6))


def test_a_checkpoint_after_two_resumes_does_not_infer_blocked_and_after_a_stop_it_does(running: Path) -> None:
    log(running, resume(), resume())
    continuation = {"next": "Go on", "session": "waiting"}
    with pytest.raises(WorkspaceError, match="next_move says continue T01"):
        workflow(running, "checkpoint", request(running, "cp-two", continuation=continuation))
    log(running, resume())
    result = workflow(running, "checkpoint", request(running, "cp-three", continuation=continuation))
    assert result["stop_reason_inferred"] == "blocked"
    state = _load_state(running)
    assert infer_stop_reason(running, state, continuation) == "blocked"
    assert next_hint(running, state) is None


def test_start_and_context_after_two_resumes_do_not_stop(running: Path, capsys: Any) -> None:
    log(running, resume(), resume())
    started, _ = start_project(None, project_dir=running)
    assert started["next_move"]["continue"] == "T01"
    compact = compact_context(validated_project_context(running), _load_state(running), running)
    assert compact["next_move"]["continue"] == "T01" and "crash_loop" not in compact
    assert next_hint(running, _load_state(running)) is not None
    log(running, resume())
    started, _ = start_project(None, project_dir=running)
    assert started["next_move"]["stop_reason"] == "blocked"
    assert started["next_move"]["move_detail"] == detail(("T01", 3))


def test_durable_move_without_loops_is_next_move(running: Path) -> None:
    state = _load_state(running)
    assert durable_move(running, state) == next_move(state)
    assert durability_view(running, state, pending_resume=False) == {}
    (running / INVOCATIONS_FILENAME).mkdir()  # unreadable log: no lines, no loop, no exception
    assert durable_move(running, state, pending_resume=True) == next_move(state)


def test_the_real_cli_path_writes_the_flags_and_stops_on_the_third_resume(tmp_path: Path, capsys: Any) -> None:
    project = make_running(tmp_path, start=())
    assert manage("task", project, "start", "T02", "--expected-revision", _load_state(project)["revision"]) == 0
    packet = project / "packet.json"
    packet.write_text(json.dumps({"worker": "w", "scope": "s", "lessons": []}))
    empty = project / "empty.json"
    empty.write_text("{}")
    assert manage("workflow", project, "resume", packet) == 0
    capsys.readouterr()
    moves = []
    for _ in range(3):
        assert manage("workflow", project, "resume", empty) == 0
        moves.append(json.loads(capsys.readouterr().out)["continue"])
    assert moves == ["T02", "T02", None]
    logged = lines(project)
    assert logged[0]["command"] == "task" and logged[0]["task"] == "T02" and "start_next" not in logged[0]
    assert logged[1]["action"] == "resume" and logged[1]["worker_packet"] is True
    assert all("worker_packet" not in line for line in logged[2:])
    record = workflow(
        project,
        "verify",
        {"id": "check-t02", "checks": [{"task": "T02", "argv": [sys.executable, "-c", "print('ok')"]}]},
    )["attempts"][0]["result"]["record_id"]
    revision = _load_state(project)["revision"]
    assert (
        manage(
            "task",
            project,
            "finish",
            "T02",
            "--expected-revision",
            revision,
            "--evidence",
            record,
            "--start-next",
            "T01",
        )
        == 0
    )
    assert lines(project)[-1]["start_next"] == "T01" and lines(project)[-1]["task"] == "T02"
    assert counts(project) == {}
    log(project, resume(), resume(), resume())
    body = {"kind": "publish", "value": "publish:v", "destination": "github", "timestamp": "2026-11-06T10:00:00+00:00"}
    receipt_file = project / "receipt.json"
    receipt_file.write_text(json.dumps(request(project, "cli-receipt", task="T01", receipt=body)))
    assert counts(project) == {"T01": 3}
    assert manage("workflow", project, "receipt", receipt_file) == 0
    assert counts(project) == {}
