"""High-level project operations built on the canonical validation and commit path."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from workspace_documents import edit_document
from workspace_evidence import selected_evidence_references
from workspace_lib import WorkspaceConflict, WorkspaceError, commit_state, validate_project
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
) -> dict[str, Any]:
    """Start, finish, block, or skip a named task with one guarded state commit."""
    project_dir = project_dir.resolve()
    state = copy.deepcopy(_load_state(project_dir))
    selected = _task(state, task_id)
    changed = [task_id]
    locked_guard = None
    if action == "start":
        selected.update(status="RUNNING", block_reason=None, skip_reason=None)
        if state["status"] in ("PLANNING", "BLOCKED", "REVIEW"):
            state["status"] = "EXECUTING"
    elif action == "finish":
        record_ids = evidence_record_ids or []
        references = selected_evidence_references(project_dir, task_id, record_ids)
        selected["status"] = "DONE"
        selected["evidence"] = list(selected["evidence"])
        for reference in references:
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
            actual = selected_evidence_references(project_dir, task_id, record_ids)
            if actual != references:
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
    return _result(f"task.{action}", project_dir, committed, changed, selected_task_id=assignment)


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
