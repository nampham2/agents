"""Small, revision-checked edits and bounded context for the sequential coordinator."""

from __future__ import annotations

import copy
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from workspace_lib import (
    WorkspaceError,
    atomic_write_json,
    commit_candidate,
    load_json,
    read_text,
    validate_v3_state,
    validate_v4_state,
)


def _load_state(project_dir: Path) -> dict[str, Any]:
    state = load_json(project_dir / "project.json")
    if state.get("schema_version") not in (3, 4):
        raise WorkspaceError("context/update require schema v3 or v4; inspect legacy state before migration")
    validator = validate_v4_state if state["schema_version"] == 4 else validate_v3_state
    report = validator(state, project_dir, check_files=False)
    if report.errors:
        raise WorkspaceError("invalid project state:\n- " + "\n- ".join(report.errors))
    return state


def _worker_context(project_dir: Path, state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """Project one assignment without loading specification text, logs, or unrelated tasks."""
    by_id = {item["id"]: item for item in state["tasks"]}
    return {
        "role": "worker",
        "project": state["project"],
        "status": state["status"],
        "revision": state["revision"],
        "schema_version": state["schema_version"],
        "working_directory": state["working_directory"],
        "roots": {
            "target": state["working_directory"],
            "workspace": str(project_dir),
            "workspace_root": str(project_dir.parent),
        },
        "selected_task": task,
        "dependencies": [
            {key: by_id[task_id][key] for key in ("id", "name", "status", "outputs", "evidence", "receipts")}
            for task_id in task["depends_on"]
        ],
        "specification": {"root": "workspace", "path": "spec.md", "anchor": "Current specification"},
        "context_required": (
            "Coordinator must supply applicable specification constraints, user decisions, input references, "
            "and dependency findings. This projection does not select them or grant authorization."
        ),
        "execution_active": bool(
            state.get("execution", {}).get("coordinator_run") or state.get("execution", {}).get("attempts")
        ),
        "validation": "structure only; coordinator checks files, current authorization, and ownership before dispatch",
    }


def project_context(
    project_dir: Path, *, limit: int = 5, task_id: str | None = None, worker: bool = False
) -> dict[str, Any]:
    """Read one canonical snapshot; omit terminal history and cap the normal resume payload.

    Structural validation precedes traversal. This is a context view, not a substitute for file,
    evidence, or execution-journal validation. An explicitly selected task is returned in full.
    Worker mode omits the general resume view and returns only the task and direct dependency references.
    """
    if not 1 <= limit <= 20:
        raise WorkspaceError("context limit must be between 1 and 20")
    if worker and task_id is None:
        raise WorkspaceError("context --worker requires --task")
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    tasks = state["tasks"]
    selected = None
    if task_id is not None:
        selected = next((task for task in tasks if task["id"] == task_id), None)
        if selected is None:
            raise WorkspaceError(f"unknown task: {task_id}")
        if worker:
            return _worker_context(project_dir, state, selected)
    done = {task["id"] for task in tasks if task["status"] == "DONE"}
    active = [task for task in tasks if task["status"] not in ("DONE", "SKIPPED")]
    ready = [
        task["id"]
        for task in active
        if task["status"] == "TODO"
        and set(task["depends_on"]) <= done
        and task["authorization"]["status"] in ("not_required", "explicit")
    ]
    # Running and blocked work comes first; TODO ordering remains the plan's ordering.
    active.sort(key=lambda task: {"RUNNING": 0, "BLOCKED": 1, "TODO": 2}[task["status"]])
    summaries = [
        {
            "id": task["id"],
            "name": task["name"][:160],
            "status": task["status"],
            "effect": task["effect"]["kind"],
            "authorization": task["authorization"]["status"],
            "block_reason": (task["block_reason"] or "")[:300],
        }
        for task in active[:limit]
    ]
    spec_path = project_dir / "spec.md"
    spec = read_text(spec_path) if spec_path.exists() else ""
    # Old decision history need not be loaded to resume current work.
    spec = spec.split("\n## Decision history", 1)[0]
    result: dict[str, Any] = {
        "project": state["project"],
        "title": state["title"][:200],
        "status": state["status"],
        "revision": state["revision"],
        "schema_version": state["schema_version"],
        "working_directory": state["working_directory"],
        "task_counts": dict(Counter(t["status"] for t in tasks)),
        "tasks": summaries,
        "omitted_active_tasks": max(0, len(active) - limit),
        "ready": ready[:limit],
        "omitted_ready_tasks": max(0, len(ready) - limit),
        "review": {key: state["review"][key] for key in ("required", "status", "cycle")},
        "execution_active": bool(
            state.get("execution", {}).get("coordinator_run") or state.get("execution", {}).get("attempts")
        ),
        "spec": spec[:6000],
        "spec_truncated": len(spec) > 6000,
        "validation": "structure only; run research-validate on resume and at closure",
    }
    if selected is not None:
        result["selected_task"] = selected
    return result


def _merge(destination: dict[str, Any], changes: dict[str, Any]) -> None:
    """Merge objects, replace lists/scalars, preserve explicit nulls for validator checks."""
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(destination.get(key), dict):
            _merge(destination[key], value)
        else:
            destination[key] = copy.deepcopy(value)


def _new_task(changes: dict[str, Any]) -> dict[str, Any]:
    effect = changes.get("effect")
    if not isinstance(effect, dict) or effect.get("kind") not in ("none", "local_write", "destructive", "external"):
        raise WorkspaceError("new tasks need an explicit effect.kind: none, local_write, destructive, or external")
    required = effect["kind"] in ("destructive", "external")
    task: dict[str, Any] = {
        "status": "TODO",
        "depends_on": [],
        "outputs": [],
        "evidence": [],
        "receipts": [],
        "skip_reason": None,
        "block_reason": None,
        "effect": {"description": None},
        "authorization": {
            "required": required,
            "status": "pending" if required else "not_required",
            "scope": None,
            "source": None,
            "authorized_at": None,
        },
    }
    _merge(task, changes)
    return task


def update_project(
    project_dir: Path,
    patch_path: Path,
    *,
    expected_revision: int,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    """Apply only named changes, then use the existing transaction and all its guards."""
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    changes = load_json(patch_path)
    unknown = changes.keys() - {"title", "status", "review", "cancellation_reason", "predecessor", "tasks"}
    if unknown:
        raise WorkspaceError("unsupported update fields: " + ", ".join(sorted(unknown)))
    task_changes = changes.pop("tasks", [])
    if not isinstance(task_changes, list):
        raise WorkspaceError("update tasks must be an array of objects with unique ids")
    by_id = {task["id"]: task for task in state["tasks"]}
    seen: set[str] = set()
    for change in task_changes:
        if not isinstance(change, dict) or not isinstance(change.get("id"), str) or not change["id"].strip():
            raise WorkspaceError("each task update needs a non-empty id")
        task_id = change["id"]
        if task_id in seen:
            raise WorkspaceError(f"duplicate task update: {task_id}")
        seen.add(task_id)
        if task_id in by_id:
            _merge(by_id[task_id], change)
        else:
            state["tasks"].append(_new_task(change))
    _merge(state, changes)
    state["current_tasks"] = [task["id"] for task in state["tasks"] if task.get("status") == "RUNNING"]
    try:
        with tempfile.TemporaryDirectory(prefix="research-update-") as temporary:
            candidate = Path(temporary) / "candidate.json"
            atomic_write_json(candidate, state)
            return commit_candidate(
                project_dir,
                candidate,
                expected_revision=expected_revision,
                lock_timeout=lock_timeout,
            )
    except OSError as error:
        raise WorkspaceError(f"cannot prepare project update: {error}") from error
