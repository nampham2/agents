"""Pure readers for resumed sessions: effect intents, crash loops, silent workers and waits.

Nothing here owns state or writes a file. An effect intent is a `research-effect` entry in the task
notes (P1); a crash loop is counted from `invocations.jsonl`, the task notes and the evidence rows
(P2); a worker is silent when its latest event is older than 30 minutes (P4); a wait is a
`research-wait` entry in the notes of a BLOCKED task (P5). Workspace files are untrusted: a
malformed entry or line is skipped, never raised, so that a resume can always run. The markers are
new names, so a 0.33.0 reader of `research-worker` lines never matches them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workspace_documents import ENTRY_MARKER_PATTERN
from workspace_evidence import _entry_rows
from workspace_invocations import INVOCATIONS_FILENAME
from workspace_lib import WorkspaceError, read_text
from workspace_session import next_move

REFERENCE = "references/durability.md"
EFFECT_MARKER = re.compile(r"^<!-- research-effect (.+) -->$", re.MULTILINE)
EFFECT_OUTCOMES = ("done", "not-done", "unknown")
EFFECT_KINDS = ("external", "destructive")
KEY_LIMIT = 256
CRASH_LOOP_THRESHOLD = 3
# An entry whose whole body is one of these markers is a record of an observation, not a finding.
MARKER_ONLY = re.compile(r"\A<!-- research-(?:worker|effect|wait) .+ -->\Z")
HEADING_STAMP = re.compile(r"^### (?P<stamp>\S+) — ", re.MULTILINE)
WAIT_MARKER = re.compile(r"^<!-- research-wait (.+) -->$", re.MULTILINE)
SILENT_AFTER_MINUTES = 30


def utcnow() -> datetime:
    """The current instant; one seam, so tests can move the clock."""
    return datetime.now(timezone.utc)


def _read(path: Path) -> str:
    """The file's text, or '' when it is missing or unreadable: a reader never fails a resume."""
    try:
        return read_text(path) if path.is_file() else ""
    except (OSError, WorkspaceError):
        return ""


def _instant(value: Any) -> "datetime | None":
    """A timezone-aware instant, or None for anything that is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def aware_instant(value: Any) -> "datetime | None":
    """A timezone-aware ISO 8601 instant parsed from text, or None: a naive time is refused, not guessed."""
    return _instant(value)


def valid_key(value: Any) -> bool:
    """An idempotency key: 1 to 256 characters on one line."""
    return isinstance(value, str) and 0 < len(value) <= KEY_LIMIT and not any(c in value for c in "\r\n")


def _notes(project: Path, task_id: str) -> str:
    return _read(project / "tasks" / f"{task_id}.md")


def _effect_entries(text: str, warnings: list[str], task_id: str) -> list[dict[str, Any]]:
    entries = []
    for match in EFFECT_MARKER.finditer(text):
        try:
            entry = json.loads(match[1])
        except ValueError:
            entry = None
        well_formed = (
            isinstance(entry, dict)
            and entry.get("op") in ("intent", "resolve")
            and valid_key(entry.get("key"))
            and (entry["op"] == "resolve" or isinstance(entry.get("effect"), str))
        )
        if not well_formed:
            warnings.append(f"{task_id}: skipped a malformed research-effect entry")
            continue
        assert isinstance(entry, dict)
        entries.append(entry)
    return entries


def open_intents(project: Path, task: dict[str, Any], warnings: "list[str] | None" = None) -> dict[str, dict[str, Any]]:
    """The task's unresolved intents by key, in the order they were recorded.

    Entries are read in file order: an intent opens its key, a `resolve` closes it. A receipt on the
    same task whose `value` equals the key also closes it, whenever it was recorded.
    """
    opened: dict[str, dict[str, Any]] = {}
    for entry in _effect_entries(_notes(project, task["id"]), warnings if warnings is not None else [], task["id"]):
        if entry["op"] == "intent":
            opened.setdefault(entry["key"], entry)
        else:
            opened.pop(entry["key"], None)
    receipts = task.get("receipts")
    values = [item.get("value") for item in receipts if isinstance(item, dict)] if isinstance(receipts, list) else []
    return {key: entry for key, entry in opened.items() if key not in values}


def _effect_intents(project: Path, state: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    return [
        {"task": task["id"], "key": key, "effect": entry["effect"], "recorded": entry.get("recorded")}
        for task in state["tasks"]
        for key, entry in open_intents(project, task, warnings).items()
    ]


def effect_intents(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every unresolved intent: an effect that may have happened, to check before any retry."""
    return _effect_intents(project, state, [])


def invocation_lines(project: Path) -> list[dict[str, Any]]:
    """The log's successful lines in file order; position, not the second-precision time, orders them."""
    lines = []
    for raw in _read(project / INVOCATIONS_FILENAME).splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and line.get("exit") == 0:
            lines.append(line)
    return lines


def _last_progress(lines: list[dict[str, Any]], task_id: str) -> int:
    """Position of the last line that is progress for the task, or -1.

    A task command names its task or the successor it started; a 0.33.0 task command names neither
    and counts for every task. A receipt names its task.
    """
    last = -1
    for position, line in enumerate(lines):
        if line.get("command") == "task":
            if "task" not in line or task_id in (line.get("task"), line.get("start_next")):
                last = position
        elif line.get("command") == "workflow" and line.get("action") == "receipt" and line.get("task") == task_id:
            last = position
    return last


def _latest_record(project: Path, task_id: str, evidence: str) -> "datetime | None":
    """The latest instant among the task's findings and evidence rows."""
    instants = []
    notes = _notes(project, task_id)
    markers = list(ENTRY_MARKER_PATTERN.finditer(notes))
    for index, marker in enumerate(markers):
        segment = notes[marker.end() : markers[index + 1].start() if index + 1 < len(markers) else len(notes)]
        heading = HEADING_STAMP.search(segment)
        if heading is None:
            continue
        _, _, body = segment[heading.start() :].partition("\n")
        if MARKER_ONLY.fullmatch(body.strip()):
            continue
        instants.append(_instant(heading["stamp"]))
    instants += [_instant(row["recorded"]) for row in _entry_rows(evidence) if row["owner"] == task_id]
    known = [instant for instant in instants if instant is not None]
    return max(known) if known else None


def crash_loops(project: Path, state: dict[str, Any], *, pending_resume: bool) -> list[dict[str, Any]]:
    """RUNNING tasks resumed at least three times without progress, with their counts.

    Counted: successful project `workflow resume` lines (not packets, not worker packets) after the
    task's last progress line, whose time is strictly later than its latest finding or evidence row.
    `pending_resume` adds the call being answered, which is not logged until it finishes.
    """
    running = [task["id"] for task in state["tasks"] if task.get("status") == "RUNNING"]
    if not running:
        return []
    lines = invocation_lines(project)
    resumes = [
        (position, _instant(line.get("at")))
        for position, line in enumerate(lines)
        if line.get("command") == "workflow" and line.get("action") == "resume" and not line.get("worker_packet")
    ]
    evidence = _read(project / "evidence.md")
    loops = []
    for task_id in running:
        last = _last_progress(lines, task_id)
        latest = _latest_record(project, task_id, evidence)
        count = sum(
            1 for position, at in resumes if position > last and (latest is None or (at is not None and at > latest))
        ) + int(pending_resume)
        if count >= CRASH_LOOP_THRESHOLD:
            loops.append({"task": task_id, "resumes": count})
    return loops


def crash_loop_detail(loops: list[dict[str, Any]]) -> str:
    named = ", ".join(f"{item['task']} {item['resumes']} times" for item in loops)
    return (
        f"resumed without progress: {named}. Record a finding or evidence for real progress, or check it is not "
        f"looping and run task block then task start; see {REFERENCE}"
    )


def durable_move(project: Path, state: dict[str, Any], *, pending_resume: bool = False) -> dict[str, Any]:
    """`next_move`, replaced by a blocked stop when a RUNNING task is in a crash loop.

    Whatever the move, each due wait and then each unresolved effect intent is appended to `move_detail`.
    """
    loops = crash_loops(project, state, pending_resume=pending_resume)
    if loops:
        move = {"continue": None, "stop_reason": "blocked", "move_detail": crash_loop_detail(loops)}
    else:
        move = next_move(state)
    move = _with_due_waits(move, waits(project, state))
    intents = "".join(
        f"; effect may have happened: {item['task']} ({item['key']})" for item in effect_intents(project, state)
    )
    return {**move, "move_detail": move["move_detail"] + intents} if intents else move


def silent_minutes(recorded: Any, now: "datetime | None" = None) -> "int | None":
    """Whole minutes since a worker's latest event, when that is more than 30; None otherwise.

    Display only: a worker writes no records, so this measures the coordinator's observations.
    """
    instant = _instant(recorded)
    if instant is None:
        return None
    minutes = ((now or utcnow()) - instant).total_seconds() / 60
    return int(minutes) if minutes > SILENT_AFTER_MINUTES else None


def _wait_entries(text: str, warnings: list[str], task_id: str) -> list[dict[str, Any]]:
    entries = []
    for match in WAIT_MARKER.finditer(text):
        try:
            entry = json.loads(match[1])
        except ValueError:
            entry = None
        well_formed = (
            isinstance(entry, dict)
            and isinstance(entry.get("on"), str)
            and bool(entry["on"].strip())
            and (entry.get("until") is None or _instant(entry["until"]) is not None)
            and (entry.get("reason") is None or isinstance(entry["reason"], str))
        )
        if not well_formed:
            warnings.append(f"{task_id}: skipped a malformed research-wait entry")
            continue
        assert isinstance(entry, dict)
        entries.append(entry)
    return entries


def _wait_after_block(lines: list[dict[str, Any]], task_id: str) -> bool:
    """Whether a successful `workflow wait` line for the task comes after its latest `task block` line.

    A project without a `task block` line for the task (an old project, or one blocked through
    `update`) falls back to the latest entry.
    """
    blocked = -1
    waited = -1
    for position, line in enumerate(lines):
        if line.get("task") != task_id:
            continue
        if line.get("command") == "task" and line.get("action") == "block":
            blocked = position
        elif line.get("command") == "workflow" and line.get("action") == "wait":
            waited = position
    return blocked < 0 or waited > blocked


def active_wait(
    project: Path,
    task: dict[str, Any],
    lines: list[dict[str, Any]],
    warnings: "list[str] | None" = None,
    now: "datetime | None" = None,
) -> "dict[str, Any] | None":
    """The wait of a BLOCKED task: its latest well-formed entry, if recorded after its latest block.

    Due when `until` is set and has passed. The reason falls back to the task's `block_reason`.
    """
    if task.get("status") != "BLOCKED" or not isinstance(task.get("id"), str):
        return None
    entries = _wait_entries(_notes(project, task["id"]), warnings if warnings is not None else [], task["id"])
    if not entries or not _wait_after_block(lines, task["id"]):
        return None
    entry = entries[-1]
    until = _instant(entry.get("until"))
    reason = entry.get("reason") or task.get("block_reason") or None
    return {
        "task": task["id"],
        "on": entry["on"],
        "until": entry.get("until"),
        "due": until is not None and until <= (now or utcnow()),
        "reason": reason,
    }


def _waits(project: Path, state: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    blocked = [task for task in state["tasks"] if task.get("status") == "BLOCKED"]
    if not blocked:
        return []
    lines = invocation_lines(project)
    now = utcnow()
    found = (active_wait(project, task, lines, warnings, now) for task in blocked)
    return [item for item in found if item is not None]


def waits(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every active wait of a BLOCKED task, with its due flag."""
    return _waits(project, state, [])


def _with_due_waits(move: dict[str, Any], found: list[dict[str, Any]]) -> dict[str, Any]:
    due = "".join(f"; wait due: {item['task']} ({item['on']})" for item in found if item["due"])
    return {**move, "move_detail": move["move_detail"] + due} if due else move


def context_view(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """The durability keys `context` shows: open intents and waits, and the reference when there is any."""
    view: dict[str, Any] = {}
    intents = effect_intents(project, state)
    if intents:
        view["effect_intents"] = intents
    found = waits(project, state)
    if found:
        view["waits"] = found
    if view:
        view["reference"] = REFERENCE
    return view


def durability_view(project: Path, state: dict[str, Any], *, pending_resume: bool) -> dict[str, Any]:
    """The keys resume adds, each omitted when empty; `reference` is present when any of them is."""
    warnings: list[str] = []
    view: dict[str, Any] = {}
    intents = _effect_intents(project, state, warnings)
    if intents:
        view["effect_intents"] = intents
    found = _waits(project, state, warnings)
    if found:
        view["waits"] = found
    loops = crash_loops(project, state, pending_resume=pending_resume)
    if loops:
        view["crash_loop"] = loops
    if view:
        view["reference"] = REFERENCE
    if warnings:
        view["durability_warnings"] = warnings
    return view
