"""Pure readers for resumed sessions: effect intents, crash loops, silent workers and waits.

Nothing here owns state or writes a file. An effect intent is a `research-effect` entry in the task
notes (P1); a crash loop is counted from `invocations.jsonl`, the task notes and the evidence rows
(P2); a worker is silent when its latest event is older than 30 minutes (P4); a wait is a
`research-wait` entry in the notes of a BLOCKED task (P5). Workspace files are untrusted: a
malformed entry or line is skipped, never raised, so that a resume can always run. The markers are
new names, so a 0.33.0 reader of `research-worker` lines never matches them. The readers of the
`research-worker` lines live here too, with the mode gate that every commit path runs on them (policy 6).
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
from workspace_journal import object_input, string_input
from workspace_lib import (
    WorkspaceError,
    coordinator_enabled,
    modes_enabled,
    read_text,
    stamp_transitions,
    task_durable_enabled,
    task_packets_enabled,
)
from workspace_session import default_authorization, next_move

REFERENCE = "references/durability.md"
EFFECT_MARKER = re.compile(r"^<!-- research-effect (.+) -->$", re.MULTILINE)
EFFECT_OUTCOMES = ("done", "not-done", "unknown")
# The effect kinds that take an effect intent, that a worker may not run, and that coordinator mode exempts
# from the worker-attempt rule of `task finish`.
SIDE_EFFECT_KINDS = ("external", "destructive")
KEY_LIMIT = 256
CRASH_LOOP_THRESHOLD = 3
# An entry whose whole body is one of these markers is a record of an observation, not a finding.
MARKER_ONLY = re.compile(r"\A<!-- research-(?:worker|effect|wait) .+ -->\Z")
HEADING_STAMP = re.compile(r"^### (?P<stamp>\S+) — ", re.MULTILINE)
WAIT_MARKER = re.compile(r"^<!-- research-wait (.+) -->$", re.MULTILINE)
SILENT_AFTER_MINUTES = 30
# The keys of a `worker-event` request; the stored line adds `recorded`, and from policy 4 `counter`.
WORKER_EVENT_FIELDS = frozenset({
    "task", "handle", "event", "scope", "observation", "assignment_revision", "attempt_id", "packet_sha256",
})
# Every worker-event field is required for every event. `attempt_id` is allowed everywhere but required nowhere in
# the schema: the builder demands it at policy 4 for every event except `intent`, and refuses it below policy 4.
WORKER_EVENTS = ("intent", "launched", "observed", "completed", "stopped", "failed")
WORKER_EVENT_REQUIRED = frozenset(WORKER_EVENT_FIELDS - {"attempt_id", "packet_sha256"})
WORKER_EVENT_OPTIONAL = frozenset({"attempt_id", "counter", "packet_sha256"})
PACKET_HASH = re.compile(r"[0-9a-f]{64}")
HELD_FREE_EVENTS = ("completed", "stopped")
WORKER_LINE = re.compile(r"<!-- research-worker (.+) -->[ \t]*")


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


def crash_loop_detail(loops: list[dict[str, Any]], durable: bool = False, coordinator: bool = False) -> str:
    named = ", ".join(f"{item['task']} {item['resumes']} times" for item in loops)
    if coordinator:
        # In coordinator mode a restart raises `assignment_counter` and voids a completed worker attempt.
        return (
            f"resumed without progress: {named}. Record a finding or evidence for real progress. In coordinator "
            "mode do not run task block then task start: a restart is a new assignment and voids a completed "
            "worker attempt. If the worker completed, finish the task; if its attempt is still open, record its "
            f"outcome with 'workflow worker-event'; see {REFERENCE}"
        )
    # Attempts and held state exist only at policy 4, so the sentence about them is shown there only.
    held = " (a held attempt needs stopped before the next intent)" if durable else ""
    return (
        f"resumed without progress: {named}. Record a finding or evidence for real progress, or check it is not "
        f"looping and run task block then task start{held}; see {REFERENCE}"
    )


def durable_move(project: Path, state: dict[str, Any], *, pending_resume: bool = False) -> dict[str, Any]:
    """`next_move`, replaced by a blocked stop when a RUNNING task is in a crash loop.

    Whatever the move, each due wait and then each unresolved effect intent is appended to `move_detail`.
    """
    loops = crash_loops(project, state, pending_resume=pending_resume)
    if loops:
        detail = crash_loop_detail(loops, task_durable_enabled(state), coordinator_enabled(state))
        move = {"continue": None, "stop_reason": "blocked", "move_detail": detail}
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


def _wait_entries(
    text: str, warnings: list[str], task_id: str, *, generation_required: bool = False
) -> list[dict[str, Any]]:
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
            and (not generation_required or type(entry.get("generation")) is int)
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
    durable: bool = False,
) -> "dict[str, Any] | None":
    """The wait of a BLOCKED task: its latest well-formed entry, if recorded after its latest block.

    `durable` says the project is at policy 4, where a wait counts only at the task's `block_generation`. The
    caller decides it from the project's policy, never from the key: a policy-4 task without the key has no wait.

    Due when `until` is set and has passed. The reason falls back to the task's `block_reason`.
    """
    if task.get("status") != "BLOCKED" or not isinstance(task.get("id"), str):
        return None
    # At policy 4 the task carries `block_generation` and a wait is current only when it was recorded at that
    # generation; the log rule below is for policy 3 and older, where no such key exists.
    anchored = durable
    if anchored and type(task.get("block_generation")) is not int:
        if warnings is not None:
            warnings.append(f"{task['id']}: block_generation is missing or not an integer, so no wait counts")
        return None
    entries = _wait_entries(
        _notes(project, task["id"]), warnings if warnings is not None else [], task["id"], generation_required=anchored
    )
    if anchored:
        entries = [entry for entry in entries if entry["generation"] == task["block_generation"]]
        if not entries:
            return None
    elif not entries or not _wait_after_block(lines, task["id"]):
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
    durable = task_durable_enabled(state)
    found = (active_wait(project, task, lines, warnings, now, durable) for task in blocked)
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


def worker_lines(
    text: str, durable: bool, warnings: "list[str] | None", task_id: str, packets: bool = False,
) -> list[dict[str, Any]]:
    """The `research-worker` lines of one notes document, in order.

    Below policy 4 a malformed line is an error, as before (a field error keeps its own text, bad JSON says so),
    and `attempt_id` or `counter` is an unknown key. At policy 4 a malformed line is skipped with a warning that
    names its line and cause, so one bad line cannot hide every attempt of the task: not valid JSON, a missing
    field, an unknown event, another task, `counter` without `attempt_id` (or the reverse), a counter that is
    not an integer, or a line cut short (no closing marker). Trailing spaces and a CR are tolerated.
    """
    allowed = WORKER_EVENT_FIELDS | {"recorded", "counter"}
    if not durable:
        allowed = allowed - WORKER_EVENT_OPTIONAL
    if not packets:
        allowed = allowed - {"packet_sha256"}
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.startswith("<!-- research-worker "):
            continue
        match = WORKER_LINE.fullmatch(raw.rstrip("\r"))
        if match is None:
            if durable and warnings is not None:
                warnings.append(f"tasks/{task_id}.md line {number}: research-worker line cut short (no closing marker)")
            continue
        try:
            try:
                event = json.loads(match[1])
            except ValueError as error:
                if not durable:
                    raise WorkspaceError("invalid worker event JSON") from error
                raise WorkspaceError("not valid JSON") from error
            object_input(event, allowed, WORKER_EVENT_REQUIRED, where="worker event")
            for field in ("task", "handle", "event", "scope", "observation"):
                string_input(event[field], field)
            if durable:
                if event["event"] not in WORKER_EVENTS or event["task"] != task_id:
                    raise WorkspaceError("an unknown event or another task")
                if ("attempt_id" in event) != ("counter" in event):
                    raise WorkspaceError("attempt_id and counter must come together")
                if "attempt_id" in event:
                    string_input(event["attempt_id"], "attempt_id")
                    if type(event["counter"]) is not int:
                        raise WorkspaceError("counter is not an integer")
                if "packet_sha256" in event and (
                    event["event"] != "intent"
                    or type(event["packet_sha256"]) is not str
                    or PACKET_HASH.fullmatch(event["packet_sha256"]) is None
                ):
                    raise WorkspaceError("packet_sha256 is not 64 lowercase hex characters on an intent event")
        except WorkspaceError as error:
            if not durable:
                raise
            if warnings is not None:
                warnings.append(f"tasks/{task_id}.md line {number}: damaged research-worker line ({error})")
            continue
        lines.append(event)
    return lines


def attempts(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per `attempt_id`, its latest line; a line after `completed` or `stopped` is ignored (the builder refuses it)."""
    found: dict[str, dict[str, Any]] = {}
    for line in lines:
        if "attempt_id" not in line:
            continue
        prior = found.get(line["attempt_id"])
        if prior is not None and prior["event"] in HELD_FREE_EVENTS:
            continue
        found[line["attempt_id"]] = line
    return found


def task_attempts(
    project: Path, state: dict[str, Any], task_id: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """The worker lines of one task in file order, the latest line per attempt, and the damage found."""
    damage: list[str] = []
    lines = worker_lines(
        _notes(project, task_id), task_durable_enabled(state), damage, task_id, task_packets_enabled(state)
    )
    return lines, attempts(lines), damage


# The findings of the mode gate (design section 4.3). From `task finish` each has the prefix
# `task <id> cannot finish: `; from the shared gate, `task <id>: `.
G0 = "coordinator mode cannot read the worker attempts: {damage}; repair or remove that line in tasks/{task}.md by hand"
G1 = (
    "coordinator mode: attempt {attempt} is still open (latest event {event}); record stopped for it with "
    "'workflow worker-event'"
)
G2 = (
    "coordinator mode needs a completed worker attempt for the current assignment (assignment_counter {counter}) "
    "and found none; dispatch a fresh worker and record its events with 'workflow worker-event', or turn "
    "coordinator off with 'workflow settings' and the user's words"
)
R2 = (
    "coordinator mode needs a replan statement on a finished task; review the tasks that have not started, then "
    "use 'task finish --replan none' or '--replan changed' with --replan-reason \"<one line>\""
)
K1 = (
    "coordinator mode: effect.kind cannot change from {old} to {new} once the task has an assignment "
    "(assignment_counter {counter}); a worker may not run an exempt step. Skip this task and add a new task for "
    "the {new} step"
)
C0 = (
    "assignment_counter is written by the tool: expected {expected}, found {found}; leave it as the tool stamps it "
    "(task start, task finish and update write it)"
)
A1 = (
    "authorization can change only through 'workflow authorize' at policy 6; this {action} would set it to "
    "{status}. Create or reset a task with its default authorization, then authorize it with the user's words"
)


def _effect_kind(task: dict[str, Any]) -> Any:
    effect = task.get("effect")
    return effect.get("kind") if isinstance(effect, dict) else None


def _attempt_findings(project: Path, state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """The worker-attempt rule: a completed attempt on the current assignment counter and none open."""
    _, found, damage = task_attempts(project, state, task["id"])
    if damage:
        # A skipped line can hide an open attempt; the event builder refuses for the same reason.
        return [G0.format(damage=damage[0], task=task["id"])]
    counter = task.get("assignment_counter")
    findings = [
        G1.format(attempt=attempt, event=line["event"])
        for attempt, line in found.items() if line["event"] not in HELD_FREE_EVENTS
    ]
    if not any(line["event"] == "completed" and line["counter"] == counter for line in found.values()):
        findings.append(G2.format(counter=counter))
    return findings


def task_finish_findings(project: Path, state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Why the modes refuse `task` becoming DONE from `state`; empty when it may (section 4.3).

    `state` is the state before the commit and `task` the task as the candidate holds it. Every finished task
    needs a replan statement; a task whose effect kind is not `external` or `destructive` also needs the worker
    attempts the coordinator recorded.
    """
    findings = [] if "replan" in task else [R2]
    if _effect_kind(task) not in SIDE_EFFECT_KINDS:
        findings.extend(_attempt_findings(project, state, task))
    return findings


def _stamped_counter(before: "dict[str, Any] | None", task: dict[str, Any]) -> "int | None":
    """The `assignment_counter` the tool stamps on `task`, or None when the stored task cannot be stamped."""
    try:
        return stamp_transitions(before, task)["assignment_counter"]
    except WorkspaceError:
        return None  # a stored counter or generation that is not an integer: the validator reports it


def _authorization_status(value: Any) -> Any:
    return value.get("status") if isinstance(value, dict) else value


def mode_gate_errors(
    project: Path, previous: dict[str, Any], candidate: dict[str, Any], action: "str | None" = None,
) -> list[str]:
    """Every mode refusal of one commit, each labelled with its task or `project` (section 4.3).

    Policy 6 only. Every policy-6 commit keeps the stamped `assignment_counter` (C0) and lets only `authorize`
    grant consent (A1). With `coordinator` on, a started task cannot change into an exempt effect kind (K1) and a
    task that becomes DONE passes `task_finish_findings`. `action` is the journaled action name, None for a task
    command, `update`, `commit` and the legacy `close`.
    """
    tasks = candidate.get("tasks") if isinstance(candidate, dict) else None
    if not modes_enabled(candidate) or not isinstance(tasks, list):
        return []
    stored = previous.get("tasks")
    before = {
        task["id"]: task for task in (stored if isinstance(stored, list) else [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    coordinator = coordinator_enabled(candidate)
    errors: list[str] = []
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            continue
        old = before.get(task["id"])
        findings: list[str] = []
        expected = _stamped_counter(old, task)
        found = task.get("assignment_counter")
        if expected is not None and (type(found) is not int or found != expected):
            findings.append(C0.format(expected=expected, found=found))
        authorization = task.get("authorization")
        kind = _effect_kind(task)
        if action != "authorize" and authorization != default_authorization(kind) and (
            old is None or authorization != old.get("authorization")
        ):
            findings.append(A1.format(action=action or "update/commit", status=_authorization_status(authorization)))
        if coordinator:
            counter = old.get("assignment_counter") if old is not None else None
            if (
                old is not None and type(counter) is int and counter >= 1
                and _effect_kind(old) not in SIDE_EFFECT_KINDS and kind in SIDE_EFFECT_KINDS
            ):
                findings.append(K1.format(old=_effect_kind(old), new=kind, counter=counter))
            if task.get("status") == "DONE" and (old is None or old.get("status") != "DONE"):
                findings.extend(task_finish_findings(project, previous, task))
        errors.extend(f"task {task['id']}: {finding}" for finding in findings)
    return errors
