"""Structured views over command evidence without changing its Markdown source of truth."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from workspace_lib import CLOSURE_STEPS, WorkspaceError, read_text
from workspace_session import _headings, _load_state

RECORD_ID = re.compile(r"^- Record ID: (?P<id>ev-[0-9a-f]{32})$", re.MULTILINE)
ANCHOR = re.compile(r'^<a id="(?P<anchor>evidence-[0-9a-f]{32})"></a>$', re.MULTILINE)
RECORDED = re.compile(r"^- Recorded: (?P<stamp>.+)$", re.MULTILINE)
EXIT_CODE = re.compile(r"^- Exit code: (?P<code>-?\d+) \((?P<label>passed|FAILED)\)$", re.MULTILINE)


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
        stamps = RECORDED.findall(preamble)
        selectable = len(ids) == len(anchors) == len(exits) == 1
        if selectable and anchors[0] != f"evidence-{ids[0][3:]}":
            selectable = False
        exit_code = int(exits[0][0]) if len(exits) == 1 else None
        if selectable and ((exit_code == 0) != (exits[0][1] == "passed")):
            selectable = False
        rows.append({
            "record_id": ids[0] if len(ids) == 1 else None,
            "owner": owner,
            "command": command if separator else title,
            "recorded": stamps[0] if len(stamps) == 1 else None,
            "exit_code": exit_code,
            "passed": exit_code == 0 if exit_code is not None else None,
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


def selected_evidence_references(project_dir: Path, task_id: str, record_ids: list[str]) -> list[dict[str, Any]]:
    """Validate explicit passing records and produce canonical evidence references."""
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
        if not row["passed"]:
            raise WorkspaceError(f"evidence record did not pass: {record_id}")
        references.append({"root": "workspace", "path": "evidence.md", "anchor": row["anchor"]})
    return references
