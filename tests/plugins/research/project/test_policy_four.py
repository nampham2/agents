"""Policy 4: policy 3's rules plus durable task fields, a wait anchor and tool-managed counters (T03)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_durability import active_wait, waits
from workspace_lib import (
    ASSIGNMENT_FIELDS,
    POLICY_VERSIONS,
    WorkspaceError,
    has_review_rules,
    stamp_transitions,
    task_durable_enabled,
    validate_v4_state,
)
from workspace_operations import task_operation
from workspace_session import _load_state, prepare_update
from workspace_workflows import workflow

from tests.plugins.research.project.test_durability_effects import make_running
from tests.plugins.research.project.test_lifecycle_commands import request


def _rewrite(project: Path, policy: "int | None") -> None:
    """Move a freshly built project to `policy`, adding the durable keys at policy 4 as a new project has them."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if policy is not None:
        state["policy_version"] = policy
        state["delivery_target"] = "local"
    if policy == 4:
        for task in state["tasks"]:
            task["assignment_counter"] = 1 if task["status"] == "RUNNING" else 0
            task["block_generation"] = 1 if task["status"] == "BLOCKED" else 0
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def four(tmp_path: Path) -> Path:
    project = make_running(tmp_path)
    _rewrite(project, 4)
    return project


@pytest.fixture
def three(tmp_path: Path) -> Path:
    project = make_running(tmp_path)
    _rewrite(project, 3)
    return project


def _task(project: Path, task_id: str = "T01") -> dict[str, Any]:
    return next(task for task in _load_state(project)["tasks"] if task["id"] == task_id)


def _update(project: Path, *changes: dict[str, Any]) -> dict[str, Any]:
    return lib.commit_state(
        project,
        prepare_update(_load_state(project), {"tasks": list(changes)}),
        expected_revision=_load_state(project)["revision"],
    )


def _block(project: Path, task_id: str = "T01") -> None:
    task_operation(project, "block", task_id, expected_revision=_load_state(project)["revision"], reason="waiting")


def _start(project: Path, task_id: str = "T01") -> None:
    task_operation(project, "start", task_id, expected_revision=_load_state(project)["revision"])


def _wait(project: Path, identity: str) -> None:
    workflow(project, "wait", request(project, identity, task="T01", on="user reply"))


# --- the policy value and predicates -------------------------------------------------------------


def test_policy_four_is_valid_and_carries_policy_three_rules() -> None:
    assert 4 in POLICY_VERSIONS and lib.NEW_PROJECT_POLICY is None
    assert [has_review_rules(p) for p in range(5)] == [False, False, False, True, True]
    assert lib.delivery_rules({"created": "2026-11-16T10:00:00+00:00", "policy_version": 4}).policy == 4
    assert lib.is_goldfish_gated({"policy_version": 4}) is True


def test_task_durable_enabled_only_at_a_valid_policy_four() -> None:
    assert task_durable_enabled({"policy_version": 4}) is True
    assert not any(task_durable_enabled({"policy_version": p}) for p in (0, 1, 2, 3, 5, True, "4"))
    assert task_durable_enabled({}) is False
    assert task_durable_enabled([]) is False  # ty: ignore[invalid-argument-type]


def test_policy_four_keeps_policy_three_settings_and_amend_rules(four: Path) -> None:
    state = {**_load_state(four), "review_risk": "high"}
    assert not any("review_risk" in e for e in validate_v4_state(state, four, check_files=False).errors)
    assert lib.staging_enabled(_load_state(four)) is True


# --- the validator -----------------------------------------------------------------------------------


def test_a_policy_four_task_needs_both_keys(four: Path) -> None:
    state = _load_state(four)
    for name in ("block_generation", "assignment_counter"):
        broken = copy.deepcopy(state)
        del broken["tasks"][0][name]
        assert any(
            f"{name} is required at policy 4" in e for e in validate_v4_state(broken, four, check_files=False).errors
        )


@pytest.mark.parametrize("bad", [-1, True, 1.0, "1", None])
def test_a_key_must_be_a_non_negative_integer(four: Path, bad: object) -> None:
    state = _load_state(four)
    state["tasks"][0]["block_generation"] = bad
    assert any("must be a non-negative integer" in e for e in validate_v4_state(state, four, check_files=False).errors)


def test_a_blocked_task_needs_generation_one_or_more(four: Path) -> None:
    _block(four)
    state = _load_state(four)
    state["tasks"][0]["block_generation"] = 0
    assert any("block_generation of at least 1" in e for e in validate_v4_state(state, four, check_files=False).errors)


@pytest.mark.parametrize("policy", [2, 3])
def test_the_keys_are_errors_below_policy_four(tmp_path: Path, policy: int) -> None:
    project = make_running(tmp_path)
    _rewrite(project, policy)
    state = _load_state(project)
    state["tasks"][0]["block_generation"] = 0
    assert any(
        "block_generation needs policy_version 4" in e
        for e in validate_v4_state(state, project, check_files=False).errors
    )


# --- stamping, route by route -----------------------------------------------------------------------------


def test_stamp_transitions_is_pure_and_positive_listed() -> None:
    before = {"status": "TODO", "assignment_counter": 0, "block_generation": 0, "name": "a"}
    assert stamp_transitions(before, {**before, "status": "RUNNING"}) == {
        "block_generation": 0,
        "assignment_counter": 1,
    }
    assert stamp_transitions(before, {**before, "status": "BLOCKED"}) == {
        "block_generation": 1,
        "assignment_counter": 0,
    }
    assert stamp_transitions(None, {"status": "BLOCKED"}) == {"block_generation": 1, "assignment_counter": 0}
    assert set(ASSIGNMENT_FIELDS) == {
        "name",
        "depends_on",
        "outputs",
        "success_criteria",
        "verification",
        "effect",
        "reads",
    }
    for field in ("authorization", "receipts", "evidence", "started_at", "block_generation"):
        assert field not in ASSIGNMENT_FIELDS


def test_task_start_counts_each_entry_into_running(four: Path) -> None:
    assert _task(four, "T02")["assignment_counter"] == 0
    _start(four, "T02")
    assert _task(four, "T02")["assignment_counter"] == 1
    _block(four, "T02")
    _start(four, "T02")
    assert _task(four, "T02")["assignment_counter"] == 2


def test_task_block_always_bumps_the_generation_even_when_already_blocked(four: Path) -> None:
    _block(four)
    assert _task(four)["block_generation"] == 1
    _block(four)
    assert _task(four)["block_generation"] == 2


def test_finish_start_next_stamps_the_successor(four: Path) -> None:
    effect = {"kind": "none", "description": None}
    _update(four, {"id": "T03", "name": "n", "success_criteria": "s", "verification": "v", "effect": effect})
    _start(four, "T02")
    record = lib.record_evidence_result(four, "T02", [sys.executable, "-c", "pass"]).record_id
    task_operation(
        four,
        "finish",
        "T02",
        expected_revision=_load_state(four)["revision"],
        evidence_record_ids=[record],
        start_next="T03",
    )
    assert _task(four, "T02")["assignment_counter"] == 1 and _task(four, "T02")["status"] == "DONE"
    assert _task(four, "T03")["assignment_counter"] == 1 and _task(four, "T03")["status"] == "RUNNING"


def test_a_backfill_counts_its_start(four: Path) -> None:
    record = lib.record_evidence_result(four, "T02", [sys.executable, "-c", "pass"]).record_id
    task_operation(
        four,
        "finish",
        "T02",
        expected_revision=_load_state(four)["revision"],
        evidence_record_ids=[record],
        backfill=True,
        note="work was done first",
    )
    assert _task(four, "T02")["assignment_counter"] == 1 and _task(four, "T02")["status"] == "DONE"


def test_update_entering_blocked_or_running_stamps_once(four: Path) -> None:
    _update(four, {"id": "T01", "status": "BLOCKED", "block_reason": "x"})
    assert (_task(four)["block_generation"], _task(four)["assignment_counter"]) == (1, 1)
    _update(four, {"id": "T01", "status": "RUNNING", "block_reason": None})
    assert (_task(four)["block_generation"], _task(four)["assignment_counter"]) == (1, 2)


def test_a_block_reason_edit_on_a_blocked_task_keeps_the_generation(four: Path) -> None:
    _block(four)
    before = _task(four)["block_generation"]
    _update(four, {"id": "T01", "block_reason": "a better reason"})
    assert _task(four)["block_generation"] == before


@pytest.mark.parametrize(
    "change",
    [
        {"name": "renamed"},
        {"success_criteria": "new"},
        {"verification": "new"},
        {"effect": {"kind": "external", "description": "changed"}},
        {"outputs": []},
    ],
)
def test_an_assignment_edit_bumps_the_counter(four: Path, change: dict[str, Any]) -> None:
    if "outputs" in change:
        change = {"outputs": [{"root": "target", "path": "x.txt", "required": True}]}
    before = _task(four)["assignment_counter"]
    _update(four, {"id": "T01", **change})
    assert _task(four)["assignment_counter"] == before + 1


def test_authorize_and_receipts_are_not_assignment_edits(four: Path) -> None:
    before = _task(four)["assignment_counter"]
    authorization = {
        "required": True,
        "status": "explicit",
        "scope": "Publish more",
        "source": "user",
        "authorized_at": lib.now_iso(),
    }
    workflow(four, "authorize", request(four, "auth-again", task="T01", authorization=authorization))
    assert _task(four)["assignment_counter"] == before


def test_a_new_task_starts_with_both_keys(four: Path) -> None:
    effect = {"kind": "none", "description": None}
    base = {"name": "n", "success_criteria": "s", "verification": "v", "effect": effect}
    _update(
        four,
        {"id": "T09", **base},
        {"id": "T10", **base, "status": "RUNNING"},
        {"id": "T11", **base, "status": "BLOCKED", "block_reason": "b"},
    )
    found = [_task(four, name) for name in ("T09", "T10", "T11")]
    assert [(t["block_generation"], t["assignment_counter"]) for t in found] == [(0, 0), (0, 1), (1, 0)]


@pytest.mark.parametrize("field", ["block_generation", "assignment_counter"])
def test_a_caller_cannot_set_either_key(four: Path, field: str) -> None:
    with pytest.raises(WorkspaceError, match="generation and counter are tool-managed"):
        prepare_update(_load_state(four), {"tasks": [{"id": "T01", field: 9}]})
    with pytest.raises(WorkspaceError, match="generation and counter are tool-managed"):
        prepare_update(_load_state(four), {"tasks": [{"id": "T01", field: 9}]}, internal=True)


def test_preview_is_repeatable_and_does_not_double_count(four: Path) -> None:
    state = _load_state(four)
    patch = {"tasks": [{"id": "T01", "name": "renamed"}]}
    assert prepare_update(state, patch) == prepare_update(state, patch)
    assert state["tasks"][0]["assignment_counter"] == 1


# --- policy 3 writes nothing new ------------------------------------------------------------------------------


def test_a_policy_three_project_writes_no_new_field_on_any_route(three: Path) -> None:
    _block(three)
    _start(three)
    _update(three, {"id": "T01", "name": "renamed"}, {"id": "T02", "status": "RUNNING"})
    _update(
        three,
        {
            "id": "T09",
            "name": "n",
            "success_criteria": "s",
            "verification": "v",
            "effect": {"kind": "none", "description": None},
        },
    )
    _wait_ok = workflow(three, "resume", {})
    assert _wait_ok
    for task in _load_state(three)["tasks"]:
        assert "block_generation" not in task and "assignment_counter" not in task


# --- the wait anchor ----------------------------------------------------------------------------------------------


def test_a_wait_records_the_generation_and_is_active_at_that_generation(four: Path) -> None:
    _block(four)
    _wait(four, "wait-1")
    text = (four / "tasks/T01.md").read_text(encoding="utf-8")
    assert f'"generation": {_task(four)["block_generation"]}' in text
    assert [w["task"] for w in waits(four, _load_state(four))] == ["T01"]


def test_a_re_block_retires_the_earlier_wait(four: Path) -> None:
    _block(four)
    _wait(four, "wait-1")
    _block(four)
    assert waits(four, _load_state(four)) == []
    _wait(four, "wait-2")
    assert len(waits(four, _load_state(four))) == 1


def test_leaving_and_re_entering_blocked_through_update_retires_the_wait(four: Path) -> None:
    _block(four)
    _wait(four, "wait-1")
    _update(four, {"id": "T01", "status": "RUNNING", "block_reason": None})
    _update(four, {"id": "T01", "status": "BLOCKED", "block_reason": "again"})
    assert waits(four, _load_state(four)) == []


def test_a_block_reason_edit_keeps_the_wait(four: Path) -> None:
    _block(four)
    _wait(four, "wait-1")
    _update(four, {"id": "T01", "block_reason": "clearer"})
    assert len(waits(four, _load_state(four))) == 1


def test_an_entry_without_a_generation_is_malformed_at_policy_four(four: Path) -> None:
    _block(four)
    notes = four / "tasks/T01.md"
    notes.parent.mkdir(exist_ok=True)
    notes.write_text(
        (notes.read_text(encoding="utf-8") if notes.exists() else "")
        + '\n<!-- research-wait {"on": "x", "until": null, "reason": null, "recorded": "2026-11-17T10:00:00+00:00"} -->'
        + "\n",
        encoding="utf-8",
    )
    warnings: list[str] = []
    assert active_wait(four, _task(four), [], warnings) is None
    assert any("malformed research-wait" in w for w in warnings)


def test_a_missing_or_malformed_block_generation_on_a_row_means_no_wait(four: Path) -> None:
    _block(four)
    _wait(four, "wait-1")
    task = _task(four)
    task["block_generation"] = "2"
    assert active_wait(four, task, []) is None
    del task["block_generation"]
    assert active_wait(four, task, []) is not None  # the policy-3 log rule applies: an old project has no key


def test_the_dashboard_row_carries_block_generation(four: Path) -> None:
    import workspace_dashboard as dash

    _block(four)
    rows = dash._task_rows(_load_state(four)["tasks"], True)
    assert rows[0]["block_generation"] == _task(four)["block_generation"]
    plain = dash._task_rows([{"id": "T1", "status": "TODO"}])
    assert "block_generation" not in plain[0]


# --- review round 9 ---------------------------------------------------------------------------------------------


def _reconcile(project: Path, identity: str, *changes: dict[str, Any]) -> Any:
    return workflow(
        project,
        "reconcile",
        request(
            project,
            identity,
            patch={"tasks": list(changes)},
            decision="Reconciled in the test.",
            continuation={"next": "continue"},
        ),
    )


def test_reconcile_stamps_retires_the_wait_and_refuses_caller_values(four: Path) -> None:
    _reconcile(four, "rec-1", {"id": "T01", "status": "BLOCKED", "block_reason": "x"})
    assert (_task(four)["block_generation"], _task(four)["assignment_counter"]) == (1, 1)
    _wait(four, "wait-1")
    assert len(waits(four, _load_state(four))) == 1
    _reconcile(four, "rec-2", {"id": "T01", "status": "RUNNING", "block_reason": None})
    _reconcile(four, "rec-3", {"id": "T01", "status": "BLOCKED", "block_reason": "again"})
    assert _task(four)["block_generation"] == 2 and waits(four, _load_state(four)) == []
    with pytest.raises(WorkspaceError, match="generation and counter are tool-managed"):
        _reconcile(four, "rec-4", {"id": "T01", "block_generation": 9})


def test_a_stray_block_generation_below_policy_four_does_not_change_the_wait_rule(three: Path) -> None:
    import workspace_dashboard as dash
    from workspace_durability import active_wait

    _block(three)
    _wait(three, "wait-1")
    task = _task(three)
    task["block_generation"] = 7  # hand-edited, never validated on this path
    assert "generation" not in (three / "tasks/T01.md").read_text(encoding="utf-8")
    assert active_wait(three, task, [], durable=False) is not None  # the log rule decides, not the stray key
    assert "block_generation" not in dash._task_rows([task], False)[0]
    assert "block_generation" in dash._task_rows([task], True)[0]


def test_a_malformed_generation_at_policy_four_warns_and_means_no_wait(four: Path) -> None:
    from workspace_durability import active_wait

    _block(four)
    _wait(four, "wait-1")
    task = _task(four)
    task["block_generation"] = "2"
    warnings: list[str] = []
    assert active_wait(four, task, [], warnings, durable=True) is None
    assert any("block_generation is missing or not an integer" in w for w in warnings)


def test_the_crash_loop_advice_names_held_attempts_only_at_policy_four() -> None:
    from workspace_durability import crash_loop_detail

    loops = [{"task": "T01", "resumes": 3}]
    assert "held attempt" not in crash_loop_detail(loops)
    assert "record stopped for a held attempt first" in crash_loop_detail(loops, True)


def test_a_counter_cannot_decrease_and_a_running_task_needs_one(four: Path) -> None:
    previous = _load_state(four)
    lowered = copy.deepcopy(previous)
    lowered["tasks"][0]["assignment_counter"] = 0
    assert any(
        "assignment_counter cannot decrease (1 -> 0)" in e for e in lib.check_state_transition(previous, lowered)
    )
    assert any(
        "a RUNNING task needs assignment_counter of at least 1" in e
        for e in lib.validate_v4_state(lowered, four, check_files=False).errors
    )


def test_stamping_refuses_a_malformed_previous_value() -> None:
    with pytest.raises(WorkspaceError, match="cannot stamp a task whose assignment_counter"):
        stamp_transitions({"status": "TODO", "assignment_counter": "1"}, {"status": "RUNNING"})


def test_writing_an_empty_list_for_an_absent_one_is_not_an_assignment_edit() -> None:
    before = {"status": "RUNNING", "assignment_counter": 1, "block_generation": 0}
    assert stamp_transitions(before, {**before, "reads": [], "outputs": []})["assignment_counter"] == 1


def test_an_internal_patch_that_sets_the_policy_stamps_for_the_new_policy(tmp_path: Path) -> None:
    project = make_running(tmp_path)
    _rewrite(project, 3)
    state = prepare_update(
        _load_state(project),
        {"policy_version": 4, "tasks": [{"id": "T09", "name": "n", "success_criteria": "s", "verification": "v",
                                         "effect": {"kind": "none", "description": None}}]},
        internal=True,
    )  # fmt: skip
    new = next(t for t in state["tasks"] if t["id"] == "T09")
    assert (new["block_generation"], new["assignment_counter"]) == (0, 0)


def test_a_policy_three_wait_and_reconcile_write_no_new_field(three: Path) -> None:
    _block(three)
    _wait(three, "wait-1")
    assert '"generation"' not in (three / "tasks/T01.md").read_text(encoding="utf-8")
    _reconcile(three, "rec-1", {"id": "T01", "status": "RUNNING", "block_reason": None})
    assert all("block_generation" not in t and "assignment_counter" not in t for t in _load_state(three)["tasks"])


def test_policy_four_has_target_and_review_rules_in_checks_and_settings(four: Path) -> None:
    from workspace_checks import control_warnings
    from workspace_goldfish import _risk_of
    from workspace_workflows import _settings

    state = _load_state(four)
    assert lib.has_target_rules(4) is True
    assert _risk_of(state) == "standard" and _risk_of({**state, "policy_version": 2}) is None
    # policy 3 and later: a project with no code task started has nothing to prove can fail, so no warning
    assert control_warnings(four, state) == []
    patch, _body, _result = _settings(state, {"confirmation": {"source": "t", "response": "ok"}, "review_risk": "low"})
    assert patch["review_risk"] == "low"
