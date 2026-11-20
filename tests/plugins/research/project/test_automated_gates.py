"""Automated mode: the changes that need a decision, cancel and maintenance, consent and worktrees (T06, C6).

With `automated` on, a task newly SKIPPED needs a `task-change` decision with choice `skip`, a finish with
replan `changed` needs a `replan` decision with choice `changed`, and the project newly DONE needs a
`project-close` decision with choice `close`, or a user answer of that choice, on every commit path.
`workflow cancel` and `workflow maintenance` need the user's words instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import workspace_operations
import workspace_workflows
from workspace_lib import WorkspaceError, commit_state, document_sha256
from workspace_operations import close_project, task_operation
from workspace_session import update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_coordinator_gate import (
    C0,
    _evidence,
    _explicit,
    _files,
    _finish,
    _project,
    _receipt,
    _revision,
    _start,
    _state,
    _task,
    _worker,
)
from tests.plugins.research.project.test_decide_action import ABSENT, automated, decide, lesser
from tests.plugins.research.project.test_finalize_staging import PLAIN_REFLECTION
from tests.plugins.research.project.test_goldfish import close as gold_close
from tests.plugins.research.project.test_goldfish import finding, open_round
from tests.plugins.research.project.test_goldfish import report as gold_report
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_options import USER, finalize, settings
from tests.plugins.research.project.test_packets import HEADING
from tests.plugins.research.project.test_policy_five import _rewrite
from tests.plugins.research.project.test_worktrees import add_worktree, make_project, make_repository
from tests.plugins.research.project.test_worktrees import close as worktree_close
from tests.plugins.research.project.test_worktrees import record as worktree_record

PREFIX = "automated mode needs a valid decision: "
NONE_YET = PREFIX + "no current '{k}' decision for {s}; record one with 'workflow decide' (choice '{c}')"
STALE = PREFIX + "the '{k}' decision for {s} is stale, {s} changed after it was recorded; record it again"
ROUNDS = PREFIX + "'{k}' for {s} is at evidence round {n} of 3; collect evidence and record it again"
EXHAUSTED = PREFIX + "'{k}' for {s} is exhausted after 3 evidence rounds; the user decides"
OTHER = PREFIX + "the {who} decision for {s} chose '{x}', not '{c}'"
WORDS = (
    "workflow {a} needs the user's words while options.automated is on: send confirmation with the source and the "
    "user's response; the agent never writes its own words there"
)
REMOVE_REFUSED = (
    "worktree close: recording remove is refused while options.automated is on. Removing a worktree is the user's "
    "own step: the tool does not run `git worktree remove`, it only refuses this record. If the worktree is still "
    "there, record keep with the user's standing words. If it is already gone, keep is refused too ('the worktree "
    "is missing'): stop for the user, turn automated off with 'workflow settings' and the user's words, then record "
    "remove with their confirmation"
)
SKIP = {"k": "task-change", "s": "T02", "c": "skip"}
CLOSE = {"k": "project-close", "s": "project", "c": "close"}
NEXT = {"next": "Continue."}


@pytest.fixture
def auto(tmp_path: Path) -> Path:
    return automated(tmp_path)


@pytest.fixture
def ready(tmp_path: Path) -> Path:
    return ready_project(tmp_path)


def ready_project(tmp_path: Path) -> Path:
    """Every task terminal (T01 finished, T02 skipped) before automated is turned on: ready to close."""
    project = _project(tmp_path, 6)
    _start(project, "T01")
    _receipt(project, "T01")
    _finish(project, "T01")
    task_operation(project, "skip", "T02", expected_revision=_revision(project), reason="not needed")
    settings(project, options={"automated": True})
    return project


def refused(call: Any, project: Path, *texts: str) -> str:
    before = _files(project)
    with pytest.raises(WorkspaceError) as caught:
        call()
    assert _files(project) == before
    message = str(caught.value)
    for text in texts:
        assert text in message, message
    return message


def skip(project: Path, task_id: str = "T02") -> dict[str, Any]:
    return task_operation(project, "skip", task_id, expected_revision=_revision(project), reason="not needed")


def skipped_patch(task_id: str = "T02") -> dict[str, Any]:
    return {"tasks": [{"id": task_id, "status": "SKIPPED", "skip_reason": "not needed"}]}


def close_decision(project: Path, **fields: Any) -> dict[str, Any]:
    return decide(project, **{"kind": "project-close", "subject": "project", "choice": "close", **fields})


def legacy_close(project: Path) -> dict[str, Any]:
    path = project / "reflection.md"
    token = document_sha256(path.read_text(encoding="utf-8")) if path.exists() else "missing"
    return close_project(
        project, expected_revision=_revision(project), reflection=PLAIN_REFLECTION, expected_reflection_sha256=token,
    )


# --- a task newly SKIPPED -------------------------------------------------------------------------------


def test_task_skip_is_refused_without_a_decision(auto: Path) -> None:
    refused(lambda: skip(auto), auto, "task T02: " + NONE_YET.format(**SKIP))
    assert _task(auto, "T02")["status"] == "TODO"


def test_task_skip_passes_with_a_skip_decision(auto: Path) -> None:
    decide(auto)
    assert skip(auto)["changed_tasks"] == [{"id": "T02", "status": "SKIPPED"}]


def test_a_keep_decision_does_not_allow_a_skip(auto: Path) -> None:
    decide(auto, choice="keep")
    refused(lambda: skip(auto), auto, OTHER.format(who="agent's", s="T02", x="keep", c="skip"))


def test_a_user_answer_allows_the_skip(auto: Path) -> None:
    decide(auto, by="user")
    refused(lambda: skip(auto), auto, OTHER.format(who="user's", s="T02", x="keep", c="skip"))
    decide(auto, by="user", choice="skip")
    skip(auto)
    assert _task(auto, "T02")["status"] == "SKIPPED"


def test_a_skip_in_an_evidence_round_or_exhausted_is_refused(auto: Path) -> None:
    lesser(auto, 1)
    refused(lambda: skip(auto), auto, ROUNDS.format(n=1, **SKIP))
    lesser(auto, 2)
    refused(lambda: skip(auto), auto, EXHAUSTED.format(**SKIP))


def test_update_cannot_set_a_task_skipped_without_a_decision(auto: Path) -> None:
    text = "task T02: " + NONE_YET.format(**SKIP)
    refused(lambda: update_project_data(auto, skipped_patch(), expected_revision=_revision(auto)), auto, text)
    reconcile = request(auto, "rec-skip", patch=skipped_patch(), decision="skip T02", continuation=NEXT)
    refused(lambda: workflow(auto, "reconcile", reconcile), auto, text)
    candidate = _state(auto)
    candidate["tasks"][1].update(status="SKIPPED", skip_reason="not needed")
    refused(lambda: commit_state(auto, candidate, expected_revision=_revision(auto)), auto, text)
    decide(auto)
    update_project_data(auto, skipped_patch(), expected_revision=_revision(auto))


def test_a_new_task_created_skipped_needs_a_decision_too(auto: Path) -> None:
    new = {
        "id": "T03", "name": "n", "success_criteria": "s", "verification": "v",
        "effect": {"kind": "none", "description": None}, "status": "SKIPPED", "skip_reason": "r",
        "spec_refs": [HEADING],
    }
    refused(
        lambda: update_project_data(auto, {"tasks": [new]}, expected_revision=_revision(auto)), auto,
        "task T03: " + NONE_YET.format(k="task-change", s="T03", c="skip"),
    )


def test_a_skip_decision_goes_stale_when_the_assignment_changes(auto: Path) -> None:
    decide(auto)
    change = {"tasks": [{"id": "T02", "success_criteria": "changed"}]}
    update_project_data(auto, change, expected_revision=_revision(auto))
    refused(lambda: skip(auto), auto, STALE.format(**SKIP))
    decide(auto)
    skip(auto)


def test_a_commit_cannot_revive_a_stale_decision_by_rewriting_the_counter(auto: Path) -> None:
    decide(auto)
    change = {"tasks": [{"id": "T02", "success_criteria": "changed"}]}
    update_project_data(auto, change, expected_revision=_revision(auto))
    candidate = _state(auto)
    candidate["tasks"][1].update(status="SKIPPED", skip_reason="not needed", assignment_counter=0)
    refused(lambda: commit_state(auto, candidate, expected_revision=_revision(auto)), auto, C0.format(n=1, m=0))


# --- a finish with replan changed -----------------------------------------------------------------------


def _worked(project: Path) -> None:
    _start(project, "T02")
    _worker(project, "T02", "completed")


def test_finish_with_changed_is_refused_without_a_replan_decision(auto: Path) -> None:
    _worked(auto)
    record = _evidence(auto, "T02")
    text = "task T02 cannot finish: " + NONE_YET.format(k="replan", s="T02", c="changed")
    refused(lambda: _finish(auto, "T02", replan="changed", reason="T03 added", record=record), auto, text)
    decide(auto, kind="replan", choice="none", assessor=ABSENT)  # the other choice does not qualify
    refused(
        lambda: _finish(auto, "T02", replan="changed", reason="T03 added", record=record), auto,
        OTHER.format(who="agent's", s="T02", x="none", c="changed"),
    )
    decide(auto, kind="replan", choice="changed", assessor=ABSENT)
    _finish(auto, "T02", replan="changed", reason="T03 added", record=record)
    assert _task(auto, "T02")["replan"]["statement"] == "changed"


def test_a_commit_of_a_changed_finish_needs_the_decision(auto: Path) -> None:
    _worked(auto)
    record = _evidence(auto, "T02")
    candidate = _state(auto)
    anchor = {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{record}"}
    candidate["tasks"][1].update(
        status="DONE", evidence=[anchor], replan={"statement": "changed", "reason": "by commit"},
    )
    candidate["current_tasks"] = []
    text = "task T02: " + NONE_YET.format(k="replan", s="T02", c="changed")
    refused(lambda: commit_state(auto, candidate, expected_revision=_revision(auto)), auto, text)


def test_finish_with_none_needs_no_decision(auto: Path) -> None:
    _worked(auto)
    _finish(auto, "T02")
    assert _task(auto, "T02")["status"] == "DONE"


# --- the project newly DONE -----------------------------------------------------------------------------


def test_finalize_is_refused_without_a_close_decision_and_writes_no_reflection(ready: Path) -> None:
    refused(lambda: finalize(ready, PLAIN_REFLECTION), ready, "project: " + NONE_YET.format(**CLOSE))
    close_decision(ready, choice="continue")
    refused(
        lambda: finalize(ready, PLAIN_REFLECTION), ready,
        "project: " + OTHER.format(who="agent's", s="project", x="continue", c="close"),
    )
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)
    assert _state(ready)["status"] == "DONE"


def test_a_user_close_answer_allows_the_close(ready: Path) -> None:
    decide(ready, by="user", kind="project-close", subject="project", choice="close")
    finalize(ready, PLAIN_REFLECTION)
    assert _state(ready)["status"] == "DONE"


def test_the_legacy_close_is_refused_without_a_close_decision_and_saves_no_reflection(ready: Path) -> None:
    message = refused(lambda: legacy_close(ready), ready, "project: " + NONE_YET.format(**CLOSE))
    assert "reflection was saved" not in message
    close_decision(ready)
    assert legacy_close(ready)["status"] == "DONE"


def test_the_legacy_close_below_policy_six_makes_no_early_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path, 5)
    _start(project, "T01")
    _receipt(project, "T01")
    task_operation(
        project, "finish", "T01", expected_revision=_revision(project), evidence_record_ids=[_evidence(project, "T01")],
    )
    skip(project)

    def boom(*_: Any, **__: Any) -> list[str]:
        raise AssertionError("no early mode check below policy 6")

    monkeypatch.setattr(workspace_operations, "mode_gate_errors", boom)
    assert legacy_close(project)["status"] == "DONE"


def _reflection(project: Path) -> None:
    """A DONE candidate is validated as a closure, which reads the reflection."""
    (project / "reflection.md").write_text(PLAIN_REFLECTION, encoding="utf-8")


def test_update_cannot_set_the_project_done_without_a_close_decision(ready: Path) -> None:
    _reflection(ready)
    refused(
        lambda: update_project_data(ready, {"status": "DONE"}, expected_revision=_revision(ready)), ready,
        "project: " + NONE_YET.format(**CLOSE),
    )
    candidate = {**_state(ready), "status": "DONE"}
    refused(lambda: commit_state(ready, candidate, expected_revision=_revision(ready)), ready, "project: ")
    close_decision(ready)
    update_project_data(ready, {"status": "DONE"}, expected_revision=_revision(ready))


def test_a_close_decision_goes_stale_when_a_task_is_added(ready: Path) -> None:
    close_decision(ready)
    new = {
        "id": "T03", "name": "n", "success_criteria": "s", "verification": "v", "effect": {"kind": "none"},
        "spec_refs": [HEADING],
    }
    update_project_data(ready, {"tasks": [new]}, expected_revision=_revision(ready))
    decide(ready, by="user", subject="T03", choice="skip")
    skip(ready, "T03")
    refused(lambda: finalize(ready, PLAIN_REFLECTION), ready, "project: " + STALE.format(**CLOSE))
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)


def _maintenance(project: Path, **fields: Any) -> dict[str, Any]:
    payload = request(project, f"m-{_revision(project)}", reason="one more look", continuation=NEXT, **fields)
    return workflow(project, "maintenance", payload)


def test_a_close_decision_goes_stale_after_a_maintenance_reopen_without_a_new_task(ready: Path) -> None:
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)
    _maintenance(ready, confirmation=USER)
    _reflection(ready)
    assert _state(ready)["status"] == "PLANNING"
    assert _state(ready)["mode_counters"] == {"automated_off": 0, "reopens": 1}
    update_project_data(ready, {"status": "EXECUTING"}, expected_revision=_revision(ready))
    refused(
        lambda: update_project_data(ready, {"status": "DONE"}, expected_revision=_revision(ready)), ready,
        "project: " + STALE.format(**CLOSE),
    )
    close_decision(ready)
    update_project_data(ready, {"status": "DONE"}, expected_revision=_revision(ready))


def test_a_maintenance_reopen_ends_the_project_answer(ready: Path) -> None:
    decide(ready, by="user", kind="project-close", subject="project", choice="close")
    finalize(ready, PLAIN_REFLECTION)
    _maintenance(ready, confirmation=USER)
    # The answer of the earlier closure no longer binds: an agent record is accepted again.
    assert close_decision(ready)["outcome"] == "decision"


# --- cancel and maintenance -----------------------------------------------------------------------------


def test_cancel_is_refused_without_a_confirmation(auto: Path) -> None:
    payload = request(auto, "cancel-1", reason="stop", continuation=NEXT)
    refused(lambda: workflow(auto, "cancel", payload), auto, WORDS.format(a="cancel"))


def test_maintenance_is_refused_without_a_confirmation(ready: Path) -> None:
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)
    refused(lambda: _maintenance(ready), ready, WORDS.format(a="maintenance"))


def test_cancel_with_a_confirmation_skips_tasks_without_a_decision(auto: Path) -> None:
    tasks = [
        {"id": task_id, "status": "SKIPPED", "skip_reason": "cancelled"} for task_id in ("T01", "T02")
    ]
    payload = request(auto, "cancel-2", reason="stop", tasks=tasks, confirmation=USER, continuation=NEXT)
    workflow(auto, "cancel", payload)
    assert _state(auto)["status"] == "CANCELLED"
    spec = (auto / "spec.md").read_text(encoding="utf-8")
    assert f"cancel: stop\nsource: {USER['source']}\nresponse: {USER['response']}\n" in spec


def test_maintenance_with_a_confirmation_skips_tasks_without_a_decision(ready: Path) -> None:
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)
    new = {
        "id": "T03", "name": "n", "success_criteria": "s", "verification": "v",
        "effect": {"kind": "none", "description": None}, "status": "SKIPPED", "skip_reason": "r",
        "spec_refs": [HEADING],
    }
    _maintenance(ready, tasks=[new], confirmation=USER)
    assert _task(ready, "T03")["status"] == "SKIPPED"
    spec = (ready / "spec.md").read_text(encoding="utf-8")
    assert f"maintenance: one more look\nsource: {USER['source']}\nresponse: {USER['response']}\n" in spec


def test_cancel_and_maintenance_take_no_words_with_automated_off(tmp_path: Path) -> None:
    project = _project(tmp_path, 6)
    workflow(project, "cancel", request(project, "cancel-3", reason="stop", continuation=NEXT))
    assert _state(project)["status"] == "CANCELLED"
    assert "mode_counters" not in _state(project)


def test_settings_and_maintenance_raise_their_counters(ready: Path) -> None:
    close_decision(ready)
    finalize(ready, PLAIN_REFLECTION)
    _maintenance(ready, confirmation=USER)
    settings(ready, options={"automated": False})
    assert _state(ready)["mode_counters"] == {"automated_off": 1, "reopens": 1}


# --- consent and worktrees ------------------------------------------------------------------------------


def _external(task_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": task_id, "name": "Publish", "success_criteria": "ok", "verification": "receipt",
        "effect": {"kind": "external", "description": "Publish the page"}, "spec_refs": [HEADING], **fields,
    }


def test_an_unauthorized_external_task_gives_the_authorization_stop(ready: Path) -> None:
    update_project_data(ready, {"tasks": [_external("T03")]}, expected_revision=_revision(ready))
    moved = workflow(ready, "resume", {})
    assert moved["stop_reason"] == "authorization"
    assert moved["move_detail"] == "T03 needs the user's authorization for its effect"


def test_an_update_cannot_create_an_authorized_external_task(ready: Path) -> None:
    patch = {"tasks": [_external("T03", authorization=_explicit())]}
    refused(
        lambda: update_project_data(ready, patch, expected_revision=_revision(ready)), ready,
        "task T03: authorization can change only through 'workflow authorize' at policy 6",
    )


def _worktree_close(project: Path, path: str, decision: str) -> dict[str, Any]:
    payload = request(
        project, f"wt-{decision}", operation="close", path=path, decision=decision,
        confirmation={"source": "automated opt-in reply", "response": "keep the worktrees"},
    )
    return workflow(project, "worktree", payload)


def test_worktree_close_remove_is_refused(auto: Path, tmp_path: Path) -> None:
    path = str(tmp_path / "nowhere")
    refused(lambda: _worktree_close(auto, path, "remove"), auto, REMOVE_REFUSED)
    settings(auto, options={"automated": False})
    with pytest.raises(WorkspaceError, match="no active or kept repository worktree is recorded"):
        _worktree_close(auto, path, "remove")


def test_worktree_close_keep_is_recorded_with_the_standing_words(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    _rewrite(project, 6)
    settings(project, options={"automated": True})
    worktree_record(project, linked)
    words = {"source": "automated opt-in reply 2026-11-20", "response": "keep every recorded worktree"}
    with pytest.raises(WorkspaceError, match=r"recording remove is refused while options\.automated is on"):
        worktree_close(project, linked, "remove", confirmation=words)
    worktree_close(project, linked, "keep", confirmation=words)
    closure = _state(project)["worktrees"][0]["closure"]
    assert closure["decision"] == "keep"
    assert (closure["source"], closure["response"]) == (words["source"], words["response"])


# --- off, and the pinned Goldfish rule ------------------------------------------------------------------


def test_the_gates_are_off_with_automated_off(tmp_path: Path) -> None:
    project = _project(tmp_path, 6)
    settings(project, options={"automated": True})
    settings(project, options={"automated": False})
    _start(project, "T01")
    _receipt(project, "T01")
    _finish(project, "T01", replan="changed", reason="no decision needed")
    skip(project)
    finalize(project, PLAIN_REFLECTION)
    assert _state(project)["status"] == "DONE"


def test_a_non_fix_major_close_still_needs_the_confirmation(auto: Path) -> None:
    """Pinned: passes before and after T06. A contested Major stops for the user in automated mode too."""
    (auto / "planning.md").write_text("# Plan\n\nStatus: draft\n\nThe plan.\n", encoding="utf-8")
    if "decide" in workspace_workflows.FIELDS:  # a qualifying decision of any kind changes nothing
        decide(auto)
    opened = open_round(auto, stages=("combined",))
    with pytest.raises(WorkspaceError, match="contested or escalated needs the user's confirmation"):
        gold_close(auto, opened, [finding("F1", "Major", "CONTEST")], reports=gold_report(("combined",)))
