"""Small, revision-checked edits and bounded context for the sequential coordinator."""

from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path
from typing import Any

from workspace_lib import (
    CLOSURE_STEPS,
    WorkspaceError,
    commit_state,
    document_sha256,
    load_json,
    read_text,
    validate_project,
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
    project_dir: Path, *, limit: int = 5, task_id: str | None = None, worker: bool = False,
    task_only: bool = False,
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
    if task_only and task_id is None:
        raise WorkspaceError("context --task-only requires --task")
    if task_only and worker:
        raise WorkspaceError("choose --task-only or --worker")
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    tasks = state["tasks"]
    selected = None
    if task_id is not None:
        selected = next((task for task in tasks if task["id"] == task_id), None)
        if selected is None:
            raise WorkspaceError(f"unknown task: {task_id}")
        if worker or task_only:
            result = _worker_context(project_dir, state, selected)
            if task_only:
                result["role"] = "coordinator"
            return result
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
    history = next(
        (pos for level, title, pos in _headings(spec) if level == 2 and title == "Decision history"), len(spec)
    )
    spec = spec[:history]
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


def validated_project_context(project_dir: Path, *, limit: int = 5) -> dict[str, Any]:
    """Return bounded resume context plus full project validation findings."""
    project_dir = project_dir.resolve()
    result = project_context(project_dir, limit=limit)
    report = validate_project(project_dir)
    after = _load_state(project_dir)
    if after["revision"] != result["revision"]:
        raise WorkspaceError("project changed while context was validated; reload context")
    documents: dict[str, Any] = {}
    for name in ("spec", "evidence", "reflection"):
        path = project_dir / f"{name}.md"
        if path.exists():
            content = read_text(path, preserve_newlines=True)
            documents[name] = {
                "path": str(path),
                "exists": True,
                "sha256": document_sha256(content),
            }
        else:
            documents[name] = {"path": str(path), "exists": False, "sha256": "missing"}
    result.update({
        "roots": {
            "target": result["working_directory"],
            "workspace": str(project_dir),
            "workspace_root": str(project_dir.parent),
        },
        "documents": documents,
        "validation": {"valid": report.valid, "errors": report.errors, "warnings": report.warnings},
    })
    return result


def _headings(text: str) -> list[tuple[int, str, int]]:
    """Return ATX headings outside fenced code, including their character offsets."""
    headings = []
    fence = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
        else:
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                headings.append((len(heading[1]), heading[2], offset))
        offset += len(line)
    return headings


def read_project_text(
    project_dir: Path, document: str, *, section: str | None = None,
    task_id: str | None = None, step: str | None = None, offset: int = 0, max_chars: int = 4000,
) -> dict[str, Any]:
    """Page selected specification sections or actual evidence; never summarize the source."""
    if document not in ("spec", "evidence"):
        raise WorkspaceError("document must be spec or evidence")
    if offset < 0 or not 1 <= max_chars <= 20000:
        raise WorkspaceError("offset must be nonnegative; max-chars must be between 1 and 20000")
    if (document == "spec" and (task_id is not None or step is not None)) or (
        document == "evidence" and section is not None
    ) or (task_id is not None and step is not None):
        raise WorkspaceError("use --section for spec; --task or --step for evidence")
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    if task_id is not None and not any(task["id"] == task_id for task in state["tasks"]):
        raise WorkspaceError(f"unknown task: {task_id}")
    if step is not None and step not in CLOSURE_STEPS:
        raise WorkspaceError(f"unknown closure step: {step}")
    path = project_dir / f"{document}.md"
    text = read_text(path)
    headings = _headings(text)
    if document == "spec":
        # Ordinary reads exclude decision history. It remains explicitly retrievable.
        history = next((pos for level, title, pos in headings if level == 2 and title == "Decision history"), len(text))
        if section != "Decision history":
            text = text[:history]
            headings = [heading for heading in headings if heading[2] < history]
        if section is not None:
            matches = [(level, pos) for level, title, pos in headings if title == section]
            if len(matches) != 1:
                raise WorkspaceError(f"section must match exactly one heading: {section}")
            level, start = matches[0]
            end = next((pos for depth, _, pos in headings if pos > start and depth <= level), len(text))
            text = text[start:end]
    elif task_id is not None or step is not None:
        owner = task_id if task_id is not None else step
        entries = [(title, pos) for level, title, pos in headings if level == 2]
        text = "".join(
            text[pos:entries[index + 1][1] if index + 1 < len(entries) else len(text)]
            for index, (title, pos) in enumerate(entries)
            if title == owner or title.startswith(f"{owner} — ")
        )
    if offset > len(text):
        raise WorkspaceError("offset is beyond the selected text; reload after source changes")
    end = min(len(text), offset + max_chars)
    return {
        "path": str(path), "revision": state["revision"], "text": text[offset:end],
        "total_chars": len(text), "offset": offset, "next_offset": end if end < len(text) else None,
        "truncated": offset > 0 or end < len(text),
    }


def list_projects(
    workspace_root: Path, *, query: str = "", status: str | None = None, limit: int = 10, offset: int = 0,
) -> dict[str, Any]:
    """Bound discovery output without trusting the generated index or hiding unreadable records."""
    if not 1 <= limit <= 100 or offset < 0:
        raise WorkspaceError("limit must be between 1 and 100; offset must be nonnegative")
    rows: list[dict[str, Any]] = []
    try:
        for directory in sorted(workspace_root.resolve().iterdir(), reverse=True):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if (directory / "project.json").is_file():
                try:
                    state = load_json(directory / "project.json")
                    fields = ("project", "title", "status", "working_directory")
                    if any(not isinstance(state.get(key), str) or not state[key].strip() for key in fields):
                        raise WorkspaceError("missing or malformed discovery fields")
                    row = {key: state[key] for key in fields}
                except WorkspaceError:
                    row = {"project": directory.name, "title": "Unreadable project.json", "status": "INVALID"}
            elif (directory / "00_meta.yaml").is_file() and (directory / "02_task_plan.md").is_file():
                row = {"project": directory.name, "title": "Legacy project; inspect files", "status": "LEGACY"}
            else:
                continue
            row["path"] = str(directory)
            if row["status"] not in ("INVALID", "LEGACY") and (
                (status is not None and row["status"] != status)
                or query.casefold() not in " ".join(row.values()).casefold()
            ):
                continue
            rows.append({
                **{key: value[:500] for key, value in row.items() if key != "path"}, "path": row["path"],
                "truncated_fields": [key for key, value in row.items() if key != "path" and len(value) > 500],
            })
    except OSError as error:
        raise WorkspaceError(f"cannot list projects: {error}") from error
    return {"projects": rows[offset:offset + limit], "total": len(rows), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None}


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
    changes = load_json(patch_path)
    return update_project_data(
        project_dir,
        changes,
        expected_revision=expected_revision,
        lock_timeout=lock_timeout,
    )


def update_project_data(
    project_dir: Path,
    patch: dict[str, Any],
    *,
    expected_revision: int,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    """Apply a mapping patch without requiring the caller to create a temporary file."""
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    if not isinstance(patch, dict):
        raise WorkspaceError("update patch must be a JSON object")
    changes = copy.deepcopy(patch)
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
    return commit_state(
        project_dir,
        state,
        expected_revision=expected_revision,
        lock_timeout=lock_timeout,
    )
