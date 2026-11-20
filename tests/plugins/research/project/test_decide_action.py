"""Automated mode: the `decide` action, its three outcomes, the pair states and the field rules (T06, C5).

With `automated` on, the agent records each choice it makes for the user with `workflow decide`. A record
with confidence at the threshold and evidence is a `decision`; a lesser one is an evidence round, and the
third lesser round of a kind and subject is `exhausted`, which stops the project for the user. The user
answers through the same action with a `confirmation`; the answer binds the pair while it stands.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
import workspace_session as session
import workspace_workflows
from workspace_lib import WorkspaceError, commit_state, validate_v4_state
from workspace_operations import task_operation
from workspace_session import next_move, update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_coordinator_gate import (
    _evidence,
    _files,
    _project,
    _revision,
    _start,
    _state,
    _task,
)
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_options import USER, settings

COUNTER = itertools.count(1)
ABSENT = object()
EXHAUSTED = (
    "automated decision exhausted after 3 evidence rounds: {items}; ask the user to choose and show them these "
    "options, then record the answer with 'workflow decide' and a confirmation"
)
REFUSED_EXHAUSTED = (
    "decision exhausted: {kind} for {subject} used 3 evidence rounds; only the user's answer is accepted: send by "
    "user with confirmation and the user's words"
)
ANSWERED = (
    "the user answered {kind} for {subject} with '{choice}'; the answer binds until {until}, a new user answer, or "
    "automated turned off"
)
TASK_UNTIL = "the task is finished or skipped"
PROJECT_UNTIL = "the project is reopened with workflow maintenance"
OFF = "decide needs options.automated on; with it off the choice is the user's"
EVIDENCE_RULE = (
    "evidence must be a list of 1 to 20 pointers: an evidence record ID (ev-<32 hex>) or a file of this project, "
    "optionally with #<anchor>; every agent round needs at least one"
)
USER_KEYS = "a user answer takes by, kind, subject, options, choice and confirmation only"
AGENT_NO_CONFIRMATION = "an agent record carries no confirmation; the user's words go in a by user answer"
RECORD_KEYS = {
    "id", "kind", "subject", "options", "choice", "evidence", "confidence", "proposer", "assessor", "round",
    "outcome", "recorded", "confirmation", "revision", "judged", "automated_off",
}


def automated(tmp_path: Path) -> Path:
    """The coordinator-gate project (T01 external and authorized, T02 kind none, both TODO) with automated on."""
    project = _project(tmp_path, 6)
    settings(project, options={"automated": True})
    return project


@pytest.fixture
def auto(tmp_path: Path) -> Path:
    return automated(tmp_path)


def decide(project: Path, by: str = "agent", **fields: Any) -> dict[str, Any]:
    """Record one decision; an agent record by default rates a skip of T02 at 0.95 on `spec.md`."""
    if by == "user":
        body: dict[str, Any] = {"kind": "task-change", "subject": "T02", "choice": "keep", "confirmation": USER}
    else:
        body = {
            "kind": "task-change", "subject": "T02", "choice": "skip", "evidence": ["spec.md"], "confidence": 0.95,
            "proposer": "coordinator", "assessor": "assessor-1",
        }
    body.update(fields)
    payload = {key: value for key, value in body.items() if value is not ABSENT}
    return workflow(project, "decide", request(project, f"d-{next(COUNTER)}", by=by, **payload))


def refused(project: Path, *texts: str, by: str = "agent", **fields: Any) -> str:
    """A refused `decide`: it writes nothing and its message holds every text."""
    before = _files(project)
    with pytest.raises(WorkspaceError) as caught:
        decide(project, by=by, **fields)
    assert _files(project) == before
    message = str(caught.value)
    for text in texts:
        assert text in message, message
    return message


def lesser(project: Path, times: int, **fields: Any) -> list[dict[str, Any]]:
    return [decide(project, confidence=0.5, **fields) for _ in range(times)]


def records(project: Path) -> list[dict[str, Any]]:
    return list(_state(project).get("automated_decisions", []))


def resume(project: Path) -> dict[str, Any]:
    return workflow(project, "resume", {})


def spec(project: Path) -> str:
    return (project / "spec.md").read_text(encoding="utf-8")


def edit(project: Path, task_id: str = "T02") -> None:
    """Edit an assignment field, which raises the task's assignment counter."""
    change = {"tasks": [{"id": task_id, "success_criteria": f"changed {next(COUNTER)}"}]}
    update_project_data(project, change, expected_revision=_revision(project))


def automated_off_and_on(project: Path) -> None:
    settings(project, options={"automated": False})
    settings(project, options={"automated": True})


# --- the three outcomes ---------------------------------------------------------------------------------


def test_a_qualifying_record_is_a_decision(auto: Path) -> None:
    revision = _revision(auto)
    result = decide(auto)
    record = result["decision"]
    assert result["outcome"] == "decision" and result["round"] == 1
    assert "rounds_left" not in result and "stop_reason" not in result and "move_detail" not in result
    assert set(record) == RECORD_KEYS
    assert record["options"] == ["skip", "keep"] and record["choice"] == "skip"
    assert record["judged"] == {"assignment_counter": 0}
    assert record["revision"] == revision and record["automated_off"] == 0
    assert record["assessor"] == "assessor-1" and record["confirmation"] is None
    assert records(auto) == [record]
    assert _revision(auto) == revision + 1


def test_an_agent_record_without_evidence_is_refused(auto: Path) -> None:
    refused(auto, EVIDENCE_RULE, evidence=[])
    refused(auto, "evidence", evidence=ABSENT)


def test_the_first_and_second_lesser_rounds_are_evidence_rounds(auto: Path) -> None:
    first, second = lesser(auto, 2)
    assert (first["outcome"], first["round"], first["rounds_left"]) == ("evidence-round", 1, 2)
    assert (second["outcome"], second["round"], second["rounds_left"]) == ("evidence-round", 2, 1)
    assert "stop_reason" not in second


def test_the_third_lesser_round_is_exhausted(auto: Path) -> None:
    third = lesser(auto, 3)[-1]
    assert (third["outcome"], third["round"], third["rounds_left"]) == ("exhausted", 3, 0)
    assert third["stop_reason"] == "user-decision"
    assert third["move_detail"] == EXHAUSTED.format(items="task-change for T02: 'skip' or 'keep'")
    assert third["decision"]["outcome"] == "exhausted"


def test_rounds_are_counted_per_kind_and_subject_whatever_the_id(auto: Path) -> None:
    lesser(auto, 2)
    # Another subject and another kind each start their own series.
    assert decide(auto, subject="T01", confidence=0.1)["round"] == 1
    assert decide(auto, kind="approach", options=["a", "b"], choice="a", confidence=0.1)["round"] == 1
    # Every call above used a new request ID; the T02 series still stands at two rounds.
    assert decide(auto, confidence=0.2)["outcome"] == "exhausted"


def test_a_qualifying_third_record_decides(auto: Path) -> None:
    lesser(auto, 2)
    third = decide(auto, confidence=0.9)
    assert (third["outcome"], third["round"]) == ("decision", 3)
    # A decision ends the series: the next agent record is round 1 again.
    assert decide(auto, confidence=0.4)["round"] == 1


def test_records_after_exhaustion_are_refused(auto: Path) -> None:
    lesser(auto, 3)
    refused(auto, REFUSED_EXHAUSTED.format(kind="task-change", subject="T02"))
    refused(auto, REFUSED_EXHAUSTED.format(kind="task-change", subject="T02"), confidence=0.99)


def test_exhaustion_survives_an_edit_and_a_restart_of_the_subject_task(auto: Path) -> None:
    lesser(auto, 3)
    edit(auto)
    refused(auto, REFUSED_EXHAUSTED.format(kind="task-change", subject="T02"))
    _start(auto, "T02")
    task_operation(auto, "block", "T02", expected_revision=_revision(auto), reason="waiting")
    _start(auto, "T02")
    assert _task(auto, "T02")["assignment_counter"] == 3
    refused(auto, REFUSED_EXHAUSTED.format(kind="task-change", subject="T02"))
    assert session.decision_state(_state(auto), "task-change", "T02")[0] == "exhausted"


def test_resume_and_task_start_report_user_decision_after_exhaustion(auto: Path) -> None:
    lesser(auto, 3)
    detail = EXHAUSTED.format(items="task-change for T02: 'skip' or 'keep'")
    moved = resume(auto)
    assert (moved["continue"], moved["stop_reason"], moved["move_detail"]) == (None, "user-decision", detail)
    edit(auto)
    assert resume(auto)["stop_reason"] == "user-decision"
    started = _start(auto, "T02")
    assert started["stop_reason"] == "user-decision" and started["continue"] is None
    # The running task stays visible after the exhausted text.
    assert started["move_detail"] == detail + "; T02 is running; keep working it"


def test_the_exhausted_stop_keeps_a_base_stop_and_a_running_task_in_the_detail(auto: Path) -> None:
    lesser(auto, 3)
    detail = EXHAUSTED.format(items="task-change for T02: 'skip' or 'keep'")
    state = _state(auto)
    # A ready task is the work the stop holds back: its detail is dropped.
    assert next_move(state)["move_detail"] == detail
    held = copy.deepcopy(state)
    held["tasks"][0]["authorization"] = {**held["tasks"][0]["authorization"], "status": "pending"}
    held["tasks"][1]["status"] = "BLOCKED"
    move = next_move(held)
    assert move["stop_reason"] == "user-decision"
    assert move["move_detail"] == detail + "; T01 needs the user's authorization for its effect"


def test_the_exhausted_detail_lists_every_pair_with_its_options(auto: Path) -> None:
    lesser(auto, 3)
    options = ["narrow", "wide", "both"]
    lesser(auto, 3, kind="scope", subject="project", options=options, choice="wide", assessor=ABSENT)
    items = "task-change for T02: 'skip' or 'keep'; scope for project: 'narrow', 'wide' or 'both'"
    assert resume(auto)["move_detail"] == EXHAUSTED.format(items=items)
    assert session.exhausted_pairs(_state(auto)) == [("task-change", "T02"), ("scope", "project")]


def test_no_exhausted_stop_with_automated_off(auto: Path) -> None:
    lesser(auto, 3)
    settings(auto, options={"automated": False})
    moved = resume(auto)
    assert moved["stop_reason"] is None and moved["continue"] == "T01"
    assert session.exhausted_pairs(_state(auto)) == []


# --- the user's answer ----------------------------------------------------------------------------------


def test_a_user_answer_is_accepted_before_any_record_during_rounds_and_after_exhaustion(auto: Path) -> None:
    assert decide(auto, by="user")["outcome"] == "decision"
    lesser(auto, 1, subject="T01")
    assert decide(auto, by="user", subject="T01", choice="skip")["decision"]["proposer"] == "user"
    free = {"kind": "scope", "subject": "project", "options": ["a", "b"], "choice": "a"}
    lesser(auto, 3, assessor=ABSENT, **free)
    answer = decide(auto, by="user", **{**free, "choice": "b"})
    assert answer["outcome"] == "decision" and answer["round"] is None
    assert session.decision_state(_state(auto), "scope", "project")[0] == "answered"


def test_a_user_answer_is_written_to_the_decision_history_and_clears_the_stop(auto: Path) -> None:
    lesser(auto, 3)
    result = decide(auto, by="user")
    record = result["decision"]
    assert "rounds_left" not in result and "stop_reason" not in result
    assert (record["proposer"], record["assessor"], record["confidence"], record["evidence"], record["round"]) == (
        "user", None, None, [], None
    )
    assert record["confirmation"] == USER and record["outcome"] == "decision"
    body = (
        "kind: automated-decision\ndecision: task-change for T02\nchoice: keep\noptions: skip, keep\n"
        f"source: {USER['source']}\nresponse: {USER['response']}\n"
    )
    assert body in spec(auto)
    assert "supersedes:" not in spec(auto)
    assert resume(auto)["stop_reason"] is None
    assert session.exhausted_pairs(_state(auto)) == []


def test_a_later_user_answer_supersedes_the_earlier_one(auto: Path) -> None:
    first = decide(auto, by="user")["decision"]
    decide(auto, by="user", choice="skip")
    assert f"choice: skip\noptions: skip, keep\nsource: {USER['source']}" in spec(auto)
    assert f"supersedes: {first['id']}\n" in spec(auto)
    state = _state(auto)
    assert session.decision_gate(state, "task-change", "T02", "skip") is None
    standing = session.decision_state(state, "task-change", "T02")[1]
    assert standing is not None and standing["choice"] == "skip"


def test_an_agent_record_is_refused_while_the_user_answer_stands(auto: Path) -> None:
    decide(auto, by="user")
    text = ANSWERED.format(kind="task-change", subject="T02", choice="keep", until=TASK_UNTIL)
    refused(auto, text)
    edit(auto)
    refused(auto, text)
    scope: dict[str, Any] = {"kind": "scope", "subject": "project", "options": ["narrow", "wide"], "assessor": ABSENT}
    decide(auto, by="user", choice="wide", **{**scope, "assessor": ABSENT})
    refused(
        auto, ANSWERED.format(kind="scope", subject="project", choice="wide", until=PROJECT_UNTIL),
        choice="narrow", **scope,
    )


def test_an_agent_decision_before_a_user_answer_never_counts_again(auto: Path) -> None:
    decide(auto)
    assert session.decision_gate(_state(auto), "task-change", "T02", "skip") is None
    decide(auto, by="user")
    automated_off_and_on(auto)
    text = session.decision_gate(_state(auto), "task-change", "T02", "skip")
    assert text == (
        "automated mode needs a valid decision: no current 'task-change' decision for T02; record one with "
        "'workflow decide' (choice 'skip')"
    )


def test_turning_automated_off_ends_exhaustion_the_answer_and_the_round_count(auto: Path) -> None:
    lesser(auto, 3)
    decide(auto, by="user", subject="T01")
    lesser(auto, 1, kind="replan", choice="changed", assessor=ABSENT)
    automated_off_and_on(auto)
    assert _state(auto)["mode_counters"] == {"automated_off": 1, "reopens": 0}
    assert decide(auto, confidence=0.5)["round"] == 1
    assert decide(auto, subject="T01", confidence=0.5)["round"] == 1
    assert decide(auto, kind="replan", choice="changed", assessor=ABSENT, confidence=0.5)["round"] == 1
    assert session.decision_state(_state(auto), "task-change", "T02")[0] == "rounds"


def test_an_agent_decision_survives_automated_off_and_on_while_valid(auto: Path) -> None:
    decide(auto)
    automated_off_and_on(auto)
    assert session.decision_gate(_state(auto), "task-change", "T02", "skip") is None
    task_operation(auto, "skip", "T02", expected_revision=_revision(auto), reason="decided")
    assert _task(auto, "T02")["status"] == "SKIPPED"


def test_an_exhausted_pair_is_dropped_when_its_task_is_terminal(auto: Path) -> None:
    lesser(auto, 3, kind="approach", options=["a", "b"], choice="a", assessor=ABSENT)
    assert resume(auto)["stop_reason"] == "user-decision"
    decide(auto)
    task_operation(auto, "skip", "T02", expected_revision=_revision(auto), reason="decided")
    assert session.exhausted_pairs(_state(auto)) == []
    assert resume(auto)["stop_reason"] is None
    refused(auto, "subject T02 is SKIPPED; a decision about a finished or skipped task has nothing left to decide")
    refused(
        auto, "subject T02 is SKIPPED; a decision about a finished or skipped task has nothing left to decide",
        by="user",
    )


# --- the request keys -----------------------------------------------------------------------------------


def test_a_listed_kind_may_leave_out_options(auto: Path) -> None:
    assert decide(auto)["decision"]["options"] == ["skip", "keep"]
    assert decide(auto, options=["keep", "skip"], subject="T01")["decision"]["options"] == ["skip", "keep"]
    replan = decide(auto, kind="replan", choice="none", assessor=ABSENT)["decision"]
    assert replan["options"] == ["changed", "none"]


def test_a_free_kind_needs_options(auto: Path) -> None:
    refused(auto, "options", kind="approach", choice="a", assessor=ABSENT)
    refused(auto, "options", by="user", kind="approach", choice="a")
    stored = decide(auto, kind="approach", options=["a", "b"], choice="b", assessor=ABSENT)["decision"]
    assert stored["options"] == ["a", "b"] and stored["assessor"] is None


def test_by_must_match_the_keys(auto: Path) -> None:
    refused(auto, "confirmation", by="user", confirmation=ABSENT)
    refused(auto, USER_KEYS, by="user", confidence=0.95)
    refused(auto, USER_KEYS, by="user", evidence=["spec.md"])
    refused(auto, AGENT_NO_CONFIRMATION, confirmation=USER)
    refused(
        auto,
        "by must be agent (a record of the agent's judgment) or user (the user's answer, with confirmation)",
        by="robot",
    )


def _anchor(project: Path) -> str:
    match = re.search(r'<a id="(o-\d+)"></a>', spec(project))
    assert match is not None
    return match[1]


def test_resolving_evidence_pointers(auto: Path) -> None:
    record = _evidence(auto, "T01")  # another task's record may be cited
    pointers = [record, "spec.md", f"spec.md#{_anchor(auto)}", "./spec.md"]
    assert decide(auto, evidence=pointers)["decision"]["evidence"] == pointers


@pytest.mark.parametrize(
    "pointer, reason",
    [
        ("ev-" + "0" * 32, "evidence record must match exactly one entry: ev-" + "0" * 32),
        ("missing.md", "not a file of this project"),
        ("../outside.md", "not a file of this project"),
        ("tasks", "not a file of this project"),
        ("spec.md#nope", 'spec.md has no id="nope"'),
    ],
)
def test_an_unresolvable_evidence_pointer_writes_nothing(auto: Path, pointer: str, reason: str) -> None:
    refused(auto, f"evidence pointer does not resolve: {pointer} ({reason})", evidence=["spec.md", pointer])


def test_an_ambiguous_evidence_record_does_not_resolve(auto: Path) -> None:
    record = _evidence(auto, "T01")
    path = auto / "evidence.md"
    text = path.read_text(encoding="utf-8")
    # An anchor that does not match the Record ID makes the entry ambiguous.
    path.write_text(text.replace(f'id="evidence-{record[3:]}"', 'id="evidence-' + "0" * 32 + '"'), encoding="utf-8")
    refused(
        auto, f"evidence pointer does not resolve: {record} (evidence record has malformed or ambiguous metadata",
        evidence=[record],
    )


def test_an_agent_record_with_proposer_user_writes_nothing(auto: Path) -> None:
    text = (
        "proposer 'user' is reserved for the user's answer; send by user with confirmation and the user's words "
        "instead"
    )
    refused(auto, text, proposer="user")
    refused(auto, text, proposer="User")
    refused(auto, text.replace("proposer", "assessor"), assessor="USER")


def test_a_gated_kind_with_a_choice_outside_its_pair_writes_nothing(auto: Path) -> None:
    refused(auto, "choice must be one of options", choice="drop")
    refused(
        auto, "kind task-change takes the options skip and keep; a gate reads no other choice",
        options=["skip", "drop"],
    )
    refused(
        auto, "kind project-close takes the options close and continue; a gate reads no other choice",
        by="user", kind="project-close", subject="project", choice="close", options=["close", "keep", "continue"],
    )


def test_a_consequential_kind_without_an_assessor_writes_nothing(auto: Path) -> None:
    for kind, choice in (("task-change", "skip"), ("task-accept", "accept"), ("project-close", "close")):
        # The printed schema row of a consequential kind requires assessor, so the key check names it first.
        refused(auto, "missing required field(s) ['assessor']", kind=kind, choice=choice, assessor=ABSENT)
    # The builder holds the same rule for a caller of the Python function.
    payload = request(
        auto, "direct", by="agent", kind="task-change", subject="T02", choice="skip", evidence=["spec.md"],
        confidence=0.95, proposer="coordinator",
    )
    with pytest.raises(WorkspaceError, match="kind task-change is consequential: assessor is required"):
        workspace_workflows._decide(auto, _state(auto), payload)


def test_an_assessor_equal_to_the_proposer_writes_nothing(auto: Path) -> None:
    refused(auto, "kind task-change is consequential: assessor must differ from proposer", assessor="coordinator")


def test_a_self_rated_kind_needs_no_assessor(auto: Path) -> None:
    replan = decide(auto, kind="replan", choice="changed", assessor=ABSENT)["decision"]
    assert replan["assessor"] is None and replan["outcome"] == "decision"
    free = decide(auto, kind="approach", options=["a", "b"], choice="a", assessor="helper-2")["decision"]
    assert free["assessor"] == "helper-2"


def test_a_project_close_record_is_refused_while_a_task_is_not_terminal(auto: Path) -> None:
    close = {"kind": "project-close", "subject": "project"}
    text = "project-close is refused while a task is not terminal: T01 TODO, T02 TODO"
    refused(auto, text, choice="close", **close)
    refused(auto, text, choice="continue", **close)
    refused(auto, text, by="user", choice="close", **close)
    assert decide(auto, by="user", choice="continue", **close)["outcome"] == "decision"


def test_decide_is_refused_with_automated_off(tmp_path: Path) -> None:
    project = _project(tmp_path, 6)
    refused(project, OFF)
    settings(project, options={"automated": True})
    settings(project, options={"automated": False})
    refused(project, OFF)


def test_decide_is_refused_below_policy_six(tmp_path: Path) -> None:
    refused(_project(tmp_path, 5), OFF)


def test_only_decide_writes_automated_decisions(auto: Path) -> None:
    decide(auto)
    with pytest.raises(WorkspaceError, match="unsupported update fields: automated_decisions"):
        update_project_data(auto, {"automated_decisions": []}, expected_revision=_revision(auto))
    candidate = {**_state(auto), "automated_decisions": []}
    with pytest.raises(WorkspaceError, match="candidate cannot change guarded field: automated_decisions"):
        commit_state(auto, candidate, expected_revision=_revision(auto))


def test_only_settings_and_maintenance_write_mode_counters(auto: Path) -> None:
    candidate = {**_state(auto), "mode_counters": {"automated_off": 0, "reopens": 0}}
    with pytest.raises(WorkspaceError, match="candidate cannot change guarded field: mode_counters"):
        commit_state(auto, candidate, expected_revision=_revision(auto))
    result = settings(auto, options={"automated": False})
    assert "mode_counters" not in result
    assert _state(auto)["mode_counters"] == {"automated_off": 1, "reopens": 0}
    assert "mode_counters" not in spec(auto)
    # Turning it off again, or a null that leaves it off, raises nothing.
    settings(auto, options={"automated": False})
    settings(auto, options=None)
    assert _state(auto)["mode_counters"] == {"automated_off": 1, "reopens": 0}
    settings(auto, options={"automated": True})
    settings(auto, options=None)
    assert _state(auto)["mode_counters"] == {"automated_off": 2, "reopens": 0}


def test_the_confirmation_caps_apply_to_decide_only(auto: Path) -> None:
    long_source = {"source": "s" * 201, "response": "keep"}
    refused(auto, "confirmation.source is longer than 200 characters", by="user", confirmation=long_source)
    long_response = {"source": "user", "response": "r" * 2001}
    refused(auto, "confirmation.response is longer than 2000 characters", by="user", confirmation=long_response)
    assert decide(auto, by="user", confirmation={"source": "s" * 200, "response": "r" * 2000})["outcome"] == "decision"
    settings(auto, options={"automated": True}, confirmation={"source": "s" * 300, "response": "r" * 3000})


@pytest.mark.parametrize(
    "fields, text",
    [
        ({"kind": "Bad Kind", "options": ["a", "b"], "choice": "a"}, "kind must be lowercase letters, digits and"),
        ({"kind": "a" * 41, "options": ["a", "b"], "choice": "a"}, "hyphens, at most 40 characters"),
        ({"kind": 7, "options": ["a", "b"], "choice": "a"}, "kind must be lowercase letters"),
        ({"subject": "T99"}, "subject must be a task ID of this project or 'project'"),
        ({"subject": "project"}, "kind task-change takes a task ID as subject"),
        (
            {"kind": "project-close", "subject": "T02", "choice": "close"},
            "kind project-close takes 'project' as subject",
        ),
        ({"options": "skip"}, "kind task-change takes the options skip and keep"),
        ({"kind": "x", "options": ["a"], "choice": "a", "assessor": ABSENT}, "options must be a list of 2 to 8"),
        ({"kind": "x", "options": ["a", "a"], "choice": "a", "assessor": ABSENT}, "options must be a list of 2 to 8"),
        ({"kind": "x", "options": ["a", "b" * 81], "choice": "a", "assessor": ABSENT}, "options must be a list"),
        ({"kind": "x", "options": ["a", "b\nc"], "choice": "a", "assessor": ABSENT}, "options must be a list"),
        ({"kind": "x", "options": ["a", " "], "choice": "a", "assessor": ABSENT}, "options must be a list"),
        ({"kind": "x", "options": ["a", 2], "choice": "a", "assessor": ABSENT}, "options must be a list"),
        ({"kind": "x", "options": list("abcdefghi"), "choice": "a", "assessor": ABSENT}, "options must be a list"),
        ({"kind": "x", "options": ["a", "b"], "choice": "c", "assessor": ABSENT}, "choice must be one of options"),
        ({"evidence": "spec.md"}, EVIDENCE_RULE),
        ({"evidence": ["spec.md"] * 21}, EVIDENCE_RULE),
        ({"evidence": ["s" * 201]}, EVIDENCE_RULE),
        ({"evidence": [""]}, EVIDENCE_RULE),
        ({"evidence": [7]}, EVIDENCE_RULE),
        ({"evidence": ["a#b#c"]}, EVIDENCE_RULE),
        ({"evidence": ["spec.md#"]}, EVIDENCE_RULE),
        ({"evidence": ["two\nlines"]}, EVIDENCE_RULE),
        ({"confidence": 1.5}, "confidence must be a number from 0 to 1"),
        ({"confidence": -0.1}, "confidence must be a number from 0 to 1"),
        ({"confidence": True}, "confidence must be a number from 0 to 1"),
        ({"confidence": "0.95"}, "confidence must be a number from 0 to 1"),
        (
            {"proposer": "bad label!"},
            "proposer must be a short agent label such as 'coordinator' or a subagent handle: letters, digits, "
            "'.', '_', ':' and '-', at most 64 characters",
        ),
        ({"proposer": "p" * 65}, "proposer must be a short agent label"),
        ({"proposer": 3}, "proposer must be a short agent label"),
        ({"assessor": "-starts-with-dash"}, "assessor must be a short agent label"),
        ({"kind": "x", "options": ["a", "b"], "choice": "a", "assessor": "no way"}, "assessor must be a short agent"),
    ],
)
def test_each_field_rule_refuses_and_writes_nothing(auto: Path, fields: dict[str, Any], text: str) -> None:
    refused(auto, text, **fields)


def test_whole_number_confidence_is_accepted(auto: Path) -> None:
    assert decide(auto, confidence=1)["outcome"] == "decision"
    assert decide(auto, subject="T01", confidence=0)["outcome"] == "evidence-round"


def test_a_malformed_confirmation_is_refused(auto: Path) -> None:
    refused(auto, "confirmation", by="user", confirmation={"source": "s"})


# --- readers and validator ------------------------------------------------------------------------------


def test_the_readers_tolerate_malformed_state(auto: Path) -> None:
    decide(auto)
    state = _state(auto)
    for broken in (
        {**state, "automated_decisions": "nope", "mode_counters": "nope"},
        {**state, "automated_decisions": [7, {"kind": "task-change"}], "mode_counters": {"automated_off": "x"}},
    ):
        assert session.exhausted_pairs(broken) == []
        assert session.decision_state(broken, "task-change", "T02")[0] == "open"
        assert session.decision_gate(broken, "task-change", "T02", "skip") is not None
    record = state["automated_decisions"][0]
    assert session.decision_valid(state, record)
    assert not session.decision_valid(state, {**record, "outcome": "evidence-round"})
    assert not session.decision_valid(state, {**record, "judged": "nope"})
    assert not session.decision_valid(state, {**record, "subject": "T99"})
    assert not session.decision_valid({**state, "tasks": "nope"}, {**record, "subject": "project"})
    assert next_move({**state, "status": "DONE"})["stop_reason"] == "closed"


def _errors(project: Path, **fields: Any) -> list[str]:
    state = {**_state(project), **fields}
    return validate_v4_state(state, project, check_files=False).errors


def test_the_validator_checks_the_shape_of_the_records(auto: Path) -> None:
    record = decide(auto)["decision"]
    assert _errors(auto) == []
    counters = {"automated_off": 0, "reopens": 0}
    assert _errors(auto, mode_counters=counters) == []
    bad = [
        ({"automated_decisions": "nope"}, "project: automated_decisions must be a list of decision records"),
        ({"automated_decisions": [7]}, "project: automated_decisions[0] must be an object with exactly the keys"),
        ({"automated_decisions": [{**record, "extra": 1}]}, "automated_decisions[0] must be an object with exactly"),
        ({"automated_decisions": [{**record, "outcome": "maybe"}]}, "automated_decisions[0]: outcome must be one of"),
        ({"automated_decisions": [{**record, "round": 4}]}, "automated_decisions[0]: round must be 1 to 3 or null"),
        ({"automated_decisions": [{**record, "round": True}]}, "automated_decisions[0]: round must be 1 to 3 or null"),
        ({"automated_decisions": [{**record, "automated_off": -1}]}, "automated_off must be a non-negative integer"),
        ({"automated_decisions": [{**record, "judged": None}]}, "automated_decisions[0]: judged must be an object"),
        ({"automated_decisions": [{**record, "id": ""}]}, "automated_decisions[0]: id must be non-empty text"),
        ({"automated_decisions": [{**record, "choice": "c" * 81}]}, "choice must be non-empty text of at most 80"),
        ({"automated_decisions": [{**record, "options": ["o" * 81, "b"]}]}, "options must be a list of strings"),
        ({"automated_decisions": [{**record, "evidence": ["e" * 201]}]}, "evidence must be a list of strings"),
        ({"automated_decisions": [{**record, "assessor": "a" * 65}]}, "assessor must be null or text of at most 64"),
        (
            {"automated_decisions": [{**record, "confirmation": {"source": "s" * 201, "response": "r"}}]},
            "confirmation must be null or a source of at most 200 and a response of at most 2000 characters",
        ),
        ({"mode_counters": "nope"}, "project: mode_counters must be an object with exactly automated_off and reopens"),
        ({"mode_counters": {"automated_off": 0}}, "mode_counters must be an object with exactly automated_off"),
        ({"mode_counters": {"automated_off": -1, "reopens": 0}}, "mode_counters must be an object with exactly"),
    ]
    for fields, text in bad:
        errors = _errors(auto, **fields)
        assert any(text in error for error in errors), (fields, errors)
    assert _errors(auto, automated_decisions=[{**record, "confirmation": USER, "round": None}]) == []


def test_the_fields_need_policy_six(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    errors = _errors(project, automated_decisions=[], mode_counters={"automated_off": 0, "reopens": 0})
    assert "project: automated_decisions needs policy_version 6" in errors
    assert "project: mode_counters needs policy_version 6" in errors
    stored = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert "automated_decisions" not in stored
