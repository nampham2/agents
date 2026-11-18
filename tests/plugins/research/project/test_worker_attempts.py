"""Policy 4 worker attempts: tool-issued ids, the event-by-state table, supersession and held attempts (T05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from workspace_lib import WorkspaceError
from workspace_session import _load_state
from workspace_workflows import worker_events, workflow

from tests.plugins.research.project.test_durability_effects import make_running
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_policy_four import _rewrite, _task, _update


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


def event(project: Path, name: str, identity: str, task: str = "T01", **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "handle": "agent-1",
        "event": name,
        "scope": "T01 code",
        "observation": "seen",
        "assignment_revision": _load_state(project)["revision"],
    }
    fields.update(extra)
    return workflow(project, "worker-event", request(project, identity, task=task, **fields))  # type: ignore[no-any-return]


def notes(project: Path, task: str = "T01") -> str:
    path = project / f"tasks/{task}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def lines(project: Path, task: str = "T01") -> list[dict[str, Any]]:
    found = []
    for raw in notes(project, task).splitlines():
        if raw.startswith("<!-- research-worker "):
            found.append(json.loads(raw[len("<!-- research-worker ") : -len(" -->")]))
    return found


def refused(project: Path, name: str, identity: str, match: str, **extra: Any) -> None:
    before = notes(project)
    with pytest.raises(WorkspaceError, match=match):
        event(project, name, identity, **extra)
    assert notes(project) == before


def open_attempt(project: Path) -> str:
    return str(event(project, "intent", "intent-1")["attempt_id"])


# --- ids ---------------------------------------------------------------------------------------


def test_intent_issues_an_id_bound_to_the_task_and_counter(four: Path) -> None:
    attempt = open_attempt(four)
    assert attempt == "T01-1-1"
    assert lines(four)[-1]["attempt_id"] == attempt and lines(four)[-1]["counter"] == 1


def test_a_second_attempt_after_stopped_gets_the_next_number(four: Path) -> None:
    first = open_attempt(four)
    event(four, "stopped", "stop-1", attempt_id=first)
    assert event(four, "intent", "intent-2")["attempt_id"] == "T01-1-2"


def test_a_caller_cannot_supply_an_id_at_intent(four: Path) -> None:
    refused(four, "intent", "intent-x", "issued by the tool", attempt_id="T01-1-1")


def test_a_non_intent_event_needs_an_attempt_id_at_policy_four(four: Path) -> None:
    refused(four, "observed", "obs-x", "attempt_id is required")


def test_an_unknown_attempt_is_refused_for_every_event(four: Path) -> None:
    for name in ("launched", "observed", "completed", "failed", "stopped"):
        refused(four, name, f"u-{name}", "unknown attempt", attempt_id="T01-1-9")


def test_intent_needs_a_running_task(four: Path) -> None:
    before = notes(four, "T02")
    with pytest.raises(WorkspaceError, match="task is not RUNNING"):
        event(four, "intent", "intent-t02", task="T02")
    assert notes(four, "T02") == before


# --- the event table ---------------------------------------------------------------------------


def _drive(project: Path, path: list[str]) -> str:
    attempt = open_attempt(project)
    for index, name in enumerate(path):
        event(project, name, f"drive-{index}", attempt_id=attempt)
    return attempt


ALLOWED = {
    "intent": ["launched", "observed", "completed", "failed", "stopped"],
    "launched": ["observed", "completed", "failed", "stopped"],
    "observed": ["observed", "completed", "failed", "stopped"],
    "failed": ["stopped"],
}
PATHS = {"intent": [], "launched": ["launched"], "observed": ["observed"], "failed": ["failed"]}


@pytest.mark.parametrize("state", sorted(ALLOWED))
@pytest.mark.parametrize("name", ["launched", "observed", "completed", "failed", "stopped"])
def test_the_event_table(four: Path, state: str, name: str) -> None:
    attempt = _drive(four, PATHS[state])
    if name in ALLOWED[state]:
        event(four, name, "next", attempt_id=attempt)
        assert lines(four)[-1]["event"] == name
    else:
        refused(four, name, "next", f"event not valid from {state}", attempt_id=attempt)


@pytest.mark.parametrize("terminal", ["completed", "stopped"])
def test_a_terminal_attempt_accepts_nothing(four: Path, terminal: str) -> None:
    attempt = _drive(four, [terminal])
    for name in ("observed", "completed", "failed", "stopped"):
        refused(four, name, f"t-{name}", "attempt already terminal", attempt_id=attempt)


# --- held attempts -----------------------------------------------------------------------------


def test_a_new_intent_is_refused_while_any_attempt_is_held(four: Path) -> None:
    attempt = _drive(four, ["failed"])
    refused(four, "intent", "intent-2", "held attempt blocks a new intent")
    event(four, "stopped", "stop", attempt_id=attempt)
    assert event(four, "intent", "intent-3")["attempt_id"] == "T01-1-2"


def test_a_completed_attempt_does_not_hold(four: Path) -> None:
    _drive(four, ["completed"])
    assert event(four, "intent", "intent-2")["attempt_id"] == "T01-1-2"


def test_held_never_blocks_finish_or_block(four: Path) -> None:
    from workspace_operations import task_operation

    _drive(four, ["observed"])
    task_operation(four, "block", "T01", expected_revision=_load_state(four)["revision"], reason="waiting")
    assert _task(four)["status"] == "BLOCKED"


# --- supersession ------------------------------------------------------------------------------


def test_an_assignment_edit_supersedes_and_only_stopped_is_accepted(four: Path) -> None:
    attempt = _drive(four, ["launched"])
    _update(four, {"id": "T01", "name": "renamed"})
    for name in ("observed", "completed", "failed", "launched"):
        refused(four, name, f"s-{name}", "superseded attempt", attempt_id=attempt)
    event(four, "stopped", "stop", attempt_id=attempt)
    # the new intent carries the new counter
    assert event(four, "intent", "intent-2")["attempt_id"] == "T01-2-2"


def test_block_then_start_supersedes(four: Path) -> None:
    from workspace_operations import task_operation

    attempt = _drive(four, ["observed"])
    task_operation(four, "block", "T01", expected_revision=_load_state(four)["revision"], reason="x")
    task_operation(four, "start", "T01", expected_revision=_load_state(four)["revision"])
    refused(four, "observed", "o", "superseded attempt", attempt_id=attempt)


def test_authorization_does_not_supersede(four: Path) -> None:
    import workspace_lib as lib

    attempt = _drive(four, ["observed"])
    authorization = {
        "required": True,
        "status": "explicit",
        "scope": "More",
        "source": "user",
        "authorized_at": lib.now_iso(),
    }
    workflow(four, "authorize", request(four, "auth-2", task="T01", authorization=authorization))
    event(four, "completed", "done", attempt_id=attempt)


# --- policy 3 is unchanged ---------------------------------------------------------------------


def test_policy_three_refuses_attempt_id_and_writes_the_old_line(three: Path) -> None:
    refused(three, "observed", "o-1", "attempt_id needs policy_version 4", attempt_id="T01-1-1")
    event(three, "intent", "i-1")
    event(three, "observed", "o-2")
    assert all(
        set(line) == {"task", "handle", "event", "scope", "observation", "assignment_revision", "recorded"}
        for line in lines(three)
    )
    assert [item["event"] for item in worker_events(three)] == ["observed"]


def test_policy_three_drops_failed_as_before(three: Path) -> None:
    event(three, "failed", "f-1")
    assert worker_events(three) == []


# --- the resume listing ------------------------------------------------------------------------


def test_resume_lists_a_held_attempt_with_its_reason(four: Path) -> None:
    attempt = _drive(four, ["failed"])
    listed = workflow(four, "resume", {})["unresolved_workers"]
    assert [(item["attempt_id"], item["reason"], item["superseded"]) for item in listed] == [(attempt, "failed", False)]


def test_an_open_attempt_reads_as_no_terminal_event_and_a_superseded_one_is_marked(four: Path) -> None:
    attempt = _drive(four, ["observed"])
    assert worker_events(four)[0]["reason"] == "no terminal event"
    _update(four, {"id": "T01", "name": "renamed"})
    assert worker_events(four)[0]["superseded"] is True and worker_events(four)[0]["attempt_id"] == attempt


def test_held_attempts_are_listed_first_and_the_cap_counts_only_the_rest(four: Path) -> None:
    attempt = _drive(four, ["observed"])
    extra = "".join(
        "<!-- research-worker "
        + json.dumps(
            {
                "task": "T02",
                "handle": f"h{n}",
                "event": "observed",
                "scope": "s",
                "observation": "o",
                "assignment_revision": 1,
                "recorded": "2026-11-17T10:00:00+00:00",
            }
        )
        + " -->\n"
        for n in range(25)
    )
    (four / "tasks").mkdir(exist_ok=True)
    (four / "tasks/T02.md").write_text("# Task T02 notes\n" + extra, encoding="utf-8")
    result = workflow(four, "resume", {})
    assert result["unresolved_workers"][0]["attempt_id"] == attempt
    assert len(result["unresolved_workers"]) == 21 and result["omitted_workers"] == 5


# --- malformed lines ---------------------------------------------------------------------------


def test_a_malformed_line_is_listed_and_warned_at_policy_four(four: Path) -> None:
    attempt = _drive(four, ["observed"])
    path = four / "tasks/T01.md"
    path.write_text(notes(four) + "<!-- research-worker {not json -->\n", encoding="utf-8")
    path.write_text(notes(four) + '<!-- research-worker {"task": "T01", "attempt_id": "a"} -->\n', encoding="utf-8")
    warnings: list[str] = []
    listed = worker_events(four, warnings)
    assert [item.get("attempt_id") for item in listed] == [None, attempt]
    assert listed[0]["reason"] == "damaged notes" and "not valid JSON" in listed[0]["detail"]
    assert len(warnings) == 2 and all(w.startswith("tasks/T01.md line ") for w in warnings)
    resumed = workflow(four, "resume", {})
    assert resumed["unresolved_workers"][0]["reason"] == "damaged notes" and "durability_warnings" in resumed


def test_a_malformed_line_still_raises_below_policy_four(three: Path) -> None:
    event(three, "observed", "o-1")
    path = three / "tasks/T01.md"
    path.write_text(notes(three) + "<!-- research-worker {not json -->\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="invalid worker event JSON"):
        worker_events(three)


def test_a_line_with_a_non_integer_counter_is_malformed(four: Path) -> None:
    line = {
        "task": "T01", "handle": "h", "event": "observed", "scope": "s", "observation": "o",
        "assignment_revision": 1, "attempt_id": "T01-1-1", "counter": "1", "recorded": "2026-11-17T10:00:00+00:00",
    }  # fmt: skip
    path = four / "tasks/T01.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# Task T01 notes\n<!-- research-worker " + json.dumps(line) + " -->\n", encoding="utf-8")
    warnings: list[str] = []
    assert [item["reason"] for item in worker_events(four, warnings)] == ["damaged notes"]
    assert warnings == ["tasks/T01.md line 2: damaged research-worker line (counter is not an integer)"]


def test_a_terminal_attempt_is_not_listed(four: Path) -> None:
    _drive(four, ["completed"])
    assert worker_events(four) == []


# --- review round 9: damaged lines, ids, the race and the legacy guard ---------------------------------------


def line(**over: Any) -> str:
    base: dict[str, Any] = {
        "task": "T01", "handle": "h", "event": "intent", "scope": "s", "observation": "o",
        "assignment_revision": 1, "attempt_id": "T01-1-1", "counter": 1, "recorded": "2026-11-17T10:00:00+00:00",
    }  # fmt: skip
    base.update(over)
    return "<!-- research-worker " + json.dumps(base) + " -->\n"


def put(project: Path, *raw: str, task: str = "T01") -> None:
    path = project / f"tasks/{task}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"# Task {task} notes\n" + "".join(raw), encoding="utf-8")


def test_a_damaged_line_refuses_a_new_intent_instead_of_hiding_a_live_attempt(four: Path) -> None:
    put(four, "<!-- research-worker {not json -->\n")
    refused(four, "intent", "i-1", "damaged research-worker line")


def test_a_line_cut_short_is_reported_and_refuses_an_intent(four: Path) -> None:
    put(four, '<!-- research-worker {"task": "T01", "event": "intent"')
    warnings: list[str] = []
    assert [item["reason"] for item in worker_events(four, warnings)] == ["damaged notes"]
    assert warnings == ["tasks/T01.md line 2: research-worker line cut short (no closing marker)"]
    refused(four, "intent", "i-1", "line 2: research-worker line cut short")


def test_a_line_naming_an_unknown_event_or_another_task_is_skipped(four: Path) -> None:
    put(four, line(event="foo"), line(task="T02", attempt_id="T02-1-1"))
    warnings: list[str] = []
    assert [item["reason"] for item in worker_events(four, warnings)] == ["damaged notes"]
    assert len(warnings) == 2


def test_a_line_after_a_terminal_event_cannot_reopen_the_attempt(four: Path) -> None:
    put(four, line(event="completed"), line(event="observed"))
    assert worker_events(four) == []


def test_an_id_already_in_the_notes_is_never_issued_again(four: Path) -> None:
    put(four, line(attempt_id="T01-1-2"), line(attempt_id="T01-1-2", event="stopped"))
    assert event(four, "intent", "i-1")["attempt_id"] == "T01-1-3"


def test_attempt_keys_are_unknown_fields_below_policy_four(three: Path) -> None:
    put(three, line())
    with pytest.raises(WorkspaceError) as caught:
        worker_events(three)
    assert "invalid worker event JSON" not in str(caught.value)  # a field error keeps its own text, as before


def test_bad_json_below_policy_four_still_says_so(three: Path) -> None:
    put(three, "<!-- research-worker {not json -->\n")
    with pytest.raises(WorkspaceError, match="invalid worker event JSON"):
        worker_events(three)


def test_a_crlf_or_trailing_spaces_do_not_damage_a_line(four: Path) -> None:
    put(four, line().replace("\n", "  \r\n"))
    warnings: list[str] = []
    assert [item["attempt_id"] for item in worker_events(four, warnings)] == ["T01-1-1"] and warnings == []


def test_a_counter_without_an_attempt_id_is_damaged(four: Path) -> None:
    base = json.loads(line()[len("<!-- research-worker ") : -len(" -->\n")])
    del base["attempt_id"]
    put(four, "<!-- research-worker " + json.dumps(base) + " -->\n")
    warnings: list[str] = []
    assert [item["reason"] for item in worker_events(four, warnings)] == ["damaged notes"]
    assert "must come together" in warnings[0]


def test_the_refusal_names_the_line_and_the_hand_repair(four: Path) -> None:
    put(four, line(), "<!-- research-worker {not json -->\n")
    refused(
        four,
        "stopped",
        "stop-x",
        r"line 3: damaged research-worker line \(not valid JSON\).*by hand",
        attempt_id="T01-1-1",
    )


def test_two_events_built_on_one_state_the_second_is_refused(four: Path) -> None:
    first = request(
        four, "race-1", task="T01", handle="h", event="intent", scope="s", observation="o", assignment_revision=1
    )
    second = request(
        four, "race-2", task="T01", handle="h", event="intent", scope="s", observation="o", assignment_revision=1
    )
    workflow(four, "worker-event", first)
    with pytest.raises(WorkspaceError):
        workflow(four, "worker-event", second)
    assert [item["event"] for item in lines(four)] == ["intent"]


def test_attempt_events_never_touch_the_legacy_execution_block(four: Path) -> None:
    from workspace_operations import task_operation

    before = _load_state(four)["execution"]
    attempt = _drive(four, ["launched", "observed"])
    assert _load_state(four)["execution"] == before
    # a mutating command still works: the reader-only guard would refuse it if execution had changed
    task_operation(four, "block", "T01", expected_revision=_load_state(four)["revision"], reason="x")
    assert _task(four)["status"] == "BLOCKED" and attempt


def test_a_held_attempt_stays_listed_on_a_skipped_task(four: Path) -> None:
    from workspace_operations import task_operation

    attempt = _drive(four, ["observed"])
    task_operation(four, "skip", "T01", expected_revision=_load_state(four)["revision"], reason="dropped")
    assert [item["attempt_id"] for item in worker_events(four)] == [attempt]


def test_stopped_needs_an_observation_like_every_event(four: Path) -> None:
    attempt = open_attempt(four)
    refused(four, "stopped", "stop-x", "observation", attempt_id=attempt, observation="  ")


def test_a_held_attempt_does_not_block_finishing_another_task(four: Path) -> None:
    import sys

    import workspace_lib as lib
    from workspace_operations import task_operation

    task_operation(four, "start", "T02", expected_revision=_load_state(four)["revision"])
    event(four, "intent", "i-t02", task="T02")
    record = lib.record_evidence_result(four, "T02", [sys.executable, "-c", "pass"]).record_id
    task_operation(four, "finish", "T02", expected_revision=_load_state(four)["revision"], evidence_record_ids=[record])
    assert _task(four, "T02")["status"] == "DONE"
