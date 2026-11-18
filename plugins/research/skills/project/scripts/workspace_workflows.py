"""Named lifecycle compositions. Agents supply judgment; scripts persist and validate facts."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from memory_search import memory_health
from workspace_ci import ci_checks
from workspace_context import (
    WORKER_KEYS,
    checkpoint_freshness,
    fingerprint,
    infer_stop_reason,
    next_hint,
    render_checkpoint,
    resume_bundle,
    selected_read,
    worktree_closure_findings,
    worktree_observation,
    worktree_registered,
)
from workspace_delivery import (
    CONFIRM_HINTS,
    DELIVERY_OPERATIONS,
    KEY_HINTS,
    PATH_HINT,
    RETARGET_HINTS,
    abandoned_note,
    delivery_operation,
    removal_findings,
    required_for,
)
from workspace_documents import (
    LEGACY_PLANNING_HINT,
    _replace_sections,
    _section_span,
    append_staged_section,
    follow_up_items,
    names_only_staged,
    near_follow_up_titles,
    reflection_lesson_sections,
)
from workspace_durability import (
    EFFECT_KINDS,
    EFFECT_OUTCOMES,
    KEY_LIMIT,
    aware_instant,
    open_intents,
    silent_minutes,
    valid_key,
)
from workspace_fields import ACTION_NESTED, NESTED_INPUTS, NESTED_REQUIRED
from workspace_goldfish import (
    OPERATION_FIELDS,
    OPERATION_REQUIRED,
    amendment_details,
    confirm_gate_errors,
    delivery_gate_errors,
    goldfish_findings,
    goldfish_operation,
    residual_findings,
)
from workspace_invocations import note
from workspace_journal import (
    READ_INPUTS,
    document_path,
    journal_path,
    list_input,
    object_input,
    recover_operation,
    run_operation,
    schema_input,
    snapshot,
    string_input,
)
from workspace_lib import (
    DELIVERY_TARGETS,
    PLANNING_STATUS_PATTERN,
    PROJECT_TRANSITIONS,
    DeliveryRules,
    WorkspaceConflict,
    WorkspaceError,
    check_state_candidate,
    confirmation_marker,
    delivery_rules,
    document_conflict_message,
    document_sha256,
    fallback_errors,
    has_review_rules,
    has_target_rules,
    is_legacy_planning_layout,
    is_retro_gated,
    load_json,
    memory_staging_warnings,
    now_iso,
    options_errors,
    planning_path,
    project_options,
    proposal_digest,
    proposal_marker,
    reason_reference_warnings,
    retro_section_findings,
    risk_errors,
    staging_enabled,
    stale_confirmations,
    task_durable_enabled,
    task_packets_enabled,
    validate_project,
)
from workspace_operations import _task, migration_plan, research_task_conflicts, rules_diff, target_conflicts
from workspace_session import _headings, _load_state, prepare_update

# Shortest reflection `finalize` accepts: enough for an outcome and a limitation, not a bare token.
REFLECTION_MIN_CHARS = 200
COMMON = {"id", "expected_revision", "tokens"}
# Every write action needs these two; per-action rules are checked when the operation runs.
WRITE_REQUIRED = {"id", "expected_revision"}
FIELDS = {
    "checkpoint": {"continuation"},
    "round": {"sections", "decisions", "planning", "architecture", "continuation"},
    "confirm": {
        "kind", "proposal_sha256", "requirements_sha256", "planning_sha256", "architecture_sha256", "review_id",
        "response", "source", "scope", "continuation", "delivery_target",
    },
    "retarget": {"delivery_target", "reason", "confirmation"},
    "settings": {"review_risk", "review_fallback", "options", "confirmation"},
    "correct": {"task", "replacement", "reason", "continuation"},
    "reconcile": {"patch", "decision", "continuation"},
    "worker-event": {
        "task", "handle", "event", "scope", "observation", "assignment_revision", "attempt_id", "packet_sha256",
    },
    "authorize": {"task", "authorization", "continuation"},
    "receipt": {"task", "receipt"},
    "effect": {"operation", "task", "key", "effect", "outcome", "note"},
    "wait": {"task", "on", "until", "reason"},
    "review": {"reviewer", "version", "scope", "findings", "status", "evidence", "required"},
    "goldfish": set().union(*OPERATION_FIELDS.values()),
    "finalize": {"reflection", "continuation"},
    "report": {"sections", "graph"},
    "maintenance": {"tasks", "reason", "continuation"},
    "cancel": {"tasks", "reason", "continuation"},
    "assess": {"topic", "disposition", "reason", "application"},
    "worktree": {
        "operation", "kind", "role", "repository", "path", "branch", "confirmation", "decision", "continuation",
        "host", "url", "number", "exempt", "files", "base", "base_moved", "alpha", "reason", "version", "merge_state",
        "method", "evidence", "merged_head", "replaces_exemption",
    },
}
# Derived, not listed twice: an action is a read action exactly when it has an input table.
READ_ACTIONS = set(READ_INPUTS)

# Some actions have a discriminator value whose keys the runtime demands on top of the universal
# ones, and `workflow <action> --schema` must print them per value: a caller that never learns
# `path`, `branch` or `review_id` until a refusal has already staged the operation learns it too
# late for a clean retry. Each entry is the complete sorted key set of that variant, so the whole
# request can be built from one entry. Listing a key the value does not always need (alpha's
# `files`, merge's `method` and `evidence`) can only over-report: the runtime ignores an extra
# allowed key outside its condition, while omitting one is the recorded friction. `architecture` is
# `kind=planning` under the name the runtime still accepts, and an alignment carries either
# `planning_sha256` or its old name `architecture_sha256`, never both, so the table names the
# current one.
CONFIRM_REQUIRED = frozenset({"kind", "review_id", "response", "scope", "source"})
WORKTREE_REQUIRED = frozenset({"confirmation", "operation", "path"})
RETARGET_REQUIRED = frozenset({"confirmation", "delivery_target", "reason"})
SETTINGS_REQUIRED = frozenset({"confirmation"})
CONFIRM_REQUIRED_BY = {
    "kind=requirements": frozenset({"proposal_sha256"}),
    "kind=planning": frozenset({"proposal_sha256"}),
    "kind=architecture": frozenset({"proposal_sha256"}),
    "kind=alignment": frozenset({"planning_sha256", "requirements_sha256"}),
}
WORKTREE_REQUIRED_BY = {
    "operation=record": frozenset({"branch"}),
    "operation=close": frozenset({"decision"}),
    "operation=pull_request": frozenset({"host", "number", "url"}),
    "operation=pull_request,exempt=no_remote": frozenset({"exempt"}),
    "operation=pull_request,exempt=local_only": frozenset({"exempt"}),
    "operation=alpha": frozenset({"alpha", "base", "files", "reason"}),
    "operation=alpha,exempt=true": frozenset({"exempt", "reason"}),
    "operation=release": frozenset({"version"}),
    "operation=release,replaces_exemption=true": frozenset({"files", "replaces_exemption", "version"}),
    "operation=merge": frozenset({"evidence", "merge_state", "merged_head", "method"}),
}
GOLDFISH_REQUIRED = frozenset({"operation"})
GOLDFISH_REQUIRED_BY = {f"operation={name}": frozenset(keys) for name, keys in OPERATION_REQUIRED.items()}
# Every worker-event field is required for every event. The `event=<name>` entries add no key; they
# are how `--schema` names the closed event vocabulary without a new top-level schema key.
WORKER_EVENTS = ("intent", "launched", "observed", "completed", "stopped", "failed")
# `attempt_id` is allowed everywhere but required nowhere in the schema: the builder demands it at policy 4 for
# every event except `intent`, and refuses it below policy 4. The stored line adds `attempt_id` and `counter`.
WORKER_EVENT_REQUIRED = frozenset(FIELDS["worker-event"] - {"attempt_id", "packet_sha256"})
WORKER_EVENT_OPTIONAL = frozenset({"attempt_id", "counter", "packet_sha256"})
PACKET_HASH = re.compile(r"[0-9a-f]{64}")
HELD_FREE_EVENTS = ("completed", "stopped")
# The events each latest state accepts (section 5 of the design); `stopped` is the only one a superseded
# attempt accepts, and `failed` stays held until it is stopped.
WORKER_EVENT_FROM = {
    "launched": ("intent",),
    "observed": ("intent", "launched", "observed"),
    "completed": ("intent", "launched", "observed"),
    "failed": ("intent", "launched", "observed"),
    "stopped": ("intent", "launched", "observed", "failed"),
}
WORKER_EVENT_REQUIRED_BY = {f"event={name}": frozenset() for name in WORKER_EVENTS}
# P1: an effect intent before an external or destructive effect, and its resolve once checked.
EFFECT_REQUIRED = frozenset({"operation", "task", "key"})
EFFECT_REQUIRED_BY = {"operation=intent": frozenset({"effect"}), "operation=resolve": frozenset({"outcome"})}
# P5: what a BLOCKED task waits for; no operation, no variants and no clear (leaving BLOCKED ends it).
WAIT_REQUIRED = frozenset({"task", "on"})
ALWAYS_REQUIRED = {
    "confirm": CONFIRM_REQUIRED, "worktree": WORKTREE_REQUIRED, "goldfish": GOLDFISH_REQUIRED,
    "retarget": RETARGET_REQUIRED, "settings": SETTINGS_REQUIRED,
    "worker-event": WORKER_EVENT_REQUIRED, "effect": EFFECT_REQUIRED, "wait": WAIT_REQUIRED,
}
REQUIRED_BY = {
    "confirm": CONFIRM_REQUIRED_BY, "worktree": WORKTREE_REQUIRED_BY, "goldfish": GOLDFISH_REQUIRED_BY,
    "worker-event": WORKER_EVENT_REQUIRED_BY, "effect": EFFECT_REQUIRED_BY,
}
# Workflow writes that name a task put it on their invocation line; a receipt there is crash-loop progress.
TASK_NOTED_ACTIONS = ("effect", "wait", "receipt", "authorize", "worker-event")


def input_schema(action: str) -> dict[str, Any]:
    """Allowed and required top-level keys of a workflow action, from the tables validation uses."""
    if action in FIELDS:
        allowed = COMMON | FIELDS[action]
        required = WRITE_REQUIRED | ALWAYS_REQUIRED.get(action, frozenset())
        variants = {name: sorted(required | keys) for name, keys in REQUIRED_BY.get(action, {}).items()}
        note = (
            "write action: further per-action rules are checked when it runs "
            "(see references/automation.md)"
        )
    else:
        allowed, required = READ_INPUTS[action]
        variants = {}
        note = "read action: the keys below are the whole request"
    nested = {
        key: {"allowed": sorted(NESTED_INPUTS[shape][0]), "required": sorted(NESTED_INPUTS[shape][1])}
        for key, shape in ACTION_NESTED.get(action, ())
    }
    return {
        "action": action,
        "allowed": sorted(allowed),
        "required": sorted(required),
        "required_by": variants,
        "nested": nested,
        "note": note,
    }


def _entry(text: str, entry_id: str, body: str, *, decision: bool = False) -> str:
    journal_path(Path("."), entry_id)
    body = string_input(body).strip()
    marker = f"<!-- research-entry: {entry_id} -->"
    if marker in text:
        raise WorkspaceError("entry ID already exists; use the original operation ID for retries")
    entry = (
        f"{marker}\n<!-- research-body-sha256: {document_sha256(body)} -->\n"
        f'<a id="{entry_id}"></a>\n### {now_iso()} — {entry_id}\n\n{body}\n'
    )
    end = _section_span(text, "Decision history")[3] if decision else len(text)
    return text[:end].rstrip() + "\n\n" + entry + text[end:]


def preview(project: Path, patch: dict[str, Any], expected_revision: int) -> dict[str, Any]:
    """Show only changed fields and canonical validation; never write candidate files."""
    current = _load_state(project)
    candidate = prepare_update(current, patch)
    report = check_state_candidate(project, candidate, expected_revision=expected_revision)
    before = {task["id"]: task for task in current["tasks"]}
    return {
        "revision": current["revision"],
        "valid": report.valid,
        "errors": report.errors,
        "warnings": report.warnings
        + reason_reference_warnings(current["tasks"], candidate["tasks"], project.resolve().parent),
        "project": {
            key: {"before": current[key], "after": value}
            for key, value in candidate.items()
            if key != "tasks" and current[key] != value
        },
        "tasks": [
            {
                "id": task["id"],
                "changes": {
                    key: {"before": before.get(task["id"], {}).get(key), "after": value}
                    for key, value in task.items()
                    if before.get(task["id"], {}).get(key) != value
                },
            }
            for task in candidate["tasks"]
            if before.get(task["id"]) != task
        ],
    }


def impact(project: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Compute graph candidates and declared path overlaps, not semantic invalidation."""
    schema_input(request, "impact")
    state = _load_state(project)
    ids = {string_input(value, "tasks") for value in list_input(request["tasks"], field="tasks")}
    for task_id in ids:
        _task(state, task_id)
    while True:
        expanded = ids | {task["id"] for task in state["tasks"] if ids.intersection(task["depends_on"])}
        if expanded == ids:
            break
        ids = expanded
    paths = list_input(request.get("paths", []))
    for ref in paths:
        object_input(ref, {"root", "path"}, {"root", "path"})
        string_input(ref["root"], "root")
        string_input(ref["path"], "path")
    overlaps = []
    for task in state["tasks"]:
        reads = [{"root": "target", "path": path} for path in task.get("reads", [])]
        for declared in [*reads, *task["outputs"]]:
            for ref in paths:
                left, right = Path(declared["path"]), Path(ref["path"])
                if declared["root"] == ref["root"] and (left.is_relative_to(right) or right.is_relative_to(left)):
                    overlaps.append({"task": task["id"], "reference": declared})
    return {
        "revision": state["revision"],
        "candidates": [
            {key: task[key] for key in ("id", "status", "depends_on", "outputs", "evidence")}
            for task in state["tasks"]
            if task["id"] in ids
        ],
        "overlaps": overlaps,
        "review": state["review"],
        "coverage": "declared dependencies and paths only; no automatic invalidation",
    }


def readiness_worktrees(state: dict[str, Any], *, rules: DeliveryRules) -> list[dict[str, Any]]:
    """Active or kept repository worktrees with whether they exist and which delivery records removal lacks.

    Only well-formed entries are read: readiness reports on a project whose state may be malformed.
    `exists` is None when Git could not be consulted.
    """
    items = state.get("worktrees")
    listed: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not (isinstance(item, dict) and isinstance(item.get("path"), str)):
            continue
        if item.get("status") not in ("active", "kept") or item.get("kind") in (None, "none"):
            continue
        seen = worktree_observation(Path(item["path"]))
        listed.append({
            "path": item["path"],
            "exists": seen["exists"] if seen["available"] else None,
            "missing": removal_findings(item, rules=rules),
        })
    return listed


def readiness_checks(state: dict[str, Any], required: Any) -> dict[str, Any]:
    """Required check names against the CI jobs of the active target worktree's recorded path."""
    if required is not None and not (isinstance(required, list) and all(isinstance(n, str) and n for n in required)):
        raise WorkspaceError("required_checks must be an array of non-empty strings")
    items = state.get("worktrees")
    entry = next(
        (
            item
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict) and item.get("role") == "target" and item.get("status") == "active"
            and item.get("kind") not in (None, "none") and isinstance(item.get("path"), str)
        ),
        None,
    )
    if entry is None:
        return ci_checks(None, required, reason="no active target worktree entry; no CI file was read")
    root = Path(entry["path"])
    if not root.is_dir():
        return ci_checks(None, required, reason=f"worktree {root} no longer exists; no CI file was read")
    return ci_checks(root, required)


def readiness(project: Path, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Group existing closure findings without introducing a second gate."""
    report = validate_project(project, close=True, check_index=True)
    groups: dict[str, list[dict[str, str]]] = {}
    for level, findings in (("error", report.errors), ("warning", report.warnings)):
        for finding in findings:
            group = next(
                (
                    key
                    for key in (
                        "worktree", "receipt", "review", "retro", "reflection", "memory", "index", "output", "task",
                    )
                    if key in finding.lower()
                ),
                "project",
            )
            groups.setdefault(group, []).append({"level": level, "finding": finding})
    try:
        state = load_json(project / "project.json")
    except WorkspaceError:
        state = {}
    return {
        "valid": report.valid,
        "groups": groups,
        "worktrees": readiness_worktrees(state, rules=delivery_rules(state)),
        "checks": readiness_checks(state, (request or {}).get("required_checks")),
    }


WORKER_LINE = re.compile(r"<!-- research-worker (.+) -->[ \t]*")


def _worker_lines(
    text: str, durable: bool, warnings: "list[str] | None", task_id: str, packets: bool = False,
) -> list[dict[str, Any]]:
    """The `research-worker` lines of one notes document, in order.

    Below policy 4 a malformed line is an error, as before (a field error keeps its own text, bad JSON says so),
    and `attempt_id` or `counter` is an unknown key. At policy 4 a malformed line is skipped with a warning that
    names its line and cause, so one bad line cannot hide every attempt of the task: not valid JSON, a missing
    field, an unknown event, another task, `counter` without `attempt_id` (or the reverse), a counter that is
    not an integer, or a line cut short (no closing marker). Trailing spaces and a CR are tolerated.
    """
    allowed = FIELDS["worker-event"] | {"recorded", "counter"}
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


def _attempts(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per `attempt_id`, its latest line; a line after `completed` or `stopped` is ignored (the builder refuses it)."""
    attempts: dict[str, dict[str, Any]] = {}
    for line in lines:
        if "attempt_id" not in line:
            continue
        prior = attempts.get(line["attempt_id"])
        if prior is not None and prior["event"] in HELD_FREE_EVENTS:
            continue
        attempts[line["attempt_id"]] = line
    return attempts


def worker_events(project: Path, warnings: "list[str] | None" = None) -> list[dict[str, Any]]:
    """Project the unresolved workers; opaque host liveness remains unknown.

    A line without `attempt_id` keeps the earlier rule: the last event per task and handle, dropped once it is
    `completed`, `stopped` or `failed`. An attempt is held unless its latest event is `completed` or `stopped`:
    one item per attempt, with `reason` (`failed` or `no terminal event`) and `superseded`. Held attempts come
    first, so the caller's cap never hides one. A task whose notes hold a damaged line at policy 4 is listed too,
    with `reason` `damaged notes` and the first `detail`.
    """
    state = _load_state(project)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    held: list[dict[str, Any]] = []
    durable = task_durable_enabled(state)
    for task in state["tasks"]:
        text, _ = snapshot(document_path(project, "tasks/" + task["id"]))
        damage: list[str] = []
        lines = _worker_lines(text, durable, damage, task["id"], task_packets_enabled(state))
        if damage:
            if warnings is not None:
                warnings.extend(damage)
            # A damaged line may hide a live attempt, so the task is listed as unresolved, first, uncapped.
            held.append({"task": task["id"], "reason": "damaged notes", "detail": damage[0], "superseded": False})
        for event in lines:
            if "attempt_id" not in event:
                latest[(event["task"], event["handle"])] = event
        for event in _attempts(lines).values():
            if event["event"] in HELD_FREE_EVENTS:
                continue
            counter = task.get("assignment_counter")
            superseded = type(counter) is int and event["counter"] < counter
            reason = "failed" if event["event"] == "failed" else "no terminal event"
            held.append({**event, "reason": reason, "superseded": superseded})
    others = [item for item in latest.values() if item["event"] not in ("completed", "stopped", "failed")]
    return [*held, *others]


def _attempt_keys(
    task: dict[str, Any], request: dict[str, Any], notes: str, packets: bool = False,
) -> dict[str, Any]:
    """The `attempt_id` and `counter` of a policy-4 worker event, after every refusal; nothing is written here.

    `intent` opens an attempt: the tool issues the id from the task's `assignment_counter`. Every other event
    names an attempt that exists, is not terminal, and is in a state that accepts it; a superseded attempt
    (counter below the task's) accepts only `stopped`.
    """
    event = request["event"]
    damage: list[str] = []
    lines = _worker_lines(notes, True, damage, task["id"], packets)
    if damage:
        # The builder decides from these lines, so a skipped one could hide a live attempt: refuse, never guess.
        raise WorkspaceError(
            f"{damage[0]}; no command edits worker lines, so repair or remove that line in the notes by hand and "
            "retry (this refuses every worker event for the task, `stopped` included, because a skipped line could "
            "hide the attempt it names)"
        )
    attempts = _attempts(lines)
    if event == "intent":
        if "attempt_id" in request:
            raise WorkspaceError("attempt_id is issued by the tool at intent; do not supply one")
        if task["status"] != "RUNNING":
            raise WorkspaceError(f"task is not RUNNING: {task['id']} is {task['status']}")
        if any(line["event"] not in HELD_FREE_EVENTS for line in attempts.values()):
            raise WorkspaceError(
                f"held attempt blocks a new intent on {task['id']}: record stopped for it first"
            )
        counter = task["assignment_counter"]
        number = 1 + sum(1 for line in lines if line["event"] == "intent" and "attempt_id" in line)
        while f"{task['id']}-{counter}-{number}" in attempts:
            number += 1  # an id already in the notes is never issued again
        return {"attempt_id": f"{task['id']}-{counter}-{number}", "counter": counter}
    attempt_id = request.get("attempt_id")
    if attempt_id is None:
        raise WorkspaceError("attempt_id is required at policy 4 for every event except intent")
    latest = attempts.get(string_input(attempt_id, "attempt_id"))
    if latest is None:
        raise WorkspaceError(f"unknown attempt: {attempt_id}")
    if latest["event"] in HELD_FREE_EVENTS:
        raise WorkspaceError(f"attempt already terminal: {attempt_id} is {latest['event']}")
    if latest["counter"] < task["assignment_counter"] and event != "stopped":
        raise WorkspaceError(f"superseded attempt: {attempt_id} predates an assignment change; record stopped")
    if latest["event"] not in WORKER_EVENT_FROM[event]:
        raise WorkspaceError(f"event not valid from {latest['event']}: {event} on {attempt_id}")
    return {"attempt_id": attempt_id, "counter": latest["counter"]}


def _packet_hash(request: dict[str, Any], packets: bool) -> None:
    """Refuse `packet_sha256` below policy 5, off an `intent` event, or when it is not 64 lowercase hex."""
    if not packets:
        raise WorkspaceError("packet_sha256 needs policy_version 5; this project is at an earlier policy")
    value = request["packet_sha256"]
    if request["event"] != "intent":
        raise WorkspaceError("packet_sha256 belongs to an intent event")
    if type(value) is not str or PACKET_HASH.fullmatch(value) is None:
        raise WorkspaceError("packet_sha256 must be 64 lowercase hex characters")


def _confirmation(value: object) -> dict[str, str]:
    # `confirmation` is printed by `--schema` as required, so its refusal has to name it; without
    # `where` a caller saw "expected a JSON object, got NoneType" with no field to fill.
    confirmation = object_input(value, *NESTED_INPUTS["confirmation"], where="confirmation")
    return {key: string_input(confirmation[key], f"confirmation.{key}") for key in ("source", "response")}


def _worktree(
    state: dict[str, Any], request: dict[str, Any], workspace_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record or close a repository worktree from what Git reports, never from what was claimed.

    `record` stores a worktree the agent created after the user confirmed it (or the linked
    worktree the target already was, or the fact that the target is no repository) and repoints
    the target root at it. `close` stores the user's keep/remove/accept-dirty decision beside the
    dirty snapshot observed at that moment. Neither runs a Git write: creation and removal are the
    agent's, under the user's authorization, and this only refuses to record what Git contradicts.
    """
    worktrees = copy.deepcopy(state.get("worktrees", []))
    raw_path = request.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise WorkspaceError(PATH_HINT)
    path = string_input(raw_path, "path")
    if not Path(path).is_absolute():
        raise WorkspaceError("worktree path must be absolute")
    path = str(Path(path).resolve())
    working_directory = state["working_directory"]
    operation = request.get("operation")
    confirmation = _confirmation(request.get("confirmation"))
    if operation == "record":
        kind = request.get("kind", "created")
        role = request.get("role", "target")
        if kind not in ("created", "existing", "none") or role not in ("target", "additional"):
            raise WorkspaceError("worktree kind must be created, existing or none; role target or additional")
        if any(item["path"] == path for item in worktrees):
            raise WorkspaceError(
                f"worktree already recorded: {path}; a path stays recorded as history even after the worktree is "
                "removed, so a new worktree needs a new path"
            )
        seen = worktree_observation(Path(path))
        if not seen["available"]:
            raise WorkspaceError("Git could not be consulted for the worktree path; retry when it is available")
        if kind == "none":
            if role != "target" or path != working_directory or seen["is_repository"]:
                raise WorkspaceError(
                    "kind none records that the target itself is not a repository; "
                    f"observed is_repository={seen['is_repository']} for {path}, target {working_directory}"
                )
            entry = {"repository": None, "branch": None, "base_commit": None}
        else:
            branch = string_input(request.get("branch"), "branch")
            if not seen["exists"] or not seen["is_repository"] or seen["toplevel"] != path:
                raise WorkspaceError(f"path is not the root of a Git working tree: {path}")
            if not seen["is_worktree"]:
                raise WorkspaceError(f"path is a main checkout, not a linked worktree: {path}")
            if seen["branch"] != branch:
                raise WorkspaceError(f"branch mismatch: requested {branch!r}, observed {seen['branch']!r}")
            repository = (
                str(Path(string_input(request["repository"], "repository")).resolve())
                if "repository" in request
                else None
            )
            if repository is not None and repository != seen["repository"]:
                raise WorkspaceError(
                    f"repository mismatch: requested {repository}, observed {seen['repository']}; "
                    f"send repository {seen['repository']}"
                )
            repository = seen["repository"]
            if not seen["head"]:
                raise WorkspaceError("the worktree has no commit to record as its base")
            allowed = {path} if kind == "existing" else {path, repository}
            if role == "target" and working_directory not in allowed:
                raise WorkspaceError(
                    f"a target worktree must be recorded from its repository {repository} or from itself; "
                    f"working_directory is {working_directory}"
                )
            if role == "additional" and path == working_directory:
                raise WorkspaceError("an additional worktree cannot be the target root")
            entry = {"repository": repository, "branch": branch, "base_commit": seen["head"]}
        entry.update(
            path=path, kind=kind, role=role, status="active", recorded_at=now_iso(),
            confirmation=confirmation, closure=None,
        )
        worktrees.append(entry)
        patch: dict[str, Any] = {"worktrees": worktrees}
        if role == "target" and kind != "none":
            patch["working_directory"] = path
        return patch, {"worktree": entry, "observed": seen}
    def gate(event: str, item: dict[str, Any]) -> None:
        errors = delivery_gate_errors(workspace_root / state["project"], state, item, event, request)
        if errors:
            raise WorkspaceError(" | ".join(errors))

    if operation in DELIVERY_OPERATIONS:
        entry, notes = delivery_operation(
            worktrees, path, operation, request, confirmation, workspace_root=workspace_root, project=state["project"],
            gate=gate, rules=delivery_rules(state),
        )
        return {"worktrees": worktrees}, {"worktree": entry, **({"notes": notes} if notes else {})}
    if operation != "close":
        raise WorkspaceError("worktree operation must be record, close, pull_request, alpha, release or merge")
    decision = request.get("decision")
    if decision not in ("keep", "accept_dirty", "remove"):
        raise WorkspaceError("worktree decision must be keep, accept_dirty or remove")
    entry = next((item for item in worktrees if item["path"] == path), None)
    # A kept worktree stays decidable: the user may change their mind, work may resume after
    # maintenance reopens the project, and removal can happen outside the tool. Replacing a closure
    # would erase the earlier consent, so the caller saves the superseded decision to the decision
    # history (`superseded` below) and only then is the new one recorded. A removed worktree is final.
    if entry is None or entry["status"] not in ("active", "kept") or entry["kind"] == "none":
        raise WorkspaceError(f"no active or kept repository worktree is recorded at {path}")
    if entry["status"] == "active" or decision == "remove":
        gate("closure", entry)
    if decision == "remove":
        missing = removal_findings(entry, rules=delivery_rules(state))
        if missing:
            raise WorkspaceError(
                "the worktree cannot be removed before its delivery records are complete:\n- "
                + "\n- ".join(missing)
            )
    superseded = copy.deepcopy(entry["closure"]) if entry["status"] == "kept" else None
    seen = worktree_observation(Path(path))
    if not seen["available"]:
        raise WorkspaceError("Git could not be consulted for the worktree; retry when it is available")
    if decision == "remove":
        registered = worktree_registered(Path(entry["repository"]), Path(path))
        if seen["exists"] or registered is not False:
            raise WorkspaceError(
                "the worktree is still present or registered; the user-authorized "
                "`git worktree remove` must run before recording removal"
            )
        dirty: list[str] = []
    else:
        if not seen["exists"] or not seen["is_repository"]:
            raise WorkspaceError(f"the worktree is missing; restore it or record its removal: {path}")
        dirty = list(seen["dirty"])
        if decision == "keep" and dirty:
            raise WorkspaceError(
                "the worktree has uncommitted changes; commit them (only when the user asks) or record "
                "accept_dirty with the user's explicit acceptance:\n" + "\n".join(dirty[:50])
            )
    entry.update(
        status="removed" if decision == "remove" else "kept",
        closure={"decision": decision, "dirty": dirty, "observed_at": now_iso(), **confirmation},
    )
    patch = {"worktrees": worktrees}
    if entry["role"] == "target" and decision == "remove":
        patch["working_directory"] = entry["repository"]
    outcome: dict[str, Any] = {"worktree": entry, "observed": seen}
    if superseded is not None:
        outcome["superseded"] = superseded
    note = abandoned_note(entry) if decision == "remove" else None
    if note is not None:
        outcome["notes"] = [note]
    return patch, outcome


def _effect(project: Path, state: dict[str, Any], request: dict[str, Any]) -> tuple[str, str]:
    """The notes document and `research-effect` entry body of an intent or resolve, after its refusals."""
    task = _task(state, string_input(request.get("task"), "task"))
    operation = request.get("operation")
    if operation not in ("intent", "resolve"):
        raise WorkspaceError("effect operation must be intent or resolve")
    key = request.get("key")
    if not valid_key(key):
        raise WorkspaceError(f"effect key must be 1-{KEY_LIMIT} characters on one line")
    opened = open_intents(project, task)
    recorded = datetime.now().astimezone().isoformat(timespec="microseconds")
    if operation == "intent":
        kind = task["effect"]["kind"]
        if kind not in EFFECT_KINDS:
            raise WorkspaceError(
                f"effect intents are for external or destructive tasks; {task['id']} has effect kind {kind}"
            )
        entry = {"op": "intent", "key": key, "effect": string_input(request.get("effect"), "effect")}
        receipts = task.get("receipts")
        if isinstance(receipts, list) and any(isinstance(item, dict) and item.get("value") == key for item in receipts):
            # A receipt with the key's value closes the intent at once, so recording one would show nothing.
            raise WorkspaceError(
                f"{task['id']} already has a receipt with value {key!r}, so that effect is recorded as done; "
                "use another key for a new effect"
            )
        if key in opened:
            raise WorkspaceError(
                f"an open intent with key {key!r} already exists on {task['id']}; check whether the effect happened, "
                "then resolve it or record its receipt"
            )
    else:
        if request.get("outcome") not in EFFECT_OUTCOMES:
            raise WorkspaceError(f"effect outcome must be one of: {', '.join(EFFECT_OUTCOMES)}")
        if key not in opened:
            raise WorkspaceError(f"no open intent with key {key!r} on {task['id']}")
        entry = {"op": "resolve", "key": key, "outcome": request["outcome"]}
        if "note" in request:
            entry["note"] = string_input(request["note"], "note")
    entry["recorded"] = recorded
    # `>` only occurs inside JSON strings, so escaping it keeps any `-->` in the text from closing the comment.
    return "tasks/" + task["id"], "<!-- research-effect " + json.dumps(entry).replace(">", "\\u003e") + " -->"


def _wait(state: dict[str, Any], request: dict[str, Any]) -> tuple[str, str]:
    """The notes document and `research-wait` entry body of a wait, after its refusals."""
    task = _task(state, string_input(request.get("task"), "task"))
    if task["status"] != "BLOCKED":
        raise WorkspaceError(
            f"a wait is recorded on a BLOCKED task; {task['id']} is {task['status']}, so run task block first"
        )
    entry: dict[str, Any] = {"on": string_input(request.get("on"), "on"), "until": None, "reason": None}
    until = request.get("until")
    if until is not None:
        if aware_instant(until) is None:
            raise WorkspaceError("wait until must be a timezone-aware ISO 8601 time, such as 2026-11-07T09:00:00+01:00")
        entry["until"] = until.strip()
    if request.get("reason") is not None:
        entry["reason"] = string_input(request["reason"], "reason")
    if task_durable_enabled(state):
        generation = task.get("block_generation")
        if type(generation) is not int:
            raise WorkspaceError(f"{task['id']} has no integer block_generation; repair the task record first")
        entry["generation"] = generation
    entry["recorded"] = datetime.now().astimezone().isoformat(timespec="microseconds")
    return "tasks/" + task["id"], "<!-- research-wait " + json.dumps(entry).replace(">", "\\u003e") + " -->"


def _alignment_target(state: dict[str, Any], kind: str, request: dict[str, Any]) -> "str | None":
    """The target an alignment confirmation records, after its refusals; None when it records none."""
    rules = delivery_rules(state)
    supplied = request.get("delivery_target")
    if kind != "alignment":
        if "delivery_target" in request:
            raise WorkspaceError(
                "only kind alignment takes delivery_target; set it with 'workflow retarget' after this confirmation"
            )
        return None
    if not has_target_rules(rules.policy):
        if "delivery_target" in request:
            raise WorkspaceError(
                f"delivery_target needs policy_version 2 or later, but this project is at policy {rules.policy}"
            )
        return None
    if "delivery_target" not in request:
        if rules.target is None:
            raise WorkspaceError(
                f"an alignment confirmation records the delivery_target: {', '.join(DELIVERY_TARGETS)}"
            )
        return None
    if supplied not in DELIVERY_TARGETS:
        raise WorkspaceError(f"delivery_target must be one of: {', '.join(DELIVERY_TARGETS)}")
    if "delivery_target" in state:
        raise WorkspaceError("this project already has a delivery_target; change it with 'workflow retarget'")
    return str(supplied)


def _policy_body(head: str, response: str, source: str, diff: dict[str, list[str]], *extra: str) -> str:
    """The decision-history text of a target change: what changed, the user's words, obligations gained and dropped."""
    return "\n".join(
        [
            head,
            f"response: {response}\nsource: {source}",
            "gained: " + ("; ".join(diff["gained"]) or "none"),
            "dropped: " + ("; ".join(diff["dropped"]) or "none"),
            *extra,
        ]
    )


def _retarget(state: dict[str, Any], request: dict[str, Any]) -> "tuple[dict[str, Any], str, dict[str, Any]]":
    """The patch, decision body and result of a target change, after its refusals."""
    old = delivery_rules(state)
    if not has_target_rules(old.policy):
        raise WorkspaceError(f"retarget needs policy_version 2 or later, but this project is at policy {old.policy}")
    if state["status"] in ("DONE", "CANCELLED"):
        raise WorkspaceError(f"a {state['status']} project keeps its delivery_target")
    target = request.get("delivery_target")
    if target not in DELIVERY_TARGETS:
        raise WorkspaceError(f"delivery_target must be one of: {', '.join(DELIVERY_TARGETS)}")
    if target == state.get("delivery_target"):
        raise WorkspaceError(f"the delivery_target is already {target}")
    reason = string_input(request.get("reason"), "reason")
    confirmation = _confirmation(request.get("confirmation"))
    new = delivery_rules({**state, "delivery_target": target})
    blockers = target_conflicts(state, target, new)
    if target == "research":
        blockers += research_task_conflicts(state)
    if blockers:
        raise WorkspaceError("retarget would drop records: " + "; ".join(blockers))
    diff = rules_diff(old, new)
    body = _policy_body(
        f"kind: retarget\nfrom: {state.get('delivery_target') or 'unset'}\nto: {target}\nreason: {reason}",
        confirmation["response"],
        confirmation["source"],
        diff,
    )
    outcome = {"delivery_target": {"from": state.get("delivery_target"), "to": target}, **diff}
    return {"delivery_target": target}, body, outcome


def _settings(state: dict[str, Any], request: dict[str, Any]) -> "tuple[dict[str, Any], str, dict[str, Any]]":
    """The patch, decision body and result of a settings change, after its refusals."""
    policy = delivery_rules(state).policy
    if not has_review_rules(policy):
        raise WorkspaceError(f"settings needs policy_version 3 or later, but this project is at policy {policy}")
    if state["status"] in ("DONE", "CANCELLED"):
        raise WorkspaceError(f"a {state['status']} project keeps its settings")
    confirmation = _confirmation(request.get("confirmation"))
    patch: dict[str, Any] = {}
    if "review_risk" in request:
        risk = request["review_risk"]
        problems = [] if risk is None else risk_errors(risk)
        if problems:
            raise WorkspaceError(problems[0])
        patch["review_risk"] = risk
    if "review_fallback" in request:
        fallback = request["review_fallback"]
        problems = [] if fallback is None else fallback_errors(fallback)
        if problems:
            raise WorkspaceError(problems[0])
        patch["review_fallback"] = fallback
    if "options" in request:
        options = request["options"]
        problems = [] if options is None else options_errors(options, state)
        if problems:
            raise WorkspaceError(problems[0])
        patch["options"] = options
    if not patch:
        raise WorkspaceError("settings needs at least one of review_risk, review_fallback and options")
    changes = "; ".join(f"{key} = {json.dumps(value, sort_keys=True)}" for key, value in patch.items())
    body = f"kind: settings\n{changes}\nsource: {confirmation['source']}\nresponse: {confirmation['response']}"
    return patch, body, {"settings": patch}


def _build(
    project: Path, action: str, request: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    documents: dict[str, str] = {}
    patch: dict[str, Any] | None = None
    result: dict[str, Any] = {}

    def read(name: str) -> str:
        return documents.get(name, snapshot(document_path(project, name))[0])

    def decision(body: str, entry_id: str | None = None) -> None:
        documents["spec"] = _entry(read("spec"), entry_id or request["id"], body, decision=True)

    if action == "round":
        if "sections" in request:
            if not isinstance(request["sections"], dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in request["sections"].items()
            ):
                raise WorkspaceError("sections must map headings to text")
            documents["spec"] = _replace_sections(read("spec"), request["sections"])
        if "planning" in request:
            documents["planning"] = string_input(request["planning"], "planning").strip() + "\n"
        if "architecture" in request:
            if is_legacy_planning_layout(project):
                raise WorkspaceError(LEGACY_PLANNING_HINT)
            documents["architecture"] = string_input(request["architecture"], "architecture").strip() + "\n"
        for item in list_input(request.get("decisions", []), field="decisions"):
            object_input(item, {"id", "body"}, {"id", "body"})
            documents["spec"] = _entry(read("spec"), item["id"], item["body"], decision=True)
        stale = stale_confirmations(read("spec"), read("planning"))
        if stale:
            result["stale_confirmations"] = stale
        notes = goldfish_findings(project)
        if notes:
            result["goldfish_findings"] = notes
    elif action == "confirm":
        kind = string_input(request.get("kind"), "kind")
        # `architecture` is the name this kind had before the planning document was split from the
        # design; it still means the planning document, and so does `architecture_sha256`.
        if kind == "architecture":
            kind = "planning"
        if kind not in ("requirements", "planning", "alignment"):
            raise WorkspaceError("confirmation kind must be requirements, planning or alignment")
        # `alignment` is one reply covering both documents, for a proposal small enough to be read and
        # agreed in one message. It records exactly what two separate confirmations would: each kind's
        # own marker, content digest and effect, against that document's current token.
        if kind == "alignment":
            if "proposal_sha256" in request:
                raise WorkspaceError("alignment takes requirements_sha256 and planning_sha256, not proposal_sha256")
            if "planning_sha256" in request and "architecture_sha256" in request:
                raise WorkspaceError("give planning_sha256 or its old name architecture_sha256, not both")
            planning_field = "architecture_sha256" if "architecture_sha256" in request else "planning_sha256"
            tokens = {
                "requirements": string_input(request.get("requirements_sha256"), "requirements_sha256"),
                "planning": string_input(request.get(planning_field), planning_field),
            }
            source = {"requirements": "requirements_sha256", "planning": planning_field}
        else:
            if "requirements_sha256" in request or "planning_sha256" in request or "architecture_sha256" in request:
                raise WorkspaceError("only kind alignment takes requirements_sha256 and planning_sha256")
            tokens = {kind: string_input(request.get("proposal_sha256"), "proposal_sha256")}
            # A single-kind confirmation reads its token from `proposal_sha256`, not from the
            # per-document key, and a refusal must name the field the retry can actually fill.
            source = {kind: "proposal_sha256"}
        for confirmed in tokens:
            name = "spec" if confirmed == "requirements" else "planning"
            current = snapshot(document_path(project, name))[1]
            if current != tokens[confirmed]:
                raise WorkspaceConflict(
                    document_conflict_message(
                        name, tokens[confirmed], current, project, use=source[confirmed],
                        prefix="proposal token changed",
                    )
                )
        for field in ("review_id", "response", "source", "scope"):
            string_input(request.get(field), field)
        confirmed_target = _alignment_target(state, kind, request)
        gate_errors = confirm_gate_errors(project, state, tuple(tokens), read("spec"), read("planning"))
        if gate_errors:
            raise WorkspaceError(" | ".join(gate_errors))
        review_id = str(request["review_id"])
        for confirmed, token in tokens.items():
            name = "spec" if confirmed == "requirements" else "planning"
            body = (
                confirmation_marker(confirmed, review_id, token)
                + "\n"
                + proposal_marker(confirmed, review_id, proposal_digest(confirmed, read(name)))
                + "\n"
                + "\n".join(
                    f"{key}: {value}"
                    for key, value in (
                        ("kind", confirmed), ("review_id", review_id), ("proposal_sha256", token),
                        ("response", request["response"]), ("source", request["source"]),
                        ("scope", request["scope"]),
                        *(
                            (("delivery_target", confirmed_target),)
                            if confirmed_target and confirmed == "planning"
                            else ()
                        ),
                    )
                )
            )
            decision(body, f"{request['id'][:50]}-{confirmed}" if kind == "alignment" else None)
            if confirmed == "planning":
                planning = read("planning")
                match = PLANNING_STATUS_PATTERN.search(planning)
                if match is None or review_id not in planning:
                    raise WorkspaceError("the planning document must declare its status and reviewed identifier")
                documents["planning"] = planning[: match.start("status")] + "agreed" + planning[match.end("status") :]
                documents["planning"] += f"\nConfirmation ({now_iso()}):\n\n{body}\n"
                heading = "Constraints and important assumptions"
                _, _, begin, finish = _section_span(read("spec"), heading)
                documents["spec"] = _replace_sections(
                    read("spec"),
                    {
                        heading: read("spec")[begin:finish].rstrip()
                        + f"\n\nAgreed planning: [revision {review_id}]({planning_path(project).name})."
                    },
                )
        if confirmed_target:
            patch = {"delivery_target": confirmed_target}
            result["delivery_target"] = confirmed_target
        notes = goldfish_findings(project)
        if notes:
            result["goldfish_findings"] = notes
        amendments = amendment_details(project)
        if amendments:
            result["amendments"] = amendments
    elif action == "retarget":
        patch, body, outcome = _retarget(state, request)
        decision(body)
        result.update(outcome)
    elif action == "settings":
        patch, body, outcome = _settings(state, request)
        decision(body)
        result.update(outcome["settings"])
    elif action == "correct":
        old = _task(state, string_input(request.get("task"), "task"))
        if old["status"] not in ("DONE", "SKIPPED"):
            raise WorkspaceError("correction source must be terminal")
        replacement = object_input(
            request.get("replacement"),
            {"name", "success_criteria", "verification", "effect", "outputs", "depends_on", "reads"},
        )
        number = 1
        occupied = {task["id"] for task in state["tasks"]}
        while f"T{number:02d}" in occupied:
            number += 1
        task_id = f"T{number:02d}"
        patch = {"tasks": [{"id": task_id, **replacement}]}
        documents["tasks/" + task_id] = _entry(
            f"# Task {task_id} notes\n", request["id"], f"Corrects {old['id']}: {string_input(request.get('reason'))}"
        )
        result["task"] = task_id
        result["assignment"] = prepare_update(state, patch)["tasks"][-1]
    elif action == "reconcile":
        patch = object_input(
            request.get("patch"), {"title", "status", "review", "cancellation_reason", "predecessor", "tasks"}
        )
        decision(string_input(request.get("decision"), "decision"))
    elif action == "worker-event":
        task = _task(state, string_input(request.get("task"), "task"))
        for field in ("handle", "scope", "observation"):
            string_input(request.get(field), field)
        if request.get("event") not in WORKER_EVENTS:
            raise WorkspaceError("unsupported worker observation")
        if (
            type(request.get("assignment_revision")) is not int
            or not 0 <= request["assignment_revision"] <= state["revision"]
        ):
            raise WorkspaceError("invalid assignment revision")
        name = "tasks/" + request["task"]
        event = {key: request[key] for key in WORKER_EVENT_REQUIRED}
        if task_durable_enabled(state):
            event.update(_attempt_keys(task, request, read(name), task_packets_enabled(state)))
            if "packet_sha256" in request:
                _packet_hash(request, task_packets_enabled(state))
                event["packet_sha256"] = request["packet_sha256"]
            if request["event"] == "intent":
                result["attempt_id"] = event["attempt_id"]
        elif "attempt_id" in request:
            raise WorkspaceError("attempt_id needs policy_version 4; this project is at an earlier policy")
        elif "packet_sha256" in request:
            raise WorkspaceError("packet_sha256 needs policy_version 5; this project is at an earlier policy")
        event["recorded"] = now_iso()
        documents[name] = _entry(
            read(name), request["id"], "<!-- research-worker " + json.dumps(event, sort_keys=True) + " -->"
        )
    elif action == "effect":
        name, body = _effect(project, state, request)
        documents[name] = _entry(read(name) or f"# Task {request['task']} notes\n", request["id"], body)
    elif action == "wait":
        name, body = _wait(state, request)
        documents[name] = _entry(read(name) or f"# Task {request['task']} notes\n", request["id"], body)
    elif action in ("authorize", "receipt"):
        task = _task(state, string_input(request.get("task"), "task"))
        if action == "authorize":
            value = object_input(request.get("authorization"), *NESTED_INPUTS["authorization"], where="authorization")
            change = {"authorization": value}
        else:
            value = object_input(request.get("receipt"), *NESTED_INPUTS["receipt"], where="receipt")
            change = {"receipts": task["receipts"] + ([] if value in task["receipts"] else [value])}
        patch = {"tasks": [{"id": task["id"], **change}]}
    elif action == "review":
        for field in ("reviewer", "version", "scope", "findings", "status"):
            string_input(request.get(field), field)
        cycle = state["review"]["cycle"] + 1
        name = f"reviews/review_{cycle:02d}"
        documents[name] = (
            f"# Delivery review {cycle}\n\n"
            + "\n\n".join(
                f"## {key.title()}\n\n{request[key]}" for key in ("reviewer", "version", "scope", "findings", "status")
            )
            + "\n"
        )
        evidence = list_input(request.get("evidence", []))
        patch = {
            "review": {
                "cycle": cycle,
                "required": request.get("required", state["review"]["required"]),
                "status": request["status"],
                "evidence": [{"root": "workspace", "path": name + ".md", "anchor": None}, *evidence],
            }
        }
        result["review"] = patch["review"]
    elif action == "goldfish":
        written, result = goldfish_operation(project, state, request)
        documents.update(written)
    elif action == "finalize":
        errors, warnings = worktree_closure_findings(state)
        if errors:
            raise WorkspaceError("closure blocked by recorded worktrees:\n- " + "\n- ".join(errors))
        result["worktree_warnings"] = warnings
        reflection = string_input(request.get("reflection"), "reflection").strip()
        # A reflection that is one token or one line is not a post-mortem; one was saved that was a
        # 64-character hash. The check is on shape only: whether it is any good is the reader's call.
        if len(reflection) < REFLECTION_MIN_CHARS or not any(level for level, _, _ in _headings(reflection)):
            raise WorkspaceError(
                f"the reflection must be a post-mortem of at least {REFLECTION_MIN_CHARS} characters with a "
                "Markdown heading (for example '# Reflection'); nothing was committed"
            )
        # The retro section is what the next retro reads first. Refused after the cutoff; before it the
        # finding comes back as a warning so an old project still closes.
        retro_findings = retro_section_findings(reflection)
        if retro_findings and is_retro_gated(state):
            raise WorkspaceError("; ".join(retro_findings) + "; nothing was committed")
        result["retro_warnings"] = retro_findings
        documents["reflection"] = reflection + "\n"
        # What this project leaves for a follow-up: the reflection's `## Follow-up` bullets, then the findings
        # a Goldfish round accepted as residual. The skill suggests a follow-up only when the list is not empty.
        bullets, loose = follow_up_items(reflection)
        result["follow_up"] = [{"kind": "reflection", "text": text, "pointer": "reflection.md"} for text in bullets]
        result["follow_up"] += residual_findings(project)
        result["follow_up_warnings"] = (
            [
                "the reflection's Follow-up section could not be read in full (text with no bullet, or a fenced "
                "block that is never closed); write one bullet per item and close every fence"
            ]
            if loose else []
        ) + [
            f"the reflection title {title!r} is not read as the follow-up section, so its bullets are not "
            "carried over; name it 'Follow-up' or 'Follow-ups'"
            for title in near_follow_up_titles(reflection)
        ]
        # Stage what the reflection taught while this operation still holds the project lock, so a
        # concurrent `stage` cannot be lost by the merge and the close warning can describe content
        # it has not committed yet. Nothing staged means nothing to write.
        planned = read("memory-staging")
        staged: list[str] = []
        skipped: list[dict[str, str]] = []
        # Titles staged before this close: a section that only lists them is a pointer, not a lesson.
        staged_titles = [name for level, name, _ in _headings(planned) if level == 2]
        for title, body in reflection_lesson_sections(reflection) if staging_enabled(state) else []:
            if names_only_staged(body, staged_titles):
                skipped.append({"title": title, "reason": "names only items already staged"})
                continue
            if any(name == title for _, name, _ in _headings(planned)):
                level, _, body_start, end = _section_span(planned, title)
                same = level == 2 and planned[body_start:end].strip() == body
                skipped.append({"title": title, "reason": "already staged" if same else "title conflict"})
                continue
            try:
                planned = append_staged_section(planned, title, body)
            except WorkspaceError as error:
                skipped.append({"title": title, "reason": str(error)})
                continue
            staged.append(title)
        if staged:
            documents["memory-staging"] = planned
        result["reflected_lessons"] = {"staged": staged, "skipped": skipped}
        result["memory_staging_warnings"] = memory_staging_warnings(project, content=planned, state=state)
        # A verify batch without an observed-firing control never proved its checks can fail, so
        # finalize surfaces that gap as a warning - same non-blocking shape as the staging warning.
        from workspace_checks import control_warnings

        result["control_warnings"] = control_warnings(project, state)
        # c3: a task whose only evidence is a deliberately failing control proved the guard fires but
        # not that the real check passes. Warning, not refusal: the task may legitimately finish on it.
        from workspace_evidence import failing_evidence_warnings

        result["failing_evidence_warnings"] = failing_evidence_warnings(project, state)
        patch = {"status": "DONE"}
        result["_after_commit"] = ["handoff"]
    elif action in ("maintenance", "cancel"):
        reason = string_input(request.get("reason"), "reason")
        patch = {
            "status": "PLANNING" if action == "maintenance" else "CANCELLED",
            "tasks": list_input(request.get("tasks", [])),
        }
        if action == "maintenance" and state["status"] != "DONE":
            raise WorkspaceError("maintenance requires a completed project")
        if action == "cancel":
            patch["cancellation_reason"] = reason
        decision(f"{action}: {reason}")
        result["legal_next"] = sorted(PROJECT_TRANSITIONS[patch["status"]])
    elif action == "assess":
        from workspace_lib import read_memory_topic

        topic = read_memory_topic(project.parent, string_input(request.get("topic"), "topic"))
        if request.get("disposition") not in ("apply", "reject", "defer"):
            raise WorkspaceError("lesson disposition must be apply, reject or defer")
        body = f"Lesson {request['topic']} ({topic['sha256']}): {request['disposition']}\n"
        body += (
            string_input(request.get("reason"), "reason")
            + "\n"
            + string_input(request.get("application"), "application")
        )
        decision(body)
        result["topic"] = {"name": request["topic"], "sha256": topic["sha256"]}
    elif action == "worktree":
        patch, result = _worktree(state, request, project.parent)
        if "superseded" in result:
            before, after = result["superseded"], result["worktree"]["closure"]
            decision(
                f"Worktree {result['worktree']['path']}: closure decision superseded. Previous: "
                f"{before['decision']} observed {before['observed_at']}, source {before['source']!r}, response "
                f"{before['response']!r}, accepted dirty paths {before['dirty']!r}. New: {after['decision']}, "
                f"source {after['source']!r}, response {after['response']!r}, dirty paths {after['dirty']!r}. "
                "The earlier decision stays in this history; only the latest one governs closure."
            )
    elif action == "report":
        from workspace_reports import render_report

        if has_review_rules(delivery_rules(state).policy) and not project_options(state)["reports"]:
            result["notice"] = (
                "reports are off for this project (options.reports); the report was not written. "
                "Turn them on with 'workflow settings' and the user's words"
            )
        else:
            documents["artifacts/report"], result = render_report(project, state, request)
    if "continuation" in request or action in ("checkpoint", "finalize", "maintenance", "cancel", "reconcile"):
        target = prepare_update(state, patch, internal=True) if patch else copy.deepcopy(state)
        target["revision"] += int(patch is not None)
        continuation = request.get("continuation", {})
        inferred = infer_stop_reason(project, target, continuation)
        if inferred is not None:
            continuation = {**continuation, "stop_reason": inferred}
            result["stop_reason_inferred"] = inferred
        documents["handoff"], metadata = render_checkpoint(
            project, target, continuation, documents, next_hint=next_hint(project, target)
        )
        result["resume_prompt"] = (
            f"Use the project skill to resume {project}. Run workflow resume with selected sources; "
            f"reconcile ownership first. Checkpoint revision {target['revision']}; "
            f"handoff SHA-256 {document_sha256(documents['handoff'])}. Next: {metadata['continuation']['next']}"
        )
    return documents, patch, result


def migrate_policy(
    project: Path,
    *,
    target: object,
    apply: bool = False,
    expected_revision: "int | None" = None,
    confirm: "str | None" = None,
) -> dict[str, Any]:
    """Preview, or with `apply` commit, the move of a project to policy 2 with a delivery target.

    The preview writes nothing. Apply is one journaled operation named `migrate_policy`, which is what lets
    it write the two guarded fields, and it records the user's words in the decision history. A retry of the
    same apply finishes or replays its journal, so the plan is checked only when no journal exists yet.
    """
    try:
        return _migrate_policy(project.resolve(), target, apply, expected_revision, confirm)
    except OSError as error:
        raise WorkspaceError(f"migrate-policy filesystem failure: {error}") from error


def _migrate_policy(
    project: Path, target: object, apply: bool, expected_revision: "int | None", confirm: "str | None"
) -> dict[str, Any]:
    state = _load_state(project)
    if not apply:
        if expected_revision is not None or confirm is not None:
            raise WorkspaceError("--expected-revision and --confirm are read only with --apply; add --apply to write")
        return {**migration_plan(state, target), "applied": False, "revision": state["revision"]}
    words = confirm.strip() if isinstance(confirm, str) else ""
    if not words:
        raise WorkspaceError("apply needs --confirm with the user's words approving the migration")
    if expected_revision is None:
        raise WorkspaceError("apply needs --expected-revision, the revision the preview was read at")
    if not isinstance(target, str) or target not in DELIVERY_TARGETS:
        raise WorkspaceError(f"target must be one of: {', '.join(DELIVERY_TARGETS)}")
    operation_id = f"migrate-policy-r{expected_revision}-{target}"
    stored = journal_path(project, operation_id)
    # A retry has the journal's own spec token: the first run changed spec.md, and the journal's signature
    # covers the request, so a fresh token would read as different input.
    token = snapshot(document_path(project, "spec"))[1]
    if stored.exists():
        token = load_json(stored).get("documents", {}).get("spec", {}).get("before", token)
    plan = None if stored.exists() else migration_plan(state, target)
    request: dict[str, Any] = {
        "id": operation_id,
        "expected_revision": expected_revision,
        "tokens": {"spec": token},
        "delivery_target": target,
        "confirmation": {"source": "migrate-policy --confirm", "response": words},
    }

    def build(current: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
        fresh = plan if plan is not None else migration_plan(current, target)
        body = _policy_body(
            f"kind: migrate-policy\nfrom: policy {fresh['from_policy']}\nto: policy 2\n"
            f"delivery_target: {fresh['delivery_target']}",
            words,
            "migrate-policy --confirm",
            fresh,
            "unsatisfied: " + ("; ".join(fresh["unsatisfied"]) or "none"),
        )
        spec = snapshot(document_path(project, "spec"))[0]
        documents = {"spec": _entry(spec, operation_id, body, decision=True)}
        return documents, {"policy_version": 2, "delivery_target": fresh["delivery_target"]}, {**fresh, "applied": True}

    return run_operation(project, "migrate_policy", request, build)


def workflow(project: Path, action: str, request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a closed vocabulary of operations; never evaluate workspace instructions."""
    try:
        return _workflow(project, action, request)
    except OSError as error:
        raise WorkspaceError(f"workflow filesystem failure: {error}") from error


def _variant_required(project: Path, action: str, request: Any) -> frozenset[str]:
    """The keys the request's own discriminator demands, so one refusal can name them with the rest.

    Only the unconditional part of a variant is enforced here: the operation code keeps the checks that
    read project state (an existing alpha line, a recorded entry) and raises their own message.
    """
    if isinstance(request, dict) and action == "confirm":
        wanted = CONFIRM_REQUIRED | CONFIRM_REQUIRED_BY.get(f"kind={request.get('kind')}", frozenset())
        # An alignment may carry the planning digest under its old name; the runtime accepts either.
        if "architecture_sha256" in request and "planning_sha256" in wanted:
            wanted = (wanted - {"planning_sha256"}) | {"architecture_sha256"}
        return wanted
    if action == "worker-event":
        return WORKER_EVENT_REQUIRED
    if action == "wait":
        return WAIT_REQUIRED
    if isinstance(request, dict) and action == "effect":
        return EFFECT_REQUIRED | EFFECT_REQUIRED_BY.get(f"operation={request.get('operation')}", frozenset())
    if not isinstance(request, dict) or action != "worktree":
        return frozenset()
    entry = None
    path = request.get("path")
    if isinstance(path, str) and Path(path).is_absolute():
        try:
            worktrees = _load_state(project).get("worktrees", [])
        except WorkspaceError:
            worktrees = []
        resolved = str(Path(path).resolve())
        entry = next((item for item in worktrees if item.get("path") == resolved), None)
    return required_for(request.get("operation"), request, entry)


def _workflow(project: Path, action: str, request: dict[str, Any]) -> dict[str, Any]:
    project = project.resolve()
    if action not in FIELDS and action not in READ_INPUTS:
        raise WorkspaceError(f"unknown workflow action: {action}")
    if action in FIELDS:
        required = WRITE_REQUIRED | _variant_required(project, action, request)
        object_input(
            request,
            COMMON | FIELDS[action],
            required,
            where=f"workflow {action}",
            nested=ACTION_NESTED.get(action),
            nested_required=NESTED_REQUIRED.get(action, frozenset()),
            hints=KEY_HINTS if action == "worktree" else CONFIRM_HINTS if action == "confirm"
            else RETARGET_HINTS if action == "retarget" else None,
        )
        if action in TASK_NOTED_ACTIONS and isinstance(request.get("task"), str):
            note(task=request["task"])
        return run_operation(project, action, request, lambda state: _build(project, action, request, state))
    if action == "verify":
        from workspace_checks import verify

        return verify(project, request)
    if action == "triage":
        from workspace_lessons import triage

        return triage(project, request)
    if action in ("resume", "packet"):
        schema_input(request, action)
        if action == "resume" and any(key in request for key in WORKER_KEYS):
            note(worker_packet=True)
        result = resume_bundle(project, request, action=action)
        worker_warnings: list[str] = []
        events = worker_events(project, worker_warnings)
        for event in events:
            silent = silent_minutes(event.get("recorded"))
            if silent is not None:
                event["silent_minutes"] = silent
        # Held attempts are always listed; the cap applies to the other items only.
        held = [event for event in events if "reason" in event]
        others = [event for event in events if "reason" not in event]
        result["unresolved_workers"] = [*held, *others[:20]]
        result["omitted_workers"] = max(0, len(others) - 20)
        if worker_warnings:
            result["durability_warnings"] = [*result.get("durability_warnings", []), *worker_warnings]
        return result
    if action == "read":
        return selected_read(project, request)
    if action == "impact":
        return impact(project, request)
    if action == "preview":
        schema_input(request, "preview")
        return preview(project, request["patch"], request["expected_revision"])
    if action == "fingerprint":
        schema_input(request, "fingerprint")
        return {"fingerprints": fingerprint(project, request["references"])}
    if action == "recover":
        schema_input(request, "recover")
        if "apply" in request and type(request["apply"]) is not bool:
            raise WorkspaceError("recover apply must be boolean")
        return recover_operation(project, request["id"], apply=request.get("apply", False))
    schema_input(request, action)
    if action == "freshness":
        return checkpoint_freshness(project)
    if action == "readiness":
        return readiness(project, request)
    # Every other action returned above, and the unknown ones were refused at the top.
    return memory_health(project.parent, _load_state(project)["title"])
