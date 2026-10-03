"""Structured views over command evidence without changing its Markdown source of truth."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from workspace_lib import (
    CLOSURE_STEPS,
    OBSERVATION_LABEL,
    WorkspaceError,
    _done_task_ids,
    _non_empty_string,
    is_new_project,
    read_text,
)
from workspace_session import _headings, _load_state

RECORD_ID = re.compile(r"^- Record ID: (?P<id>ev-[0-9a-f]{32})$", re.MULTILINE)
ANCHOR = re.compile(r'^<a id="(?P<anchor>evidence-[0-9a-f]{32})"></a>$', re.MULTILINE)
RECORDED = re.compile(r"^- Recorded: (?P<stamp>.+)$", re.MULTILINE)
EXIT_CODE = re.compile(r"^- Exit code: (?P<code>-?\d+) \((?P<label>passed|FAILED)\)$", re.MULTILINE)
OBSERVATION = re.compile(rf"^- Observation: (?P<label>passed|FAILED) \({re.escape(OBSERVATION_LABEL)}\)$", re.MULTILINE)


def _entry_rows(text: str) -> list[dict[str, Any]]:
    headings = [(title, pos) for level, title, pos in _headings(text) if level == 2]
    rows: list[dict[str, Any]] = []
    for index, (title, start) in enumerate(headings):
        end = headings[index + 1][1] if index + 1 < len(headings) else len(text)
        owner, separator, command = title.partition(" — ")
        segment = text[start:end]
        fence = re.search(r"(?m)^ {0,3}(?:`{3,}|~{3,})", segment)
        preamble = segment[: fence.start()] if fence is not None else segment
        ids = RECORD_ID.findall(preamble)
        anchors = ANCHOR.findall(preamble)
        exits = EXIT_CODE.findall(preamble)
        observations = OBSERVATION.findall(preamble)
        stamps = RECORDED.findall(preamble)
        # Exactly one verdict line, of exactly one kind: an entry that claims both an exit code and an
        # observation is ambiguous about what kind of evidence it is, so it selects as neither.
        selectable = len(ids) == len(anchors) == 1 and len(exits) + len(observations) == 1
        if selectable and anchors[0] != f"evidence-{ids[0][3:]}":
            selectable = False
        exit_code = int(exits[0][0]) if len(exits) == 1 else None
        if selectable and exits and ((exit_code == 0) != (exits[0][1] == "passed")):
            selectable = False
        if observations:
            passed: bool | None = observations[0] == "passed" if len(observations) == 1 and not exits else None
        else:
            passed = exit_code == 0 if exit_code is not None else None
        rows.append({
            "record_id": ids[0] if len(ids) == 1 else None,
            "owner": owner,
            "command": command if separator else title,
            "recorded": stamps[0] if len(stamps) == 1 else None,
            "kind": "observation" if observations else "command",
            "exit_code": exit_code,
            "passed": passed,
            "anchor": anchors[0] if len(anchors) == 1 else None,
            "selectable": selectable,
            "legacy": not ids,
            "text": segment,
        })
    return rows


def evidence_entries(
    project_dir: Path,
    *,
    task_id: str | None = None,
    step: str | None = None,
    record_id: str | None = None,
    offset: int = 0,
    limit: int = 20,
    include_text: bool = False,
) -> dict[str, Any]:
    """List bounded evidence entry metadata or retrieve one exact generated entry."""
    if task_id is not None and step is not None:
        raise WorkspaceError("choose task or step evidence")
    if offset < 0 or not 1 <= limit <= 100:
        raise WorkspaceError("offset must be nonnegative; limit must be between 1 and 100")
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    if task_id is not None and not any(task["id"] == task_id for task in state["tasks"]):
        raise WorkspaceError(f"unknown task: {task_id}")
    if step is not None and step not in CLOSURE_STEPS:
        raise WorkspaceError(f"unknown closure step: {step}")
    path = project_dir / "evidence.md"
    rows = _entry_rows(read_text(path))
    owner = task_id if task_id is not None else step
    if owner is not None:
        rows = [row for row in rows if row["owner"] == owner]
    if record_id is not None:
        rows = [row for row in rows if row["record_id"] == record_id]
        if len(rows) != 1:
            raise WorkspaceError(f"evidence record must match exactly one entry: {record_id}")
    total = len(rows)
    selected = rows[offset : offset + limit]
    if not include_text:
        selected = [{key: value for key, value in row.items() if key != "text"} for row in selected]
    return {
        "path": str(path), "revision": state["revision"], "entries": selected, "total": total,
        "offset": offset, "next_offset": offset + limit if offset + limit < total else None,
    }


def selected_evidence_references(
    project_dir: Path,
    task_id: str,
    record_ids: list[str],
    *,
    kind: str = "command",
) -> list[dict[str, Any]]:
    """Validate explicit passing records of one kind and produce canonical evidence references.

    `kind` is `command` for `--evidence` and `observation` for `--observation`. Each flag refuses the
    other kind's records by name, so an agent's own account is never finished on by accident and a
    process result is never relabelled as one.
    """
    if not record_ids:
        raise WorkspaceError("finishing a task requires at least one explicit evidence record")
    if len(record_ids) != len(set(record_ids)):
        raise WorkspaceError("duplicate evidence record ID")
    all_rows = _entry_rows(read_text(project_dir.resolve() / "evidence.md"))
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        if row["record_id"] is not None:
            by_id.setdefault(row["record_id"], []).append(row)
    references = []
    for record_id in record_ids:
        matches = by_id.get(record_id, [])
        if len(matches) != 1:
            raise WorkspaceError(f"evidence record must match exactly one entry: {record_id}")
        row = matches[0]
        if not row["selectable"]:
            raise WorkspaceError(f"evidence record has malformed or ambiguous metadata: {record_id}")
        if row["owner"] != task_id:
            raise WorkspaceError(f"evidence record {record_id} belongs to {row['owner']}, not {task_id}")
        if row["kind"] != kind:
            if kind == "command":
                raise WorkspaceError(
                    f"evidence record {record_id} is an agent-attested observation, not command evidence; "
                    "finish with --observation to accept it as such"
                )
            raise WorkspaceError(
                f"evidence record {record_id} is a command result, not an observation; use --evidence for it"
            )
        if not row["passed"]:
            raise WorkspaceError(f"evidence record did not pass: {record_id}")
        references.append({"root": "workspace", "path": "evidence.md", "anchor": row["anchor"]})
    return references


GENERATED_ANCHOR = re.compile(r"evidence-[0-9a-f]{32}")
SAFE_TASK_ID = re.compile(r"[A-Za-z0-9_-]+")


def newly_terminal_task_errors(
    project_dir: Path, previous: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Errors and warnings for tasks that become DONE in this commit, whichever command commits them.

    `task finish` has always refused a failed, foreign or ambiguous record, but a revision-checked
    `update` setting the same task DONE with the same reference was accepted: shared validation only
    checked that a reference was well formed and its file existed. This is the one check every
    commit path shares. It judges only tasks that were not already DONE in `previous`, so terminal
    history is never re-read.

    A record that was recorded and did not pass, or belongs to another task, is an error in every
    project. A reference the tool did not generate (a legacy anchor), or one that matches no entry,
    cannot be judged: a warning for a project created before the gate cutoff, an error after it,
    where it must also leave at least one passing record. An observation-only finish needs the
    attested-finish finding `task finish --observation` writes first, because a terminal task cannot
    be annotated later.
    """
    errors: list[str] = []
    warnings: list[str] = []
    tasks = candidate.get("tasks")
    if not isinstance(tasks, list):
        return errors, warnings
    already = _done_task_ids(previous)
    gated = is_new_project(candidate)
    rows_by_anchor: dict[str, list[dict[str, Any]]] | None = None
    for task in tasks:
        if not isinstance(task, dict) or task.get("status") != "DONE":
            continue
        task_id = task.get("id")
        if not _non_empty_string(task_id) or task_id in already:
            continue
        if rows_by_anchor is None:
            rows_by_anchor = {}
            path = project_dir / "evidence.md"
            for row in _entry_rows(read_text(path)) if path.is_file() else []:
                if row["anchor"] is not None:
                    rows_by_anchor.setdefault(row["anchor"], []).append(row)
        label = f"task {task_id}"
        unjudged = errors if gated else warnings
        errors_before = len(errors)
        commands = observations = 0
        references = task.get("evidence")
        for reference in references if isinstance(references, list) else []:
            if not isinstance(reference, dict) or reference.get("root") != "workspace":
                continue
            if reference.get("path") != "evidence.md":
                continue
            anchor = reference.get("anchor")
            if not isinstance(anchor, str) or GENERATED_ANCHOR.fullmatch(anchor) is None:
                unjudged.append(
                    f"{label}: evidence anchor {anchor!r} is not a recorded evidence entry, so its verdict "
                    "cannot be checked; cite a record from record-evidence"
                )
                continue
            matches = rows_by_anchor.get(anchor, [])
            if len(matches) != 1 or not matches[0]["selectable"]:
                unjudged.append(f"{label}: evidence {anchor} matches no single well-formed recorded entry")
                continue
            row = matches[0]
            if row["owner"] != task_id:
                errors.append(f"{label}: evidence record {row['record_id']} belongs to {row['owner']}, not {task_id}")
            elif not row["passed"]:
                errors.append(
                    f"{label}: evidence record {row['record_id']} did not pass; a task cannot finish on a failed "
                    "record"
                )
            elif row["kind"] == "observation":
                observations += 1
            else:
                commands += 1
        if not gated or len(errors) > errors_before:
            continue
        if not commands and not observations:
            errors.append(f"{label}: finishing requires at least one passing evidence record from this task")
        elif not commands:
            note = project_dir / "tasks" / f"{task_id}.md"
            attested = (
                SAFE_TASK_ID.fullmatch(task_id) is not None
                and note.is_file()
                and f"research-entry: attested-{task_id.lower()}-" in read_text(note)
            )
            if not attested:
                errors.append(
                    f"{label}: finishing on agent-attested observations alone must go through "
                    "`task finish --observation`, which records the attested finish first"
                )
    return errors, warnings

