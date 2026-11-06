"""P5: a BLOCKED task records what it waits for and until when; resume, context and the dashboard show it.

Design: artifacts/architecture.md (project 2026-11-06-002) §4 "Waits", "Workflow inputs and refusals"
and "Due waits", §6 `durable_move` step 3. A wait is a `research-wait` entry in the task notes; only one
recorded after the task's latest `task block` line counts, and the revision never moves.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import workspace_dashboard as dash
import workspace_durability
from workspace_context import compact_context
from workspace_durability import waits
from workspace_lib import WorkspaceError
from workspace_operations import task_operation
from workspace_session import _load_state, validated_project_context
from workspace_workflows import input_schema, worker_events, workflow

from tests.plugins.research.project.test_durability_effects import lines, make_running, manage
from tests.plugins.research.project.test_lifecycle_commands import request

PAST = "2020-01-01T09:00:00+00:00"
FUTURE = "2099-01-01T09:00:00+00:00"
REFERENCE = "references/durability.md"
PAGE = Path(dash.__file__).with_name("dashboard.html")


def block(project: Path, task_id: str = "T01", reason: str = "waiting for the reviewer") -> None:
    task_operation(project, "block", task_id, expected_revision=_load_state(project)["revision"], reason=reason)


def wait(project: Path, identity: str, task: str = "T01", on: str = "user reply", **extra: Any) -> Any:
    return workflow(project, "wait", request(project, identity, task=task, on=on, **extra))


def cli_wait(project: Path, identity: str, capsys: Any, **extra: Any) -> int:
    path = project / f"{identity}.json"
    path.write_text(json.dumps(request(project, identity, task="T01", on="user reply", **extra)), encoding="utf-8")
    code = manage("workflow", project, "wait", path)
    capsys.readouterr()
    return code


def cli_task(project: Path, action: str, capsys: Any, *extra: str) -> int:
    code = manage("task", project, action, "T01", "--expected-revision", _load_state(project)["revision"], *extra)
    capsys.readouterr()
    return code


@pytest.fixture
def blocked(tmp_path: Path) -> Path:
    """T01 RUNNING then BLOCKED; T02 TODO with no dependency, so it is ready."""
    project = make_running(tmp_path)
    block(project)
    return project


def test_a_due_wait_is_shown_and_named_in_the_move_while_another_task_is_ready(blocked: Path, capsys: Any) -> None:
    before = _load_state(blocked)["revision"]
    wait(blocked, "wait-1", until=PAST)
    assert _load_state(blocked)["revision"] == before
    text = (blocked / "tasks/T01.md").read_text(encoding="utf-8")
    marker = re.search(r"^<!-- research-wait (.+) -->$", text, re.MULTILINE)
    assert marker is not None
    entry = json.loads(marker[1])
    assert (entry["on"], entry["until"], entry["reason"]) == ("user reply", PAST, None)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}[+-]\d\d:\d\d", entry["recorded"])
    expected = [{"task": "T01", "on": "user reply", "until": PAST, "due": True, "reason": "waiting for the reviewer"}]
    resumed = workflow(blocked, "resume", {})
    assert resumed["waits"] == expected and resumed["reference"] == REFERENCE
    assert resumed["continue"] == "T02"
    assert resumed["move_detail"] == "T02 is ready: start it; wait due: T01 (user reply)"
    packet = workflow(blocked, "packet", {"task": "T01", "worker": "w", "scope": "s", "lessons": []})
    assert packet["waits"] == expected
    compact = compact_context(validated_project_context(blocked), _load_state(blocked), blocked)
    assert compact["waits"] == expected and compact["reference"] == REFERENCE
    assert compact["next_move"]["move_detail"].endswith("; wait due: T01 (user reply)")
    assert manage("context", blocked) == 0
    assert json.loads(capsys.readouterr().out)["waits"] == expected


def test_a_future_wait_is_not_due_and_its_own_reason_wins(blocked: Path) -> None:
    wait(blocked, "wait-1", until=FUTURE, reason="the vendor answers on Monday")
    resumed = workflow(blocked, "resume", {})
    assert resumed["waits"] == [
        {"task": "T01", "on": "user reply", "until": FUTURE, "due": False, "reason": "the vendor answers on Monday"}
    ]
    assert "wait due" not in resumed["move_detail"]
    wait(blocked, "wait-2", on="the CI rerun")  # no until: never due; the latest entry is the wait
    (shown,) = workflow(blocked, "resume", {})["waits"]
    assert (shown["on"], shown["until"], shown["due"]) == ("the CI rerun", None, False)


def test_leaving_blocked_ends_the_wait(blocked: Path) -> None:
    wait(blocked, "wait-1", until=PAST)
    task_operation(blocked, "start", "T01", expected_revision=_load_state(blocked)["revision"])
    resumed = workflow(blocked, "resume", {})
    assert "waits" not in resumed and "reference" not in resumed
    assert "wait due" not in resumed["move_detail"]


def test_a_wait_recorded_before_the_latest_block_is_ignored(tmp_path: Path, capsys: Any) -> None:
    project = make_running(tmp_path)
    assert cli_task(project, "block", capsys, "--reason", "first block") == 0
    assert cli_wait(project, "wait-1", capsys, until=PAST) == 0
    assert [line["task"] for line in lines(project) if line["action"] == "wait"] == ["T01"]
    assert workflow(project, "resume", {})["waits"][0]["on"] == "user reply"
    assert cli_task(project, "start", capsys) == 0
    assert cli_task(project, "block", capsys, "--reason", "blocked again") == 0
    resumed = workflow(project, "resume", {})
    assert "waits" not in resumed and "wait due" not in resumed["move_detail"]
    # A refused wait call is logged with its task but failed, so it does not revive the old entry.
    assert cli_wait(project, "wait-bad", capsys, until="2026-11-07T09:00:00") != 0
    assert "waits" not in workflow(project, "resume", {})
    assert cli_wait(project, "wait-2", capsys, until=FUTURE) == 0
    assert workflow(project, "resume", {})["waits"][0]["until"] == FUTURE
    # Another task's block line does not hide this task's wait.
    with (project / "invocations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"command": "task", "action": "block", "task": "T02", "exit": 0}) + "\n")
    assert workflow(project, "resume", {})["waits"][0]["until"] == FUTURE


def test_refusals(tmp_path: Path) -> None:
    project = make_running(tmp_path)
    with pytest.raises(WorkspaceError, match="BLOCKED"):
        wait(project, "wait-running")
    with pytest.raises(WorkspaceError, match="BLOCKED"):
        wait(project, "wait-todo", task="T02")
    block(project)
    with pytest.raises(WorkspaceError, match="timezone"):
        wait(project, "wait-naive", until="2026-11-07T09:00:00")
    with pytest.raises(WorkspaceError, match="timezone"):
        wait(project, "wait-garbage", until="next week")
    with pytest.raises(WorkspaceError, match="timezone"):
        wait(project, "wait-number", until=7)
    with pytest.raises(WorkspaceError, match="on must be non-empty text"):
        wait(project, "wait-empty", on=" ")
    with pytest.raises(WorkspaceError, match="reason"):
        wait(project, "wait-reason", reason=3)
    with pytest.raises(WorkspaceError, match="missing required field"):
        workflow(project, "wait", request(project, "wait-no-on", task="T01"))
    with pytest.raises(WorkspaceError, match="unknown field"):
        workflow(project, "wait", request(project, "wait-op", task="T01", on="x", operation="clear"))
    with pytest.raises(WorkspaceError, match="unknown task"):
        wait(project, "wait-t09", task="T09")
    notes = project / "tasks/T01.md"
    assert not notes.exists() or "research-wait" not in notes.read_text(encoding="utf-8")
    first = request(project, "wait-1", task="T01", on="user reply")
    workflow(project, "wait", first)
    workflow(project, "wait", first)  # the same ID completes once
    with pytest.raises(WorkspaceError, match="already used with different input"):
        workflow(project, "wait", {**first, "on": "something else"})


def test_the_schema_has_no_operation_and_requires_task_and_on() -> None:
    schema = input_schema("wait")
    assert {"task", "on", "until", "reason"} <= set(schema["allowed"]) and "operation" not in schema["allowed"]
    assert {"task", "on"} <= set(schema["required"]) and "until" not in schema["required"]
    assert schema["required_by"] == {}


def test_a_malformed_entry_is_skipped_and_the_old_worker_parser_is_not_tripped(blocked: Path) -> None:
    wait(blocked, "wait-1", until=FUTURE, reason="text with --> inside")
    path = blocked / "tasks/T01.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n<!-- research-wait {not json -->\n<!-- research-wait [1] -->\n"
        + '<!-- research-wait {"until": null} -->\n'
        + '<!-- research-wait {"on": "x", "until": "2026-11-07T09:00:00"} -->\n'
        + '<!-- research-wait {"on": "x", "until": null, "reason": 5} -->\n',
        encoding="utf-8",
    )
    resumed = workflow(blocked, "resume", {})
    assert [(item["on"], item["reason"]) for item in resumed["waits"]] == [("user reply", "text with --> inside")]
    assert len(resumed["durability_warnings"]) == 5
    assert all("research-wait" in item for item in resumed["durability_warnings"])
    assert waits(blocked, _load_state(blocked)) == resumed["waits"]
    # A 0.33.0-shaped reader matches only `research-worker` lines, so it neither raises nor sees these.
    assert worker_events(blocked) == []
    written = next(line for line in path.read_text(encoding="utf-8").splitlines() if "inside" in line)
    assert written.count("-->") == 1


def test_an_unreadable_notes_file_or_task_without_notes_has_no_wait(blocked: Path) -> None:
    assert waits(blocked, _load_state(blocked)) == []
    (blocked / "tasks").mkdir(exist_ok=True)
    (blocked / "tasks/T01.md").write_bytes(b"\xff\xfe broken")
    assert waits(blocked, _load_state(blocked)) == []


def test_due_is_judged_against_now(blocked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    until = "2030-06-01T12:00:00+02:00"
    wait(blocked, "wait-1", until=until)
    moment = datetime.fromisoformat(until).astimezone(timezone.utc)
    monkeypatch.setattr(workspace_durability, "utcnow", lambda: moment - timedelta(seconds=1))
    assert waits(blocked, _load_state(blocked))[0]["due"] is False
    monkeypatch.setattr(workspace_durability, "utcnow", lambda: moment)
    assert waits(blocked, _load_state(blocked))[0]["due"] is True


def test_the_dashboard_row_carries_the_wait(blocked: Path) -> None:
    wait(blocked, "wait-1", until=PAST)
    row = dash.attach_waits([dash.build_project(blocked)], dash.utcnow())[0]
    by_id = {item["id"]: item for item in row["tasks"]}
    assert by_id["T01"]["wait"] == {"on": "user reply", "until": PAST, "due": True}
    assert by_id["T02"]["wait"] is None


def test_the_dashboard_judges_due_at_serve_time(blocked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    until = "2030-06-01T12:00:00+02:00"
    wait(blocked, "wait-1", until=until)
    moment = datetime.fromisoformat(until).astimezone(timezone.utc)
    monkeypatch.setattr(dash, "utcnow", lambda: moment - timedelta(seconds=1))
    board = dash.Dashboard(blocked.parent, blocked.parent / "claude")
    before = board.version()["fingerprint"]
    first = {item["id"]: item for item in board.payload()["projects"][0]["tasks"]}
    assert first["T01"]["wait"] == {"on": "user reply", "until": until, "due": False}
    monkeypatch.setattr(dash, "utcnow", lambda: moment)  # time passes; no file changes
    assert board.version()["fingerprint"] != before  # the page reloads when the wait turns due
    second = {item["id"]: item for item in board.payload()["projects"][0]["tasks"]}
    assert second["T01"]["wait"]["due"] is True


def test_an_invocation_line_alone_does_not_rebuild_the_dashboard(
    blocked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "invocations.jsonl" not in dash.WATCHED_FILES
    builds = []
    original = dash.build_model

    def counting(root: Path) -> dict[str, Any]:
        builds.append(root)
        return original(root)

    monkeypatch.setattr(dash, "build_model", counting)
    board = dash.Dashboard(blocked.parent, blocked.parent / "claude")
    board.payload()
    before = board.version()["fingerprint"]
    with (blocked / "invocations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"command": "context", "exit": 0}) + "\n")
    assert board.version()["fingerprint"] == before
    board.payload()
    assert len(builds) == 1


def test_a_new_wait_shows_on_the_next_payload(blocked: Path) -> None:
    board = dash.Dashboard(blocked.parent, blocked.parent / "claude")
    before = board.version()["fingerprint"]
    assert {item["id"]: item for item in board.payload()["projects"][0]["tasks"]}["T01"]["wait"] is None
    wait(blocked, "wait-1", until=FUTURE)
    assert board.version()["fingerprint"] != before
    payload = board.payload()
    assert payload["fingerprint"] == board.version()["fingerprint"]
    rows = {item["id"]: item for item in payload["projects"][0]["tasks"]}
    assert rows["T01"]["wait"] == {"on": "user reply", "until": FUTURE, "due": False}


def test_the_page_renders_the_wait_in_the_status_cell() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "waits on" in page and "waitText(t.wait)" in page and "(due)" in page


def test_a_due_wait_and_an_open_intent_are_both_named_in_the_move(blocked: Path) -> None:
    from tests.plugins.research.project.test_durability_effects import intent

    intent(blocked, "intent-1")
    wait(blocked, "wait-1", until=PAST)
    assert workflow(blocked, "resume", {})["move_detail"] == (
        "T02 is ready: start it; wait due: T01 (user reply); effect may have happened: T01 (pr-63)"
    )


def test_an_unreadable_row_passes_through_attach_waits(tmp_path: Path) -> None:
    (tmp_path / "2026-11-06-009").mkdir()
    row = dash.build_project(tmp_path / "2026-11-06-009")
    assert row["unreadable"] and dash.attach_waits([row], dash.utcnow()) == [row]
