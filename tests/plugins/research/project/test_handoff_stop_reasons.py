"""Stricter handoff: a closed list of stop reasons, a `continue` signal, and a checkpoint that refuses a bare wait."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_lib
from workspace_context import checkpoint_data
from workspace_lib import WorkspaceError, allocate_project
from workspace_operations import task_operation
from workspace_session import STOP_REASONS, next_move, ready_task_ids
from workspace_workflows import workflow

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_lifecycle_commands import planned, request

SKILL = REPO_ROOT / "plugins/research/skills/project"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    return allocate_project(root, title="Stop reasons", working_directory=target)


def continuation_of(project: Path) -> dict[str, Any]:
    """The continuation fields of the saved checkpoint."""
    data = checkpoint_data(project)
    assert data is not None
    return data["continuation"]


def checkpoint(project: Path, identity: str, **continuation: str) -> dict[str, Any]:
    return workflow(project, "checkpoint", request(project, identity, continuation={"next": "Go on", **continuation}))


def a_task(
    identifier: str, status: str = "TODO", *, depends_on: list[str] | None = None, authorized: bool = True
) -> Any:
    return {
        "id": identifier, "status": status, "depends_on": depends_on or [],
        "authorization": {"status": "not_required" if authorized else "pending"},
    }


def a_state(tasks: list[dict[str, Any]], status: str = "EXECUTING", **fields: Any) -> dict[str, Any]:
    return {
        "status": status, "tasks": tasks, "worktrees": [], "created": "2026-10-20T10:00:00+00:00",
        "review": {"required": False, "status": "not_required", "cycle": 0}, **fields,
    }


def test_the_stop_reasons_are_the_closed_list_of_the_design() -> None:
    assert STOP_REASONS == (
        "user-decision", "authorization", "blocked", "context-pressure", "closure-step", "closed",
    )


@pytest.mark.parametrize("label", ["waiting", "blocked", "ready for handoff"])
def test_a_checkpoint_that_stops_must_name_a_listed_reason(project: Path, label: str) -> None:
    with pytest.raises(WorkspaceError, match="needs a stop_reason, one of: user-decision, authorization"):
        checkpoint(project, "cp-bare", session=label)
    with pytest.raises(WorkspaceError, match="needs a stop_reason"):
        checkpoint(project, "cp-wrong", session=label, stop_reason="tired")
    with pytest.raises(WorkspaceError, match="needs a stop_reason"):
        checkpoint(project, "cp-empty", session=label, stop_reason="")


@pytest.mark.parametrize("reason", STOP_REASONS)
def test_every_listed_reason_is_accepted_and_recorded(project: Path, reason: str) -> None:
    checkpoint(project, f"cp-{reason}", session="waiting", stop_reason=reason)
    saved = continuation_of(project)
    assert (saved["session"], saved["stop_reason"]) == ("waiting", reason)
    assert f"## Stop reason\n\n{reason}\n" in (project / "handoff.md").read_text(encoding="utf-8")


def test_a_working_session_carries_no_reason_and_clears_an_earlier_one(project: Path) -> None:
    checkpoint(project, "cp-wait", session="waiting", stop_reason="user-decision")
    with pytest.raises(WorkspaceError, match="a working session carries no stop_reason"):
        checkpoint(project, "cp-both", session="working", stop_reason="blocked")
    checkpoint(project, "cp-resume", session="working")
    saved = continuation_of(project)
    assert saved["session"] == "working" and "stop_reason" not in saved
    assert "## Stop reason" not in (project / "handoff.md").read_text(encoding="utf-8")


def test_a_checkpoint_with_no_session_stays_working(project: Path) -> None:
    checkpoint(project, "cp-plain")
    assert "stop_reason" not in continuation_of(project)


def test_the_other_actions_that_write_a_handoff_apply_the_same_rule(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="needs a stop_reason"):
        workflow(project, "round", request(project, "round-wait", continuation={"next": "Ask", "session": "waiting"}))
    workflow(
        project, "round",
        request(
            project, "round-ok",
            continuation={"next": "Ask", "session": "waiting", "stop_reason": "user-decision"},
        ),
    )
    assert continuation_of(project)["stop_reason"] == "user-decision"


def test_an_old_handoff_is_still_read_and_a_bare_rewrite_is_refused(project: Path) -> None:
    checkpoint(project, "cp-old", session="waiting", stop_reason="user-decision")
    path = project / "handoff.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\A<!-- research-checkpoint-v1\n(.*?)\n-->\n", text, re.DOTALL)
    assert match is not None
    metadata = json.loads(match[1])
    del metadata["continuation"]["stop_reason"]
    legacy = "<!-- research-checkpoint-v1\n" + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + "\n-->\n"
    path.write_text(legacy + text[match.end():].replace("\n## Stop reason\n\nuser-decision\n", ""), encoding="utf-8")
    assert continuation_of(project)["session"] == "waiting"
    bundle = workflow(project, "resume", {})
    assert bundle["stop_reason"] == "user-decision"  # the signal comes from the records, not from the old note
    with pytest.raises(WorkspaceError, match="needs a stop_reason"):
        checkpoint(project, "cp-after-old")  # inherits session waiting, so it must now name a reason
    checkpoint(project, "cp-after-old-working", session="working")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (a_state([], "DONE"), (None, "closed")),
        (a_state([a_task("T01", "DONE")], "CANCELLED"), (None, "closed")),
        (a_state([], "ALIGNING"), (None, "user-decision")),
        (a_state([], "PLANNING"), ("plan", None)),
        (a_state([a_task("T01", "RUNNING"), a_task("T02")]), ("T01", None)),
        (a_state([a_task("T01", "DONE"), a_task("T02"), a_task("T03")]), ("T02", None)),
        (a_state([a_task("T01"), a_task("T02", depends_on=["T01"])]), ("T01", None)),
        (a_state([a_task("T01", authorized=False)]), (None, "authorization")),
        (a_state([a_task("T01", "BLOCKED"), a_task("T02", depends_on=["T01"])]), (None, "blocked")),
        (a_state([a_task("T01", "SKIPPED"), a_task("T02", depends_on=["T01"])]), (None, "blocked")),
        (a_state([a_task("T01", "DONE"), a_task("T02", "SKIPPED")]), ("finalize", None)),
    ],
)
def test_the_next_move_is_a_task_or_exactly_one_listed_reason(state: dict[str, Any], expected: tuple[Any, Any]) -> None:
    move = next_move(state)
    assert (move["continue"], move["stop_reason"]) == expected
    assert (move["continue"] is None) != (move["stop_reason"] is None)
    assert move["move_detail"]


def test_a_task_waiting_on_authorization_is_named_and_one_that_waits_on_a_dependency_is_not() -> None:
    state = a_state([a_task("T01", "DONE"), a_task("T02", authorized=False), a_task("T03", depends_on=["T02"])])
    move = next_move(state)
    assert move["stop_reason"] == "authorization"
    assert "T02" in move["move_detail"] and "T03" not in move["move_detail"]


def test_closing_waits_for_the_user_while_a_worktree_or_a_review_is_open() -> None:
    finished = [a_task("T01", "DONE")]
    worktree = {"path": "/w", "kind": "created", "status": "active", "role": "target"}
    move = next_move(a_state(finished, worktrees=[worktree]))
    assert (move["continue"], move["stop_reason"]) == (None, "closure-step")
    assert "keep or remove" in move["move_detail"]
    kept = {**worktree, "status": "kept"}
    none = {**worktree, "kind": "none"}
    assert next_move(a_state(finished, worktrees=[kept, none]))["continue"] == "finalize"
    review = {"required": True, "status": "pending", "cycle": 1}
    move = next_move(a_state(finished, review=review))
    assert move["stop_reason"] == "closure-step" and "review" in move["move_detail"]
    review["status"] = "accepted"
    assert next_move(a_state(finished, review=review))["continue"] == "finalize"


def test_closing_waits_for_the_delivery_records_of_a_gated_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "DELIVERY_GATES_FROM", "2000-01-01T00:00:00+00:00")
    worktree = {"path": "/w", "kind": "created", "status": "kept", "role": "target"}
    move = next_move(a_state([a_task("T01", "DONE")], worktrees=[worktree]))
    assert move["stop_reason"] == "closure-step" and "no pull_request" in move["move_detail"]


def test_ready_tasks_need_their_dependencies_done_and_no_pending_authorization() -> None:
    tasks = [
        a_task("T01", "DONE"), a_task("T02", depends_on=["T01"]), a_task("T03", depends_on=["T02"]),
        a_task("T04", authorized=False), a_task("T05", "RUNNING"),
    ]
    assert ready_task_ids(tasks) == ["T02"]


def test_task_results_carry_the_next_move(project: Path) -> None:
    planned(project)
    started = task_operation(project, "start", "T01", expected_revision=1)
    assert "continue" not in started
    verified = workflow(
        project, "verify",
        {"id": "check", "checks": [{"task": "T01", "argv": [sys.executable, "-c", "print('verified')"]}]},
    )
    record = verified["attempts"][0]["result"]["record_id"]
    finished = task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[record])
    assert (finished["continue"], finished["stop_reason"]) == ("T02", None)
    skipped = task_operation(project, "skip", "T02", expected_revision=3, reason="Superseded within scope")
    assert (skipped["continue"], skipped["stop_reason"]) == ("finalize", None)
    assert "move_detail" in skipped


def test_a_blocked_task_stops_with_the_blocked_reason(project: Path) -> None:
    planned(project)
    task_operation(project, "start", "T01", expected_revision=1)
    blocked = task_operation(project, "block", "T01", expected_revision=2, reason="Waiting on an unavailable service")
    assert (blocked["continue"], blocked["stop_reason"]) == (None, "blocked")


def test_resume_carries_the_next_move(project: Path) -> None:
    planned(project)
    bundle = workflow(project, "resume", {})
    assert (bundle["continue"], bundle["stop_reason"]) == ("T01", None)
    assert "T01 is ready" in bundle["move_detail"]


def test_the_skill_and_the_stopping_reference_name_every_reason() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    stopping = (SKILL / "references/stopping.md").read_text(encoding="utf-8")
    for reason in STOP_REASONS:
        assert f"`{reason}`" in skill, f"SKILL.md does not name {reason}"
        assert f"`{reason}`" in stopping, f"stopping.md does not define {reason}"
    assert "stopping.md" in skill
