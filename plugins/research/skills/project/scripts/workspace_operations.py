"""High-level project operations built on the canonical validation and commit path."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from workspace_documents import append_record, edit_document
from workspace_evidence import evidence_entries, selected_evidence_references
from workspace_lib import (
    WorkspaceConflict,
    WorkspaceError,
    commit_state,
    reason_reference_warnings,
    staged_lesson_lines,
    validate_project,
)
from workspace_session import _load_state


class CloseOperationError(WorkspaceError):
    """A close failure carrying truthful partial-write state for machine callers."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def _task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = next((item for item in state["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise WorkspaceError(f"unknown task: {task_id}")
    return task


def _result(
    operation: str,
    project_dir: Path,
    state: dict[str, Any],
    changed: list[str],
    *,
    selected_task_id: str | None = None,
) -> dict[str, Any]:
    by_id = {task["id"]: task for task in state["tasks"]}
    result: dict[str, Any] = {
        "operation": operation,
        "project_directory": str(project_dir.resolve()),
        "committed": True,
        "revision": state["revision"],
        "status": state["status"],
        "changed_tasks": [{"id": task_id, "status": by_id[task_id]["status"]} for task_id in changed],
        "current_tasks": state["current_tasks"],
    }
    if selected_task_id is not None:
        selected = by_id[selected_task_id]
        result.update({
            "selected_task": selected,
            "dependencies": [
                {key: by_id[dependency][key] for key in ("id", "name", "status", "outputs", "evidence", "receipts")}
                for dependency in selected["depends_on"]
            ],
            "roots": {
                "target": state["working_directory"],
                "workspace": str(project_dir),
                "workspace_root": str(project_dir.parent),
            },
        })
        from workspace_context import worktree_status

        # An assignment is the moment before the first write, which is exactly when a repository
        # target without a recorded worktree has to be noticed.
        result["worktree"] = worktree_status(state)
        result["warnings"] = list(result["worktree"]["warnings"])
    return result


def task_operation(
    project_dir: Path,
    action: str,
    task_id: str,
    *,
    expected_revision: int,
    reason: str | None = None,
    evidence_record_ids: list[str] | None = None,
    start_next: str | None = None,
    lock_timeout: float = 5.0,
    backfill: bool = False,
    note: str | None = None,
    observation_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Start, finish, block, or skip a named task with one guarded state commit.

    `backfill` finishes a task whose work already happened without `task start`: it is a start and
    a finish, each an ordinary guarded commit, with the reason saved between them.
    """
    project_dir = project_dir.resolve()
    if backfill:
        return _backfill_finish(
            project_dir, action, task_id, expected_revision=expected_revision,
            evidence_record_ids=evidence_record_ids, start_next=start_next, lock_timeout=lock_timeout, note=note,
            observation_record_ids=observation_record_ids,
        )
    state = copy.deepcopy(_load_state(project_dir))
    before_tasks = copy.deepcopy(state["tasks"])
    selected = _task(state, task_id)
    changed = [task_id]
    locked_guard = None
    if action == "start":
        selected.update(status="RUNNING", block_reason=None, skip_reason=None)
        if state["status"] in ("PLANNING", "BLOCKED", "REVIEW"):
            state["status"] = "EXECUTING"
    elif action == "finish":
        record_ids = evidence_record_ids or []
        observation_ids = observation_record_ids or []
        references, observed = _finish_references(project_dir, task_id, record_ids, observation_ids)
        if observed and not record_ids:
            # Intent first, like a backfill: the record must say this finish rests on the agent's own
            # account before the terminal commit, because a terminal task cannot be annotated later.
            digest = hashlib.sha256(",".join(sorted(observation_ids)).encode()).hexdigest()[:10]
            append_record(
                project_dir, "finding",
                f"Attested finish: {task_id} is finished on agent-attested observation(s) "
                f"{', '.join(observation_ids)} and no command evidence. They are the agent's own accounts, "
                "not process results.",
                task_id=task_id, entry_id=f"attested-{task_id.lower()}-{digest}", lock_timeout=lock_timeout,
            )
        selected["status"] = "DONE"
        selected["evidence"] = list(selected["evidence"])
        for reference in [*references, *observed]:
            if reference not in selected["evidence"]:
                selected["evidence"].append(reference)
        if start_next is not None:
            if start_next == task_id:
                raise WorkspaceError("--start-next must name a different task")
            successor = _task(state, start_next)
            successor.update(status="RUNNING", block_reason=None, skip_reason=None)
            changed.append(start_next)

        def guard(current: dict[str, Any]) -> None:
            del current
            if _finish_references(project_dir, task_id, record_ids, observation_ids) != (references, observed):
                raise WorkspaceError("selected evidence changed before task completion; reload evidence")

        locked_guard = guard
    elif action in ("block", "skip"):
        if reason is None or not reason.strip():
            raise WorkspaceError(f"{action} requires a non-empty reason")
        if action == "block":
            selected.update(status="BLOCKED", block_reason=reason.strip(), skip_reason=None)
        else:
            selected.update(status="SKIPPED", skip_reason=reason.strip(), block_reason=None)
    else:
        raise WorkspaceError("task action must be start, finish, block, or skip")
    state["current_tasks"] = [task["id"] for task in state["tasks"] if task["status"] == "RUNNING"]
    committed = commit_state(
        project_dir,
        state,
        expected_revision=expected_revision,
        lock_timeout=lock_timeout,
        locked_guard=locked_guard,
    )
    assignment = task_id if action == "start" else start_next if action == "finish" else None
    result = _result(f"task.{action}", project_dir, committed, changed, selected_task_id=assignment)
    if action == "finish" and observation_record_ids:
        result["observations"] = list(observation_record_ids)
        result["attested"] = not evidence_record_ids
    if action in ("block", "skip"):
        result["warnings"] = reason_reference_warnings(before_tasks, committed["tasks"], project_dir.parent)
    if action == "finish" and _had_failed_attempt(project_dir, task_id) and not staged_lesson_lines(project_dir):
        result["lesson_hint"] = (
            f"a recorded attempt for {task_id} failed before it passed, and nothing is staged. If it taught "
            "something, stage it now while the cause is in view: research-project stage <project-dir> "
            '--title "<one line>" --body-file -'
        )
    return result


def _finish_references(
    project_dir: Path, task_id: str, record_ids: list[str], observation_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Command and observation references for a finish, requiring at least one record across both."""
    if not record_ids and not observation_ids:
        raise WorkspaceError("finishing a task requires at least one explicit evidence record")
    # Each kind is looked up only when it has records: a command-only finish reads the evidence file
    # exactly once, as it did before observations existed.
    commands = selected_evidence_references(project_dir, task_id, record_ids) if record_ids else []
    observed = (
        selected_evidence_references(project_dir, task_id, observation_ids, kind="observation")
        if observation_ids
        else []
    )
    return commands, observed


def _had_failed_attempt(project_dir: Path, task_id: str) -> bool:
    offset: int | None = 0
    while offset is not None:
        page = evidence_entries(project_dir, task_id=task_id, offset=offset, limit=100)
        if any(entry["passed"] is False for entry in page["entries"]):
            return True
        offset = page["next_offset"]
    return False


def _backfill_finish(
    project_dir: Path,
    action: str,
    task_id: str,
    *,
    expected_revision: int,
    evidence_record_ids: list[str] | None,
    start_next: str | None,
    lock_timeout: float,
    note: str | None,
    observation_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Record a task whose work preceded `task start`, without relaxing `TODO -> DONE`.

    Two commits, so every guard an ordinary start and finish enforces still applies to each: the
    dependencies, the authorization, the evidence ownership and the `current_tasks` rule. The order
    is chosen so that no state is misleading. Everything that can be refused cheaply is refused
    first, the note is saved while the task is merely RUNNING, and only then is it finished. If the
    second commit fails the task is left RUNNING with its reason on record, and a plain `task
    finish` completes it.
    """
    if action != "finish":
        raise WorkspaceError("--backfill applies only to task finish")
    if note is None or not note.strip():
        raise WorkspaceError("--backfill requires a non-empty --note saying why the task was not started first")
    state = _load_state(project_dir)
    status = _task(state, task_id)["status"]
    if status != "TODO":
        raise WorkspaceError(
            f"--backfill is for a task that was never started; {task_id} is {status}. "
            "Use a plain task finish for a RUNNING task."
        )
    # Refused before anything is written: a record that is missing, failing or another task's.
    _finish_references(project_dir, task_id, evidence_record_ids or [], observation_record_ids or [])
    started = task_operation(
        project_dir, "start", task_id, expected_revision=expected_revision, lock_timeout=lock_timeout
    )
    try:
        append_record(
            project_dir, "finding",
            f"Backfilled: the work for {task_id} was done before it was started. Reason: {note.strip()}",
            task_id=task_id, entry_id=f"backfill-{task_id.lower()}", lock_timeout=lock_timeout,
        )
        finished = task_operation(
            project_dir, "finish", task_id, expected_revision=started["revision"],
            evidence_record_ids=evidence_record_ids, start_next=start_next, lock_timeout=lock_timeout,
            observation_record_ids=observation_record_ids,
        )
    except WorkspaceError as error:
        raise WorkspaceError(
            f"{task_id} was started by --backfill but not finished ({error}); "
            "finish it with a plain 'task finish'"
        ) from error
    finished["backfilled"] = True
    finished["revisions"] = [started["revision"], finished["revision"]]
    return finished


def close_project(
    project_dir: Path,
    *,
    expected_revision: int,
    reflection: str | None = None,
    expected_reflection_sha256: str | None = None,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    """Optionally save a reflection, commit DONE, then return closure validation."""
    project_dir = project_dir.resolve()
    if reflection is not None and expected_reflection_sha256 is None:
        raise WorkspaceError("a supplied reflection requires --expected-reflection-sha256")
    initial = _load_state(project_dir)
    if initial["revision"] != expected_revision:
        raise WorkspaceConflict(
            f"revision conflict: expected {expected_revision}, found {initial['revision']}"
        )
    reflection_result = None
    if reflection is not None:
        assert expected_reflection_sha256 is not None
        reflection_result = edit_document(
            project_dir,
            "reflection",
            expected_sha256=expected_reflection_sha256,
            body=reflection,
            lock_timeout=lock_timeout,
        )
    state = copy.deepcopy(_load_state(project_dir))
    state["status"] = "DONE"
    try:
        committed = commit_state(
            project_dir,
            state,
            expected_revision=expected_revision,
            lock_timeout=lock_timeout,
        )
    except WorkspaceError as error:
        actual = _load_state(project_dir)
        actual_payload = {key: value for key, value in actual.items() if key not in ("revision", "updated")}
        candidate_payload = {key: value for key, value in state.items() if key not in ("revision", "updated")}
        commit_landed = actual["revision"] == expected_revision + 1 and actual_payload == candidate_payload
        if commit_landed:
            result = {
                "operation": "project.close",
                "project_directory": str(project_dir),
                "committed": True,
                "reflection_saved": reflection_result is not None,
                "reflection": reflection_result,
                "revision": actual["revision"],
                "status": actual["status"],
                "validation": None,
                "error": str(error),
                "recovery": "run rebuild-index, then validate with --close --check-index",
            }
            raise CloseOperationError(
                "project state was committed, but post-commit recovery is required: " + str(error),
                result,
            ) from error
        if reflection_result is None:
            raise
        result = {
            "operation": "project.close",
            "project_directory": str(project_dir),
            "committed": False,
            "reflection_saved": True,
            "reflection": reflection_result,
            "state": {"revision": actual["revision"], "status": actual["status"]},
            "error": str(error),
        }
        raise CloseOperationError(
            "reflection was saved, but project state was not committed: " + str(error),
            result,
        ) from error
    report = validate_project(project_dir, close=True, check_index=True)
    return {
        "operation": "project.close", "project_directory": str(project_dir), "committed": True,
        "revision": committed["revision"], "status": committed["status"],
        "reflection": reflection_result,
        "validation": {"valid": report.valid, "errors": report.errors, "warnings": report.warnings},
    }
