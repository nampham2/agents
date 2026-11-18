"""Worker packets: spec references, decision bodies and the packet a worker is told to use.

A task at policy 5 names the specification sections (`spec_refs`, exact `###` heading text under
`## Current specification`) and the decision entries (`decision_refs`, `research-entry` IDs) its worker
needs. This module resolves them and assembles the packet. `workspace_lib`, `workspace_session` and
`workspace_documents` reach it by a function-local import only: it imports all three.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from workspace_documents import entry_body
from workspace_journal import document_path
from workspace_lib import WorkspaceError, project_options, read_text, task_packets_enabled
from workspace_session import _headings
from workspace_workflows import HELD_FREE_EVENTS, _attempts, _worker_lines

SPEC_SECTION = "Current specification"
# Characters of referenced sections plus decision bodies above which the packet carries a warning.
PACKET_WARN_CHARS = 12_000
TERMINAL = ("DONE", "SKIPPED")


def _spec_text(project: Path) -> str:
    path = project / "spec.md"
    if not path.exists():
        raise WorkspaceError("spec.md is missing")
    return read_text(path, preserve_newlines=False)


def spec_sections(text: str) -> tuple[dict[str, str], list[str]]:
    """The `###` sections under `## Current specification`: heading text to section text, and duplicates.

    A section runs from its heading line to the next heading of level 3 or less, so `####` children
    belong to it. The extent of `## Current specification` ends at the next level-two heading.
    """
    headings = _headings(text)
    start = next(
        (index for index, (level, title, _) in enumerate(headings) if level == 2 and title == SPEC_SECTION), None
    )
    if start is None:
        return {}, []
    end_index = next((i for i in range(start + 1, len(headings)) if headings[i][0] <= 2), len(headings))
    limit = headings[end_index][2] if end_index < len(headings) else len(text)
    sections: dict[str, str] = {}
    duplicates: list[str] = []
    for index in range(start + 1, end_index):
        level, title, offset = headings[index]
        if level != 3:
            continue
        stop = next((headings[i][2] for i in range(index + 1, end_index) if headings[i][0] <= 3), limit)
        if title in sections:
            if title not in duplicates:
                duplicates.append(title)
            continue
        sections[title] = text[offset:stop]
    return sections, duplicates


def _unique(values: object) -> list[str]:
    """Repeats removed in order; a malformed value reads as empty, because the validator reports it."""
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(item for item in values if isinstance(item, str) and item))


def _task_errors(
    task: dict[str, Any], sections: dict[str, str] | None, spec: str | None, load_error: str,
) -> list[str]:
    """Reference errors of one task; each names the task and the field."""
    label = f"task {task['id']}"
    refs = _unique(task.get("spec_refs"))
    decisions = _unique(task.get("decision_refs"))
    errors: list[str] = []
    if not refs:
        errors.append(f"{label}: spec_refs needs at least one specification heading")
    if (refs or decisions) and (sections is None or spec is None):
        return [*errors, f"{label}: spec_refs and decision_refs cannot be checked: {load_error}"]
    for ref in refs:
        if sections is not None and ref not in sections:
            errors.append(f"{label}: spec_refs names no `###` heading under `## {SPEC_SECTION}`: {ref}")
    for ref in decisions:
        try:
            entry_body(spec or "", ref)
        except WorkspaceError as error:
            errors.append(f"{label}: decision_refs {error}")
    return errors


def reference_errors(
    project: Path, state: dict[str, Any], task_ids: "list[str] | None" = None,
) -> list[str]:
    """Reference errors for the given tasks (default: every task that is not DONE or SKIPPED).

    Below policy 5 there is nothing to check. A DONE or SKIPPED task is never checked, so a later
    specification edit does not strand finished work. Two `###` headings with the same text are one
    project-level error.
    """
    if not task_packets_enabled(state):
        return []
    tasks = [
        task for task in state["tasks"]
        if task["status"] not in TERMINAL and (task_ids is None or task["id"] in task_ids)
    ]
    if not tasks:
        return []
    sections: dict[str, str] | None = None
    spec: str | None = None
    errors: list[str] = []
    load_error = ""
    try:
        spec = _spec_text(project)
        sections, duplicates = spec_sections(spec)
        errors.extend(f"spec.md: two `###` headings under `## {SPEC_SECTION}` read `{title}`" for title in duplicates)
    except WorkspaceError as error:
        load_error = str(error)
    for task in tasks:
        errors.extend(_task_errors(task, sections, spec, load_error))
    return errors


def _worker_history(project: Path, task_id: str, warnings: list[str]) -> list[dict[str, Any]]:
    """The task's `research-worker` lines in file order; damaged lines are skipped with a warning."""
    path = document_path(project, "tasks/" + task_id)
    if not path.exists():
        return []
    return _worker_lines(read_text(path), True, warnings, task_id, True)


def build_packet(project: Path, state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The complete packet of one task, or raises `WorkspaceError` for a missing or unknown reference.

    The sections and decision bodies are never truncated; above `PACKET_WARN_CHARS` the packet
    carries a warning. For a DONE or SKIPPED task an unresolved reference is dropped with a warning.
    """
    notes: list[str] = []
    warnings: list[str] = []
    terminal = task["status"] in TERMINAL
    spec = _spec_text(project)
    sections, duplicates = spec_sections(spec)
    if duplicates and not terminal:
        raise WorkspaceError(f"spec.md: two `###` headings under `## {SPEC_SECTION}` read `{duplicates[0]}`")
    if duplicates:  # a finished task is never stranded by a later spec edit: the first section is used
        warnings.append(f"spec.md has two `###` headings that read `{duplicates[0]}`; the first is used")
    refs = _unique(task.get("spec_refs"))
    decisions = _unique(task.get("decision_refs"))
    if not terminal:
        errors = _task_errors(task, sections, spec, "")
        if errors:
            raise WorkspaceError("; ".join(errors))
    chosen: list[dict[str, str]] = []
    for ref in refs:
        if ref in sections:
            chosen.append({"heading": ref, "text": sections[ref].rstrip()})
        else:
            warnings.append(f"spec_refs names no heading now, dropped: {ref}")
    bodies: list[dict[str, Any]] = []
    for ref in decisions:
        try:
            found = entry_body(spec, ref)
        except WorkspaceError:
            warnings.append(f"decision_refs names no entry now, dropped: {ref}")
            continue
        if not found["digest_ok"]:
            warnings.append(f"decision {ref}: body does not match its research-body-sha256 line")
        bodies.append({"id": ref, "body": found["body"]})
    packet_sha256 = hashlib.sha256("\n\n".join(item["text"] for item in chosen).encode("utf-8")).hexdigest()
    size = sum(len(item["text"]) for item in chosen) + sum(len(item["body"]) for item in bodies)
    if size > PACKET_WARN_CHARS:
        warnings.append(
            f"packet text is {size} characters, above {PACKET_WARN_CHARS}; nothing is truncated"
        )
    by_id = {item["id"]: item for item in state["tasks"]}
    history = _worker_history(project, task["id"], warnings)
    intents = [line for line in history if line["event"] == "intent"]
    intent = intents[-1] if intents else None
    open_attempts = [
        {key: line[key] for key in ("attempt_id", "counter", "event", "handle", "observation")}
        for line in _attempts(history).values() if line["event"] not in HELD_FREE_EVENTS
    ]
    delegating = project_options(state)["delegating"]
    if intent is not None and "packet_sha256" in intent and intent["packet_sha256"] != packet_sha256:
        message = (
            f"the latest intent {intent.get('attempt_id', '')} recorded another packet_sha256; "
            "the referenced specification text changed since the worker was dispatched"
        )
        notes.append(message)
        if delegating:
            warnings.append(message)
    if delegating:
        warnings.extend(
            f"dependency {dep} is {by_id[dep]['status']}, not DONE"
            for dep in task["depends_on"] if by_id[dep]["status"] != "DONE"
        )
    return {
        "spec_sections": chosen,
        "decisions": bodies,
        "acceptance": {"success_criteria": task.get("success_criteria"), "verification": task.get("verification")},
        "dependencies": [
            {key: by_id[dep][key] for key in ("id", "name", "status", "outputs", "evidence", "receipts")}
            for dep in task["depends_on"]
        ],
        "write_scope": {"outputs": task.get("outputs", []), "effect": task.get("effect")},
        "prohibited_effects": {"effect": task.get("effect"), "authorization": task.get("authorization")},
        "assignment_counter": task.get("assignment_counter"),
        "partial_work": {"task_notes": f"tasks/{task['id']}.md", "open_attempts": open_attempts},
        "result_format": "references/task-workers.md",
        "packet_sha256": packet_sha256,
        "chars": size,
        "notes": notes,
        "warnings": warnings,
    }
