"""Automated mode: the pending offer in `resume`, `context` and the `task` results (T06, C4).

At policy 6 with `coordinator` on, the agent offers automated mode once: while the project has an agreed
plan (status PLANNING, EXECUTING or REVIEW), a task is ready or running, and the stored options hold no
`automated` key. The offer is a suffix of `move_detail`; `continue` and `stop_reason` do not change.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import workspace_durability
import workspace_session as session
from workspace_context import compact_context
from workspace_durability import durable_move
from workspace_operations import task_operation
from workspace_session import next_move, update_project_data, validated_project_context
from workspace_workflows import workflow

from tests.plugins.research.project.test_coordinator_gate import (
    _evidence,
    _project,
    _revision,
    _start,
    _state,
    _worker,
)
from tests.plugins.research.project.test_options import settings
from tests.plugins.research.project.test_packets import HEADING

OFFER = (
    "automated mode is not answered: ask the user once whether to turn it on, and for the two standing words; "
    "record the answer with 'workflow settings' (options.automated true or false)"
)


@pytest.fixture
def six(tmp_path: Path) -> Path:
    return _project(tmp_path, 6)


def offered(result: dict[str, Any]) -> bool:
    return str(result["move_detail"]).endswith("; " + OFFER)


def resume(project: Path) -> dict[str, Any]:
    return workflow(project, "resume", {})


def test_resume_reports_the_pending_offer(six: Path) -> None:
    moved = resume(six)
    assert moved["move_detail"] == "T01 is ready: start it; " + OFFER
    assert (moved["continue"], moved["stop_reason"]) == ("T01", None)
    assert session.automated_offer(_state(six))


def test_task_results_report_the_pending_offer(six: Path) -> None:
    started = _start(six, "T02")
    assert offered(started) and started["continue"] == "T02"
    _worker(six, "T02", "completed")
    finished = task_operation(
        six, "finish", "T02", expected_revision=_revision(six), evidence_record_ids=[_evidence(six, "T02")],
        replan="none", replan_reason="no change",
    )
    assert offered(finished)
    update_project_data(six, {"tasks": [{
        "id": "T03", "name": "More", "success_criteria": "ok", "verification": "run",
        "effect": {"kind": "none", "description": None}, "spec_refs": [HEADING],
    }]}, expected_revision=_revision(six))
    blocked = task_operation(six, "block", "T03", expected_revision=_revision(six), reason="waiting")
    assert offered(blocked)
    skipped = task_operation(six, "skip", "T03", expected_revision=_revision(six), reason="not needed")
    assert offered(skipped)


def test_no_offer_before_a_task_is_ready(six: Path) -> None:
    state = _state(six)
    waiting = copy.deepcopy(state)
    for task in waiting["tasks"]:
        task["status"] = "BLOCKED"
    assert not session.automated_offer(waiting)
    assert not offered(next_move(waiting))
    assert not session.automated_offer({**state, "tasks": []})
    assert next_move({**state, "tasks": []})["move_detail"] == "derive tasks from the agreed design"


def test_no_offer_while_aligning(six: Path) -> None:
    state = {**_state(six), "status": "ALIGNING"}
    assert not session.automated_offer(state)
    assert next_move(state)["stop_reason"] == "user-decision" and not offered(next_move(state))


def test_no_offer_in_a_blocked_project_with_a_ready_task(six: Path) -> None:
    blocked = {"status": "BLOCKED", "tasks": [{"id": "T02", "status": "BLOCKED", "block_reason": "waiting"}]}
    update_project_data(six, blocked, expected_revision=_revision(six))
    moved = resume(six)
    assert moved["continue"] == "T01" and not offered(moved)
    started = _start(six, "T01")
    assert started["status"] == "EXECUTING" and offered(started)


@pytest.mark.parametrize("answer", [True, False])
def test_the_offer_stops_once_a_key_is_stored(six: Path, answer: bool) -> None:
    settings(six, options={"automated": answer})
    assert not offered(resume(six))
    assert not session.automated_offer(_state(six))
    assert not offered(_start(six, "T02"))


def test_options_null_re_arms_the_offer(six: Path) -> None:
    settings(six, options={"automated": False})
    assert not offered(resume(six))
    settings(six, options=None)
    assert offered(resume(six))
    settings(six, options={"automated": True})
    settings(six, options=None)
    assert offered(resume(six))


def test_no_offer_with_coordinator_off(six: Path) -> None:
    settings(six, options={"coordinator": False})
    assert not session.automated_offer(_state(six))
    assert not offered(resume(six))
    settings(six, options={"coordinator": True})
    assert offered(resume(six))


def test_the_offer_ignores_options_that_are_not_an_object(six: Path) -> None:
    state = _state(six)
    assert session.automated_offer({**state, "options": {"reports": True}})
    assert not session.automated_offer({**state, "options": {"automated": False}})


def test_compact_context_carries_the_offer(six: Path) -> None:
    summary = compact_context(validated_project_context(six), _state(six), six)
    assert summary["next_move"]["move_detail"] == "T01 is ready: start it; " + OFFER


def test_a_crash_loop_hides_the_offer(six: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_durability, "crash_loops", lambda *_, **__: [{"task": "T02", "resumes": 3}])
    move = durable_move(six, _state(six))
    assert move["stop_reason"] == "blocked" and OFFER not in move["move_detail"]


def test_a_policy_five_task_start_result_is_unchanged(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    started = task_operation(project, "start", "T02", expected_revision=_revision(project))
    assert not {"continue", "stop_reason", "move_detail"} & started.keys()
    assert not session.automated_offer(_state(project))
