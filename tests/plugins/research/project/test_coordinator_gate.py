"""Coordinator mode: the finish gate in the shared transition path and the replan statement (T05, C2 and C3).

A policy-6 project with `coordinator` on finishes a task whose effect kind is `none` or `local_write`
only on a completed worker attempt for the task's current assignment, with no attempt still open; an
`external` or `destructive` task is exempt. Every finish carries a replan statement. The rules sit in
`transition_gate_errors`, so `update`, `reconcile` and `commit` are refused the same way, before any
document is written.
"""

from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_durability
import workspace_lib as lib
from workspace_durability import crash_loop_detail, durable_move
from workspace_lib import WorkspaceError, check_state_candidate, commit_state, validate_v4_state
from workspace_operations import task_operation
from workspace_session import _load_state, update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_durability_effects import make_running, manage
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_options import settings
from tests.plugins.research.project.test_packets import HEADING, _set
from tests.plugins.research.project.test_policy_five import _rewrite

COUNTER = itertools.count(1)
NEXT = {"next": "Finish the remaining tasks."}
G1 = (
    "coordinator mode: attempt {a} is still open (latest event {e}); record stopped for it with "
    "'workflow worker-event'"
)
G2 = (
    "coordinator mode needs a completed worker attempt for the current assignment (assignment_counter {c}) and "
    "found none; dispatch a fresh worker and record its events with 'workflow worker-event', or turn coordinator "
    "off with 'workflow settings' and the user's words"
)
R2 = (
    "coordinator mode needs a replan statement on a finished task; review the tasks that have not started, then use "
    "'task finish --replan none' or '--replan changed' with --replan-reason \"<one line>\""
)
R0 = (
    "--replan needs coordinator mode (policy_version 6 with options.coordinator on); this project stores no replan "
    "statement"
)
R1 = '--replan takes none or changed, with --replan-reason "<one line, at most 200 characters>"'
R3 = "--start-next is refused with --replan changed; finish first, then start the next task from the changed plan"
RA = "--replan applies only to task finish"
B0 = (
    "task {t} cannot finish: --backfill is refused in coordinator mode; start the task, dispatch a worker and use "
    "a plain task finish"
)
K1 = (
    "coordinator mode: effect.kind cannot change from {old} to {new} once the task has an assignment "
    "(assignment_counter {c}); a worker may not run an exempt step. Skip this task and add a new task for the "
    "{new} step"
)
C0 = (
    "assignment_counter is written by the tool: expected {n}, found {m}; leave it as the tool stamps it (task start, "
    "task finish and update write it)"
)
A1 = (
    "authorization can change only through 'workflow authorize' at policy 6; this {action} would set it to {status}. "
    "Create or reset a task with its default authorization, then authorize it with the user's words"
)


def _project(tmp_path: Path, policy: int) -> Path:
    """T01 external and authorized, T02 kind none; both TODO at `policy`, with a spec reference each."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = make_running(tmp_path, start=())
    _rewrite(project, policy)
    for task_id in ("T01", "T02"):
        _set(project, task_id, spec_refs=[HEADING])
    return project


@pytest.fixture
def six(tmp_path: Path) -> Path:
    return _project(tmp_path, 6)


def _state(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _task(project: Path, task_id: str) -> dict[str, Any]:
    return next(task for task in _state(project)["tasks"] if task["id"] == task_id)


def _revision(project: Path) -> int:
    return int(_state(project)["revision"])


def _start(project: Path, task_id: str) -> dict[str, Any]:
    return task_operation(project, "start", task_id, expected_revision=_revision(project))


def _event(project: Path, task_id: str, event: str, attempt: "str | None" = None) -> dict[str, Any]:
    extra = {"attempt_id": attempt} if attempt is not None else {}
    return workflow(project, "worker-event", request(
        project, f"we-{next(COUNTER)}", task=task_id, handle="w", event=event, scope="s", observation="o",
        assignment_revision=_revision(project), **extra,
    ))


def _worker(project: Path, task_id: str, *events: str) -> str:
    """Record an intent, then `events` on the attempt it opened; the attempt ID."""
    attempt = str(_event(project, task_id, "intent")["attempt_id"])
    for event in events:
        _event(project, task_id, event, attempt)
    return attempt


def _evidence(project: Path, task_id: str) -> str:
    return str(lib.record_evidence_result(project, task_id, [sys.executable, "-c", "pass"]).record_id)


def _finish(
    project: Path, task_id: str, replan: "str | None" = "none", reason: "str | None" = "nothing to change",
    **extra: Any,
) -> dict[str, Any]:
    record = extra.pop("record", None) or _evidence(project, task_id)
    return task_operation(
        project, "finish", task_id, expected_revision=_revision(project), evidence_record_ids=[record],
        replan=replan, replan_reason=reason, **extra,
    )


def _refused(project: Path, task_id: str, *findings: str, **extra: Any) -> str:
    with pytest.raises(WorkspaceError) as caught:
        _finish(project, task_id, **extra)
    message = str(caught.value)
    assert message.startswith(f"task {task_id} cannot finish: ")
    for finding in findings:
        assert finding in message
    return message


def _files(project: Path) -> dict[str, bytes]:
    return {str(path.relative_to(project)): path.read_bytes() for path in sorted(project.rglob("*")) if path.is_file()}


def _coordinator_off(project: Path) -> None:
    settings(project, options={"coordinator": False})


def _receipt(project: Path, task_id: str) -> None:
    body = {"kind": "publish", "value": "https://example.com/r/1", "destination": "web", "timestamp": lib.now_iso()}
    workflow(project, "receipt", request(project, f"rc-{next(COUNTER)}", task=task_id, receipt=body))


def _explicit() -> dict[str, Any]:
    return {
        "required": True, "status": "explicit", "scope": "Publish", "source": "user", "authorized_at": lib.now_iso(),
    }


def _default(kind: str) -> dict[str, Any]:
    required = kind in ("external", "destructive")
    return {
        "required": required, "status": "pending" if required else "not_required", "scope": None, "source": None,
        "authorized_at": None,
    }


# --- the finish gate ------------------------------------------------------------------------------------


def test_finish_is_refused_with_no_attempt(six: Path) -> None:
    _start(six, "T02")
    message = _refused(six, "T02", G2.format(c=1))
    assert "still open" not in message
    assert _task(six, "T02")["status"] == "RUNNING"


def test_a_direct_finish_of_a_task_that_never_ran_is_refused(six: Path) -> None:
    _refused(six, "T02", G2.format(c=0))


def test_finish_is_refused_with_an_attempt_for_an_earlier_assignment(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    edit = {"tasks": [{"id": "T02", "success_criteria": "changed"}]}
    update_project_data(six, edit, expected_revision=_revision(six))
    assert _task(six, "T02")["assignment_counter"] == 2
    _refused(six, "T02", G2.format(c=2))


def test_finish_is_refused_when_the_latest_event_is_failed(six: Path) -> None:
    _start(six, "T02")
    attempt = _worker(six, "T02", "failed")
    _refused(six, "T02", G1.format(a=attempt, e="failed"), G2.format(c=1))


def test_finish_is_refused_when_the_latest_event_is_stopped(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "stopped")
    message = _refused(six, "T02", G2.format(c=1))
    assert "still open" not in message


def test_finish_is_refused_with_a_completed_attempt_beside_an_open_one(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    second = _worker(six, "T02", "launched")
    message = _refused(six, "T02", G1.format(a=second, e="launched"))
    assert "needs a completed worker attempt" not in message


def test_finish_passes_with_a_completed_attempt_and_none_open(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    result = _finish(six, "T02")
    assert result["changed_tasks"] == [{"id": "T02", "status": "DONE"}]
    assert _task(six, "T02")["replan"] == {"statement": "none", "reason": "nothing to change"}


def test_an_external_task_finishes_without_an_attempt(six: Path) -> None:
    _start(six, "T01")
    _receipt(six, "T01")
    _finish(six, "T01")
    assert _task(six, "T01")["status"] == "DONE"


def test_a_destructive_task_finishes_without_an_attempt(six: Path) -> None:
    update_project_data(six, {"tasks": [{
        "id": "T03", "name": "Drop", "success_criteria": "ok", "verification": "run",
        "effect": {"kind": "destructive", "description": "Drop a table"}, "spec_refs": [HEADING],
    }]}, expected_revision=_revision(six))
    workflow(six, "authorize", request(six, "auth-t03", task="T03", authorization=_explicit()))
    _start(six, "T03")
    _finish(six, "T03")
    assert _task(six, "T03")["status"] == "DONE"


def test_backfill_is_refused_in_coordinator_mode(six: Path) -> None:
    before = _files(six)
    for task_id in ("T02", "T01"):  # the exempt task too
        with pytest.raises(WorkspaceError) as caught:
            task_operation(six, "finish", task_id, expected_revision=_revision(six), backfill=True, note="done early")
        assert str(caught.value) == B0.format(t=task_id)
    assert _files(six) == before


def test_skip_passes_without_an_attempt(six: Path) -> None:
    _start(six, "T02")
    task_operation(six, "skip", "T02", expected_revision=_revision(six), reason="not needed")
    assert _task(six, "T02")["status"] == "SKIPPED"


def test_a_damaged_worker_line_refuses_the_finish(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    notes = six / "tasks/T02.md"
    notes.write_text(notes.read_text(encoding="utf-8") + "<!-- research-worker {not json} -->\n", encoding="utf-8")
    message = _refused(six, "T02", "coordinator mode cannot read the worker attempts: tasks/T02.md line ")
    damage = "damaged research-worker line (not valid JSON); repair or remove that line in tasks/T02.md by hand"
    assert damage in message


def test_block_then_start_voids_a_completed_attempt(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    task_operation(six, "block", "T02", expected_revision=_revision(six), reason="waiting")
    _start(six, "T02")
    message = _refused(six, "T02", G2.format(c=2))
    assert "still open" not in message


def test_a_refused_finish_writes_no_attested_finish_record(six: Path) -> None:
    _start(six, "T02")
    observed = lib.record_observation(six, "T02", "looked", source="a page", result="passed").record_id
    before = _files(six)
    with pytest.raises(WorkspaceError, match="needs a completed worker attempt"):
        task_operation(
            six, "finish", "T02", expected_revision=_revision(six), observation_record_ids=[observed],
            replan="none", replan_reason="no change",
        )
    assert _files(six) == before


def test_coordinator_off_restores_the_old_finish(six: Path) -> None:
    _coordinator_off(six)
    _start(six, "T02")
    record = _evidence(six, "T02")
    task_operation(six, "finish", "T02", expected_revision=_revision(six), evidence_record_ids=[record])
    assert _task(six, "T02")["status"] == "DONE" and "replan" not in _task(six, "T02")


# --- the shared path ------------------------------------------------------------------------------------


def _done_patch(project: Path, task_id: str) -> dict[str, Any]:
    record = _evidence(project, task_id)
    anchor = {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{record}"}
    return {"tasks": [{"id": task_id, "status": "DONE", "evidence": [anchor]}]}


def test_update_cannot_set_a_gated_task_done(six: Path) -> None:
    _start(six, "T02")
    patch = _done_patch(six, "T02")
    before = _files(six)
    with pytest.raises(WorkspaceError) as caught:
        update_project_data(six, patch, expected_revision=_revision(six))
    assert f"task T02: {R2}" in str(caught.value)
    assert f"task T02: {G2.format(c=1)}" in str(caught.value)
    assert _files(six) == before


def test_update_cannot_set_an_exempt_task_done_either(six: Path) -> None:
    _start(six, "T01")
    _receipt(six, "T01")
    with pytest.raises(WorkspaceError) as caught:
        update_project_data(six, _done_patch(six, "T01"), expected_revision=_revision(six))
    assert f"task T01: {R2}" in str(caught.value) and "worker attempt" not in str(caught.value)


def test_reconcile_cannot_set_a_gated_task_done_and_writes_no_decision_line(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    patch = _done_patch(six, "T02")
    before = _files(six)
    with pytest.raises(WorkspaceError) as caught:
        workflow(six, "reconcile", request(six, "rec-1", patch=patch, decision="Mark T02 done", continuation=NEXT))
    assert f"task T02: {R2}" in str(caught.value)
    assert "needs a completed worker attempt" not in str(caught.value)
    assert _files(six) == before


def _candidate(project: Path, task_id: str, **fields: Any) -> dict[str, Any]:
    candidate = copy.deepcopy(_load_state(project))
    next(task for task in candidate["tasks"] if task["id"] == task_id).update(fields)
    candidate["current_tasks"] = [task["id"] for task in candidate["tasks"] if task["status"] == "RUNNING"]
    return candidate


def _done_fields(project: Path, task_id: str) -> dict[str, Any]:
    fields = _done_patch(project, task_id)["tasks"][0]
    del fields["id"]
    return {**fields, "replan": {"statement": "none", "reason": "by commit"}}


def test_commit_of_a_candidate_with_a_done_task_runs_the_same_gate(six: Path) -> None:
    _start(six, "T02")
    candidate = _candidate(six, "T02", **_done_fields(six, "T02"))
    with pytest.raises(WorkspaceError) as caught:
        commit_state(six, candidate, expected_revision=_revision(six))
    assert f"task T02: {G2.format(c=1)}" in str(caught.value) and "replan statement" not in str(caught.value)
    _worker(six, "T02", "completed")
    commit_state(six, _candidate(six, "T02", **_done_fields(six, "T02")), expected_revision=_revision(six))
    assert _task(six, "T02")["status"] == "DONE"


def test_commit_cannot_raise_the_counter_to_match_a_completed_attempt(six: Path) -> None:
    _start(six, "T02")
    line = {
        "task": "T02", "handle": "w", "event": "completed", "scope": "s", "observation": "o",
        "assignment_revision": 1, "attempt_id": "T02-2-1", "counter": 2, "recorded": lib.now_iso(),
    }
    (six / "tasks").mkdir(exist_ok=True)
    (six / "tasks/T02.md").write_text(f"# Task T02 notes\n\n<!-- research-worker {json.dumps(line)} -->\n")
    candidate = _candidate(six, "T02", assignment_counter=2, **_done_fields(six, "T02"))
    with pytest.raises(WorkspaceError) as caught:
        commit_state(six, candidate, expected_revision=_revision(six))
    assert f"task T02: {C0.format(n=1, m=2)}" in str(caught.value)


def test_commit_cannot_lower_the_counter_under_a_completed_attempt(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    update_project_data(six, {"tasks": [{"id": "T02", "verification": "again"}]}, expected_revision=_revision(six))
    candidate = _candidate(six, "T02", assignment_counter=1, **_done_fields(six, "T02"))
    with pytest.raises(WorkspaceError) as caught:
        commit_state(six, candidate, expected_revision=_revision(six))
    assert f"task T02: {C0.format(n=2, m=1)}" in str(caught.value)
    candidate = _candidate(six, "T02", assignment_counter=0)
    with pytest.raises(WorkspaceError, match=r"expected 2, found 0"):
        commit_state(six, candidate, expected_revision=_revision(six))


def test_a_task_whose_stored_counter_is_malformed_is_left_to_the_validator(six: Path) -> None:
    _set(six, "T02", assignment_counter="one")
    state = _state(six)
    errors = workspace_durability.mode_gate_errors(six, state, copy.deepcopy(state))
    assert not any("assignment_counter is written by the tool" in error for error in errors)


def test_every_existing_path_writes_the_stamped_counter(six: Path) -> None:
    _start(six, "T02")
    task_operation(six, "block", "T02", expected_revision=_revision(six), reason="wait")
    task_operation(six, "block", "T02", expected_revision=_revision(six), reason="wait again")
    _start(six, "T02")
    update_project_data(six, {"tasks": [{"id": "T02", "name": "Renamed"}]}, expected_revision=_revision(six))
    assert _task(six, "T02")["assignment_counter"] == 3
    _worker(six, "T02", "completed")
    _finish(six, "T02")
    assert _task(six, "T02")["status"] == "DONE"


def test_only_authorize_can_grant_consent(six: Path) -> None:
    update_project_data(six, {"tasks": [{
        "id": "T03", "name": "Publish", "success_criteria": "ok", "verification": "run",
        "effect": {"kind": "external", "description": "Publish"}, "spec_refs": [HEADING],
    }]}, expected_revision=_revision(six))
    grant = {"tasks": [{"id": "T03", "authorization": _explicit()}]}
    with pytest.raises(WorkspaceError) as caught:
        update_project_data(six, grant, expected_revision=_revision(six))
    assert f"task T03: {A1.format(action='update/commit', status='explicit')}" in str(caught.value)
    before = _files(six)
    with pytest.raises(WorkspaceError) as caught:
        workflow(six, "reconcile", request(six, "rec-grant", patch=grant, decision="grant", continuation=NEXT))
    assert A1.format(action="reconcile", status="explicit") in str(caught.value)
    assert _files(six) == before
    with pytest.raises(WorkspaceError, match="this update/commit would set it to explicit"):
        commit_state(six, _candidate(six, "T03", authorization=_explicit()), expected_revision=_revision(six))
    new = {"tasks": [{
        "id": "T04", "name": "Publish more", "success_criteria": "ok", "verification": "run",
        "effect": {"kind": "external", "description": "Publish"}, "authorization": _explicit(), "spec_refs": [HEADING],
    }]}
    with pytest.raises(WorkspaceError, match="task T04: authorization can change only"):
        update_project_data(six, new, expected_revision=_revision(six))
    workflow(six, "authorize", request(six, "auth-t03", task="T03", authorization=_explicit()))
    assert _task(six, "T03")["authorization"]["status"] == "explicit"


def test_a_reset_to_the_default_authorization_is_allowed(six: Path) -> None:
    update_project_data(
        six, {"tasks": [{"id": "T01", "authorization": _default("external")}]}, expected_revision=_revision(six)
    )
    assert _task(six, "T01")["authorization"]["status"] == "pending"
    update_project_data(six, {"tasks": [{
        "id": "T02", "effect": {"kind": "external", "description": "Publish"}, "authorization": _default("external"),
    }]}, expected_revision=_revision(six))
    assert _task(six, "T02")["effect"]["kind"] == "external"


def test_authorization_rules_are_unchanged_below_policy_six(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    update_project_data(
        project, {"tasks": [{"id": "T01", "authorization": _default("external")}]}, expected_revision=_revision(project)
    )
    update_project_data(project, {"tasks": [{"id": "T01", "authorization": _explicit()}]},
                        expected_revision=_revision(project))
    assert _task(project, "T01")["authorization"]["status"] == "explicit"
    assert workspace_durability.mode_gate_errors(project, _state(project), _state(project)) == []


def test_the_dry_run_reports_the_gate_findings(six: Path) -> None:
    _start(six, "T02")
    candidate = _candidate(six, "T02", **_done_fields(six, "T02"))
    report = check_state_candidate(six, candidate)
    assert f"task T02: {G2.format(c=1)}" in report.errors


def test_a_started_task_cannot_change_into_an_exempt_kind(six: Path) -> None:
    _start(six, "T02")
    change = {"tasks": [{"id": "T02", "effect": {"kind": "external", "description": "Publish"}}]}
    with pytest.raises(WorkspaceError) as caught:
        update_project_data(six, change, expected_revision=_revision(six))
    assert f"task T02: {K1.format(old='none', new='external', c=1)}" in str(caught.value)
    task_operation(six, "block", "T02", expected_revision=_revision(six), reason="wait")
    with pytest.raises(WorkspaceError, match=r"effect\.kind cannot change from none to external"):
        update_project_data(six, change, expected_revision=_revision(six))
    back = {"tasks": [{"id": "T02", "status": "TODO", "block_reason": None}]}
    update_project_data(six, back, expected_revision=_revision(six))
    destructive = {"tasks": [{"id": "T02", "effect": {"kind": "destructive", "description": "Drop"}}]}
    with pytest.raises(WorkspaceError, match=r"effect\.kind cannot change from none to destructive"):
        update_project_data(six, destructive, expected_revision=_revision(six))
    # a gated kind stays gated: none to local_write is no escape and is allowed
    update_project_data(six, {"tasks": [{"id": "T02", "effect": {"kind": "local_write", "description": "Edit"}}]},
                        expected_revision=_revision(six))


def test_a_task_with_no_assignment_yet_may_change_its_kind(six: Path) -> None:
    assert _task(six, "T02")["assignment_counter"] == 0
    update_project_data(six, {"tasks": [{
        "id": "T02", "effect": {"kind": "external", "description": "Publish"}, "authorization": _default("external"),
    }]}, expected_revision=_revision(six))
    assert _task(six, "T02")["effect"]["kind"] == "external"


def test_the_gate_tolerates_a_malformed_candidate(six: Path) -> None:
    state = _state(six)
    assert workspace_durability.mode_gate_errors(six, state, {**state, "tasks": "broken"}) == []
    odd = {**state, "tasks": [*state["tasks"], "not a task", {"id": 3}]}
    assert workspace_durability.mode_gate_errors(six, state, odd) == []


def test_the_gate_does_not_run_below_policy_six(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    _start(project, "T02")
    record = _evidence(project, "T02")
    task_operation(project, "finish", "T02", expected_revision=_revision(project), evidence_record_ids=[record])
    assert _task(project, "T02")["status"] == "DONE"


# --- the crash-loop text --------------------------------------------------------------------------------

LOOPS = [{"task": "T02", "resumes": 3}]
COORDINATOR_TEXT = (
    "resumed without progress: T02 3 times. Record a finding or evidence for real progress. In coordinator mode do "
    "not run task block then task start: a restart is a new assignment and voids a completed worker attempt. If the "
    "worker completed, finish the task; if its attempt is still open, record its outcome with 'workflow "
    "worker-event'; see references/durability.md"
)


def test_the_crash_loop_text_never_advises_a_restart_in_coordinator_mode(
    six: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert crash_loop_detail(LOOPS, True, True) == COORDINATOR_TEXT
    monkeypatch.setattr(workspace_durability, "crash_loops", lambda *args, **kwargs: LOOPS)
    assert durable_move(six, _load_state(six))["move_detail"] == COORDINATOR_TEXT
    _coordinator_off(six)
    assert "run task block then task start (a held attempt" in durable_move(six, _load_state(six))["move_detail"]


def test_the_crash_loop_text_is_unchanged_with_coordinator_off() -> None:
    assert crash_loop_detail(LOOPS) == crash_loop_detail(LOOPS, False, False) == (
        "resumed without progress: T02 3 times. Record a finding or evidence for real progress, or check it is not "
        "looping and run task block then task start; see references/durability.md"
    )
    assert "(a held attempt needs stopped before the next intent)" in crash_loop_detail(LOOPS, True)


# --- the replan statement -------------------------------------------------------------------------------


def test_finish_without_a_replan_statement_is_refused(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    message = _refused(six, "T02", R2, replan=None, reason=None)
    assert message == f"task T02 cannot finish: {R2}"


def test_the_replan_statement_is_stored_on_the_task(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    _finish(six, "T02", "changed", "  T03 split after the gate grew  ")
    assert _task(six, "T02")["replan"] == {"statement": "changed", "reason": "T03 split after the gate grew"}
    with pytest.raises(WorkspaceError, match="terminal task history is immutable"):
        commit_state(six, _candidate(six, "T02", replan={"statement": "none", "reason": "edit"}),
                     expected_revision=_revision(six))


def test_start_next_is_refused_with_changed(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    before = _files(six)
    with pytest.raises(WorkspaceError) as caught:
        task_operation(six, "finish", "T02", expected_revision=_revision(six), evidence_record_ids=["x"],
                       start_next="T01", replan="changed", replan_reason="T01 moved")
    assert str(caught.value) == R3
    assert _files(six) == before
    _receipt(six, "T01")  # --start-next with none is allowed
    _finish(six, "T02", start_next="T01")
    assert _task(six, "T01")["status"] == "RUNNING"


def test_an_exempt_task_needs_the_replan_statement_too(six: Path) -> None:
    _start(six, "T01")
    _receipt(six, "T01")
    assert _refused(six, "T01", replan=None, reason=None) == f"task T01 cannot finish: {R2}"


def test_update_cannot_write_replan(six: Path) -> None:
    patch = {"tasks": [{"id": "T02", "replan": {"statement": "none", "reason": "x"}}]}
    with pytest.raises(WorkspaceError, match="replan is written by task finish; it cannot be set"):
        update_project_data(six, patch, expected_revision=_revision(six))


def test_replan_is_refused_with_coordinator_off(six: Path, tmp_path: Path) -> None:
    five = _project(tmp_path / "five", 5)
    _coordinator_off(six)
    for project in (five, six):
        for replan, reason in (("none", "x"), (None, "x"), ("none", None)):
            with pytest.raises(WorkspaceError) as caught:
                task_operation(project, "finish", "T02", expected_revision=_revision(project),
                               evidence_record_ids=["x"], replan=replan, replan_reason=reason)
            assert str(caught.value) == R0
        with pytest.raises(WorkspaceError) as caught:  # --backfill with --replan: R0 with coordinator off
            task_operation(project, "finish", "T02", expected_revision=_revision(project), backfill=True,
                           note="n", replan="none", replan_reason="x")
        assert str(caught.value) == R0


def test_replan_applies_only_to_task_finish(six: Path) -> None:
    for action in ("start", "block", "skip"):
        with pytest.raises(WorkspaceError) as caught:
            task_operation(six, action, "T02", expected_revision=_revision(six), reason="r", replan="none",
                           replan_reason="x")
        assert str(caught.value) == RA
    with pytest.raises(WorkspaceError) as caught:
        task_operation(six, "start", "T02", expected_revision=_revision(six), replan_reason="x")
    assert str(caught.value) == RA


@pytest.mark.parametrize(
    ("replan", "reason"),
    [
        ("none", None), (None, "why"), ("maybe", "why"), ("none", ""), ("none", "   "), ("none", "two\nlines"),
        ("none", "carriage\rreturn"), ("none", "x" * 201), ("none", 5),
    ],
)
def test_a_replan_reason_must_be_one_line_of_at_most_200_characters(six: Path, replan: Any, reason: Any) -> None:
    _start(six, "T02")
    with pytest.raises(WorkspaceError) as caught:
        task_operation(six, "finish", "T02", expected_revision=_revision(six), evidence_record_ids=["x"],
                       replan=replan, replan_reason=reason)
    assert str(caught.value) == R1


def test_a_reason_of_exactly_200_characters_is_accepted(six: Path) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    _finish(six, "T02", "none", "x" * 200)
    assert _task(six, "T02")["replan"]["reason"] == "x" * 200


def test_the_validator_refuses_a_replan_on_a_task_that_is_not_done(six: Path, tmp_path: Path) -> None:
    good = {"statement": "none", "reason": "fine"}

    def errors(project: Path, **fields: Any) -> list[str]:
        state = _state(project)
        state["tasks"][1].update(fields)
        return validate_v4_state(state, project, check_files=False).errors

    assert "task T02: replan belongs only to a DONE task" in errors(six, replan=good)
    shape = (
        "task T02: replan must be an object with only statement (none or changed) and reason (one line of at most "
        "200 characters)"
    )
    for bad in ("none", {"statement": "none"}, {"statement": "later", "reason": "x"}, {**good, "extra": 1},
                {"statement": "none", "reason": "a\nb"}, {"statement": "none", "reason": " "},
                {"statement": "none", "reason": "x" * 201}):
        assert shape in errors(six, replan=bad, status="DONE")
    assert errors(six, replan=good, status="DONE", evidence=[]) == errors(six, status="DONE", evidence=[])
    five = _project(tmp_path / "five", 5)
    assert "task T02: replan needs policy_version 6" in errors(five, replan=good, status="DONE")


def test_the_task_command_takes_the_replan_flags(six: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _start(six, "T02")
    _worker(six, "T02", "completed")
    record = _evidence(six, "T02")
    code = manage("task", six, "finish", "T02", "--expected-revision", _revision(six), "--evidence", record,
                  "--replan", "changed", "--replan-reason", "T03 is no longer needed")
    assert code == 0 and json.loads(capsys.readouterr().out)["status"] == "EXECUTING"
    assert _task(six, "T02")["replan"] == {"statement": "changed", "reason": "T03 is no longer needed"}
    with pytest.raises(SystemExit):
        manage("task", six, "finish", "T01", "--expected-revision", _revision(six), "--replan", "maybe")
