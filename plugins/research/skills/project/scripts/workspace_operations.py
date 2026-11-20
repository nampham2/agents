"""High-level project operations built on the canonical validation and commit path."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from workspace_documents import append_record, edit_document, follow_up_items
from workspace_durability import task_finish_findings
from workspace_evidence import evidence_entries, evidence_findings, selected_evidence_references
from workspace_goldfish import residual_findings
from workspace_journal import OperationError, snapshot
from workspace_lib import (
    BRIEFING_CANONICAL_SECTIONS,
    DELIVERY_TARGETS,
    TERMINAL_TASK_STATUSES,
    DirectoryLock,
    IndexNotRebuilt,
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    authorization_findings,
    commit_state,
    coordinator_enabled,
    delivery_rules,
    has_target_rules,
    load_json,
    normalize_tags,
    now_iso,
    read_text,
    reason_reference_warnings,
    receipt_findings,
    replan_valid,
    staged_lesson_lines,
    stamp_transitions,
    tags_of,
    task_durable_enabled,
    task_packets_enabled,
    task_stamps_enabled,
    validate_project,
    vcs_warnings,
)
from workspace_session import _load_state, document_tokens, next_move
from workspace_similar import _rows, check_new_project, repository_of


class CloseOperationError(WorkspaceError):
    """A close failure carrying truthful partial-write state for machine callers."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def rules_diff(old: Any, new: Any) -> dict[str, list[str]]:
    """Obligations gained and dropped between two `DeliveryRules`, in plain words."""
    flags = (
        ("warn_unrecorded_worktree", "a warning for work in an unrecorded worktree"),
        ("mr_required", "a merge request (or exemption) before work in a repository"),
        ("release_demanded", "a release record or version exemption before closure"),
        ("merge_required", "a merge record or decision before closure"),
        ("warn_unrecorded_merge", "a warning when closed with no merge recorded"),
    )
    gained = [text for name, text in flags if getattr(new, name) and not getattr(old, name)]
    dropped = [text for name, text in flags if getattr(old, name) and not getattr(new, name)]
    gained += [f"a diff round at {event}" for event in sorted(new.diff_events - old.diff_events)]
    dropped += [f"a diff round at {event}" for event in sorted(old.diff_events - new.diff_events)]
    if new.forbids_target_writes and not old.forbids_target_writes:
        gained.append("no writes to the target")
    return {"gained": gained, "dropped": dropped}


def target_conflicts(state: dict[str, Any], target: str, new: Any) -> list[str]:
    """Records on the worktrees that the rules of `target` would not require."""
    found: list[str] = []
    for entry in state.get("worktrees", []):
        path = entry.get("path")
        if "pull_request" in entry and not new.mr_required:
            found.append(f"worktree {path} has a pull_request that {target} would not require")
        if isinstance(entry.get("merge"), dict) and not new.merge_required:
            found.append(f"worktree {path} has a merge record that {target} would not require")
        line = entry.get("version_line")
        if isinstance(line, dict) and line.get("release") is not None and not new.release_demanded:
            found.append(f"worktree {path} has a release record that {target} would not require")
        if target == "research" and entry.get("kind") not in (None, "none") and entry.get("status") != "removed":
            found.append(f"worktree {path} is a {entry.get('kind')} worktree, which research does not use")
    return found


def research_task_conflicts(state: dict[str, Any]) -> list[str]:
    """Open tasks that still write to the target, which a research target forbids."""
    return [
        f"task {task['id']} still writes to the target"
        for task in state.get("tasks", [])
        if task.get("status") not in TERMINAL_TASK_STATUSES
        and any(isinstance(output, dict) and output.get("root") == "target" for output in task.get("outputs", []))
    ]


def migration_plan(state: dict[str, Any], target: object) -> dict[str, Any]:
    """What moving a project to policy 2 with `target` would change, or the refusal; reads state only.

    The preview builds a hypothetical state (policy 2 plus the target) and diffs its rules against the stored
    ones. Records the new rules would not require are listed, never removed; obligations that existing entries
    do not yet satisfy are listed too, and the gates catch them later.
    """
    if not isinstance(target, str) or target not in DELIVERY_TARGETS:
        raise WorkspaceError(f"target must be one of: {', '.join(DELIVERY_TARGETS)}")
    if state.get("schema_version") != 4:
        raise WorkspaceError(
            f"migrate-policy needs schema_version 4, but this project has {state.get('schema_version')}"
        )
    old = delivery_rules(state)
    if has_target_rules(old.policy):
        raise WorkspaceError(
            f"this project is already at policy {old.policy}; change its target with 'workflow retarget'"
        )
    if state.get("status") in ("ALIGNING", "DONE", "CANCELLED"):
        raise WorkspaceError(
            f"a {state.get('status')} project cannot migrate: finish alignment first, "
            "and a closed project keeps its policy"
        )
    busy = [
        f"{item['id']} ({item['status']})"
        for item in state.get("tasks", [])
        if item.get("status") in ("RUNNING", "BLOCKED")
    ]
    if busy:
        raise WorkspaceError("migrate-policy needs no RUNNING or BLOCKED task: " + ", ".join(busy))
    execution = state.get("execution")
    if isinstance(execution, dict) and (execution.get("attempts") or execution.get("coordinator_run") is not None):
        raise WorkspaceError("migrate-policy is refused while an attempt is open or a coordinator run is set")
    if target == "research":
        writing = research_task_conflicts(state)
        if writing:
            raise WorkspaceError("a research target forbids writes to the target: " + "; ".join(writing))
    new = delivery_rules({**state, "policy_version": 2, "delivery_target": target})
    # The closure check itself, run on the hypothetical state, so the preview and the gate cannot drift.
    from workspace_delivery import closure_findings

    unsatisfied = closure_findings({**state, "policy_version": 2, "delivery_target": target})
    notes = [
        f"worktree {item.get('path')} keeps its local_only exemption, which stays valid; "
        "policy 2 only refuses recording a new one"
        for item in state.get("worktrees", [])
        if isinstance(item.get("pr_exemption"), dict) and item["pr_exemption"].get("reason") == "local_only"
        and item.get("kind") not in (None, "none") and item.get("status") != "removed"
    ]
    return {
        "from_policy": old.policy,
        "to_policy": 2,
        "delivery_target": target,
        **rules_diff(old, new),
        "unsatisfied": unsatisfied,
        "contradictions": target_conflicts(state, target, new),
        "notes": notes,
    }


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


def _start_findings(state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Everything that stops this task from starting, each rule once and the next step named."""
    findings = []
    held = authorization_findings(task, "RUNNING")
    if held:
        findings.append(
            f"{task['id']}: {held[0]}; record `workflow <project-dir> authorize` with the user's words"
        )
        if task["effect"]["kind"] == "external":
            # Advisory: receipts are recorded while the task runs and are required at DONE.
            findings.append(f"{task['id']} will need `workflow <project-dir> receipt` before it can finish")
    if any(output["root"] == "target" for output in task["outputs"]):
        from workspace_delivery import start_findings

        findings.extend(f"{task['id']} writes to the target: {finding}" for finding in start_findings(state))
    return findings


def task_preconditions(
    state: dict[str, Any],
    task: dict[str, Any],
    action: str,
    successor: dict[str, Any] | None,
    record_ids: list[str],
    observation_ids: list[str],
) -> list[str]:
    """Every unmet precondition of `task start` or `task finish`, gathered so one message names them all."""
    if action == "start":
        return _start_findings(state, task)
    findings = [*evidence_findings(record_ids, observation_ids), *receipt_findings(task)]
    if successor is not None:
        findings.extend(_start_findings(state, successor))
    return findings


def _refuse_unmet(task_id: str, action: str, findings: list[str]) -> None:
    if findings:
        raise WorkspaceError(f"task {task_id} cannot {action}: " + "; ".join(findings))


REPLAN_ONLY_FINISH = "--replan applies only to task finish"
REPLAN_NEEDS_COORDINATOR = (
    "--replan needs coordinator mode (policy_version 6 with options.coordinator on); this project stores no replan "
    "statement"
)
REPLAN_MALFORMED = '--replan takes none or changed, with --replan-reason "<one line, at most 200 characters>"'
REPLAN_START_NEXT = (
    "--start-next is refused with --replan changed; finish first, then start the next task from the changed plan"
)


def _replan_statement(
    state: dict[str, Any], action: str, task_id: str, *, replan: "str | None", replan_reason: "str | None",
    backfill: bool, start_next: "str | None",
) -> "dict[str, str] | None":
    """The command-line checks of the mode flags, in the order of design section 4.3; the statement to store."""
    given = replan is not None or replan_reason is not None
    if given and action != "finish":
        raise WorkspaceError(REPLAN_ONLY_FINISH)
    coordinator = coordinator_enabled(state)
    if given and not coordinator:
        raise WorkspaceError(REPLAN_NEEDS_COORDINATOR)
    if backfill and coordinator and action == "finish":
        raise WorkspaceError(
            f"task {task_id} cannot finish: --backfill is refused in coordinator mode; start the task, dispatch a "
            "worker and use a plain task finish"
        )
    if not given:
        return None
    reason = replan_reason.strip() if isinstance(replan_reason, str) else None
    if not isinstance(replan, str) or reason is None or not replan_valid({"statement": replan, "reason": reason}):
        raise WorkspaceError(REPLAN_MALFORMED)
    if replan == "changed" and start_next is not None:
        raise WorkspaceError(REPLAN_START_NEXT)
    return {"statement": replan, "reason": reason}


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
    stamp_start: bool = True,
    replan: str | None = None,
    replan_reason: str | None = None,
) -> dict[str, Any]:
    """Start, finish, block, or skip a named task with one guarded state commit.

    `backfill` finishes a task whose work already happened without `task start`: it is a start and
    a finish, each an ordinary guarded commit, with the reason saved between them. It is refused in
    coordinator mode. `replan` and `replan_reason` are the replan statement a finish stores in
    coordinator mode (policy 6); the mode gate is listed with the other unmet preconditions.
    """
    project_dir = project_dir.resolve()
    loaded = _load_state(project_dir)
    statement = _replan_statement(
        loaded, action, task_id, replan=replan, replan_reason=replan_reason, backfill=backfill, start_next=start_next,
    )
    if backfill:
        return _backfill_finish(
            project_dir, action, task_id, expected_revision=expected_revision,
            evidence_record_ids=evidence_record_ids, start_next=start_next, lock_timeout=lock_timeout, note=note,
            observation_record_ids=observation_record_ids,
        )
    state = copy.deepcopy(loaded)
    before_tasks = copy.deepcopy(state["tasks"])
    selected = _task(state, task_id)
    changed = [task_id]
    locked_guard = None
    stamps = task_stamps_enabled(state)
    durable = task_durable_enabled(state)
    before_by_id = {task["id"]: task for task in before_tasks}
    if action == "start":
        _refuse_unmet(task_id, "start", task_preconditions(state, selected, "start", None, [], []))
        selected.update(status="RUNNING", block_reason=None, skip_reason=None)
        if durable:
            selected.update(stamp_transitions(before_by_id[task_id], selected))
        # The first start only: a task restarted after BLOCKED keeps the moment work began.
        if stamps and stamp_start and "started_at" not in selected:
            selected["started_at"] = now_iso()
        if state["status"] in ("PLANNING", "BLOCKED", "REVIEW"):
            state["status"] = "EXECUTING"
    elif action == "finish":
        record_ids = evidence_record_ids or []
        observation_ids = observation_record_ids or []
        successor = None
        if start_next is not None:
            if start_next == task_id:
                raise WorkspaceError("--start-next must name a different task")
            successor = _task(state, start_next)
        findings = task_preconditions(state, selected, "finish", successor, record_ids, observation_ids)
        if coordinator_enabled(state):
            if statement is not None:
                selected["replan"] = statement
            # Early, for one complete message and a refusal before the attested-finish record; the shared gate
            # runs again at the commit and is the rule that counts.
            findings.extend(task_finish_findings(project_dir, loaded, {**selected, "status": "DONE"}))
        _refuse_unmet(task_id, "finish", findings)
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
        if stamps:
            selected["finished_at"] = now_iso()
        selected["evidence"] = list(selected["evidence"])
        for reference in [*references, *observed]:
            if reference not in selected["evidence"]:
                selected["evidence"].append(reference)
        if successor is not None:
            successor.update(status="RUNNING", block_reason=None, skip_reason=None)
            if durable:
                successor.update(stamp_transitions(before_by_id[successor["id"]], successor))
            if stamps and "started_at" not in successor:
                successor["started_at"] = now_iso()
            # Starting a task is what moves a project into EXECUTING, whether it is the command's
            # subject or its successor. A direct `TODO -> DONE` finish leaves the project alone,
            # so without this the successor would be RUNNING inside a PLANNING project.
            if state["status"] in ("PLANNING", "BLOCKED", "REVIEW"):
                state["status"] = "EXECUTING"
            changed.append(successor["id"])

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
            if durable:
                selected.update(stamp_transitions(before_by_id[task_id], selected))
                if before_by_id[task_id].get("status") == "BLOCKED":
                    # An explicit block is a re-block: it retires any earlier wait, as at policy 3.
                    selected["block_generation"] += 1
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
    if action in ("finish", "block", "skip"):
        result.update(next_move(committed))
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
    """Command and observation references for a finish; `task_preconditions` has already required a record."""
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
    """Record a task whose work preceded `task start`, keeping the reason on record.

    A plain `task finish` moves `TODO` to `DONE` when no task is `RUNNING`, but it records no
    reason for the missing start. This path writes that reason before the terminal commit, so it
    stays the audited route. Two commits, so every guard an ordinary start and finish enforces
    still applies to each: the
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
    # One message for every unmet precondition of both halves, before anything is written.
    selected = _task(state, task_id)
    successor = _task(state, start_next) if start_next is not None and start_next != task_id else None
    _refuse_unmet(task_id, "backfill", [
        *task_preconditions(state, selected, "start", None, [], []),
        *task_preconditions(
            state, selected, "finish", successor, evidence_record_ids or [], observation_record_ids or []
        ),
    ])
    # Refused before anything is written: a record that is missing, failing or another task's.
    _finish_references(project_dir, task_id, evidence_record_ids or [], observation_record_ids or [])
    # No started_at for a backfill: both of its commits happen now, and a start stamp would claim a
    # zero-length interval for exactly the work that was done earlier. finished_at is still true.
    started = task_operation(
        project_dir, "start", task_id, expected_revision=expected_revision, lock_timeout=lock_timeout,
        stamp_start=False,
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


def init_project(
    workspace_root: Path,
    title: str,
    working_directory: Path,
    *,
    acknowledged: list[str] | None = None,
    lock_timeout: float = 5.0,
    create_root: bool = False,
    briefing: bool = False,
    tags: list[str] | None = None,
) -> tuple[Path, list[str]]:
    """Allocate a project and return its directory with the warnings to show; `init` and `start` share it.

    `tags` are recorded when given. The CLI requires 1 to 5 of them for a new project before it calls here.
    """
    acknowledged = acknowledged or []
    # Before the directory exists: a refused init leaves nothing behind, and only the user's answer
    # lets a live look-alike pass.
    similar_warnings = check_new_project(workspace_root, title, working_directory, acknowledged)
    project_dir = allocate_project(
        workspace_root,
        title=title,
        working_directory=working_directory,
        lock_timeout=lock_timeout,
        create_root=create_root,
        briefing=briefing,
        acknowledged_duplicates=acknowledged,
        tags=tags,
    )
    # Warned at init as well as at validation, because this is the moment someone chooses where the record
    # of the work will live. Checked after allocation, since create_root means the directory to check may not
    # have existed a moment ago.
    return project_dir, [*similar_warnings, *vcs_warnings(workspace_root)]


def require_new_tags(tags: list[str]) -> list[str]:
    """The 1 to 5 tags a new project needs, normalized, or a refusal that says nothing was created."""
    try:
        return normalize_tags(tags, minimum=1)
    except WorkspaceError as error:
        raise WorkspaceError(f"init created nothing: {error}; see `research-project suggest-tags`") from error


def _follow_up_agreement_entry(source_id: str, agreement: str) -> str:
    """The decision-history entry for the user's agreement: a bullet, then the text verbatim in a fenced block.

    The fence is one backtick longer than the longest run in the text (three at least), so the text cannot close
    it, and every character of the text is kept.
    """
    longest = max((len(run) for run in re.findall(r"`+", agreement)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"- Follow-up of {source_id}: the user agreed on {now_iso()[:10]}.\n\n{fence}\n{agreement}\n{fence}"


def _follow_up_briefing(source_id: str, title: str, items: list[dict[str, str]]) -> str:
    """The briefing of a follow-up: the marker line, then every contract heading written, never a placeholder."""
    carried = "\n".join(f"- [{item['kind']}] {' '.join(item['text'].split())} ({item['pointer']})" for item in items)
    sections = {
        "Stated requirements": f"See {source_id}/spec.md.",
        "Verified facts": "None carried over.",
        "Corrected assumptions": "None carried over.",
        "Background": f"See {source_id}/reflection.md.",
        "Open questions for grill": carried or "None carried over.",
    }
    body = "".join(f"## {name}\n\n{sections[name]}\n\n" for name, _ in BRIEFING_CANONICAL_SECTIONS)
    return f"Follow-up of: {source_id}\n\n# {title.strip()} \u2014 briefing\n\n{body}"


def follow_up_project(
    source_dir: Path,
    *,
    title: str,
    agreement: str,
    tags: list[str] | None = None,
    working_directory: Path | None = None,
    lock_timeout: float = 5.0,
) -> tuple[Path, list[str]]:
    """Create the follow-up of a finished project and return its directory with the warnings to show.

    The source is only read, and nothing is written into it, not even a lock. The new project starts in
    ALIGNING with no tasks, no worktree and no repository write, beside the source, with `predecessor` set.
    A project that is not DONE, a blank or dangerous agreement, a working directory that is gone, no tags and
    an existing follow-up are each refused before anything is written; every refusal starts with
    `follow-up refused:`. A failure after the project exists says so instead.
    """
    project_dir: Path | None = None
    warnings: list[str] = []
    try:
        source_dir = Path(source_dir).resolve()
        root = source_dir.parent
        # A mistyped path must not create a lock directory in some other folder: check for a project first.
        if not (source_dir / "project.json").is_file():
            raise WorkspaceError(f"{source_dir} holds no project.json")
        # One lock for this source, held outside the source, covers every read, the scan and the creation: two
        # follow-ups of one source cannot both pass the scan, and the finished project is not written to.
        # The lock is a directory that is removed on release.
        with DirectoryLock(root / f".follow-up-{source_dir.name}.lock", timeout=lock_timeout):
            state = load_json(source_dir / "project.json")
            if state.get("project") != source_dir.name:
                raise WorkspaceError("the project field does not match the directory name")
            if state.get("status") != "DONE":
                raise WorkspaceError(f"the source project is {state.get('status')}, not DONE")
            if not isinstance(agreement, str) or not agreement.strip():
                raise WorkspaceError("the agreement text is blank; pass the user's own words with --agreement")
            if any(line.lstrip().startswith("#") for line in agreement.splitlines()):
                raise WorkspaceError(
                    "the agreement text has a line that starts with '#', which would add a heading to the record; "
                    "ask the user to rephrase"
                )
            if "<!--" in agreement:
                raise WorkspaceError(
                    "the agreement text contains '<!--', which could forge a record marker; ask the user to rephrase"
                )
            if working_directory is not None:
                directory = Path(working_directory).resolve()
            else:
                recorded = state.get("working_directory")
                if not isinstance(recorded, str) or not recorded.strip():
                    raise WorkspaceError("the source has no working directory; pass --working-directory <dir>")
                directory = Path(repository_of(str(Path(recorded).resolve())))
            if not directory.is_dir():
                raise WorkspaceError(f"working directory {directory} does not exist; pass --working-directory <dir>")
            stored_tags = state.get("tags")
            if tags:
                new_tags = normalize_tags(tags, minimum=1)
            elif tags_of(state):
                new_tags = tags_of(state)
            elif stored_tags not in (None, []):
                raise WorkspaceError("the source's tags field is malformed; pass 1 to 5 --tag values")
            else:
                raise WorkspaceError("the source has no tags; pass 1 to 5 --tag values")
            rows = _rows(root)
            for row in rows:
                if row.get("predecessor") != source_dir.name:
                    continue
                briefing = Path(row["path"]) / "briefing.md"
                if not briefing.exists():
                    continue  # a maintenance successor has no briefing: only the marker line makes a follow-up
                try:
                    first = read_text(briefing).split("\n", 1)[0]
                except WorkspaceError:
                    warnings.append(
                        f"the briefing of {row['project']} could not be read, so it was not checked as a follow-up"
                    )
                    continue
                if first.rstrip("\r") == f"Follow-up of: {source_dir.name}":
                    raise WorkspaceError(
                        f"{row['project']} is already a follow-up of this project; there is one per project"
                    )
            invalid = sum(1 for row in rows if row.get("status") == "INVALID")
            if invalid:
                plural = "s" if invalid != 1 else ""
                warnings.append(f"{invalid} unreadable project{plural} could not be checked for an existing follow-up")
            bullets: list[str] = []
            reflection_file = source_dir / "reflection.md"
            if reflection_file.exists():  # a legacy project may have none
                try:
                    bullets, incomplete = follow_up_items(read_text(reflection_file))
                except WorkspaceError:
                    warnings.append("reflection.md could not be read, so its follow-up bullets were not carried over")
                else:
                    if incomplete:
                        warnings.append(
                            "the Follow-up section of reflection.md could not be read in full (text with no bullet, "
                            "or a fenced block that is never closed), so some items may not be carried over"
                        )
            items = [{"kind": "reflection", "text": text, "pointer": "reflection.md"} for text in bullets]
            items += residual_findings(source_dir)
            project_dir = allocate_project(
                root,
                title=title,
                working_directory=directory,
                lock_timeout=lock_timeout,
                briefing=_follow_up_briefing(source_dir.name, title, items),
                tags=new_tags,
                predecessor=source_dir.name,
                history_entries=[_follow_up_agreement_entry(source_dir.name, agreement)],
            )
    except IndexNotRebuilt:
        raise  # the project exists and the message already says what to run
    except (WorkspaceError, OSError, ValueError) as error:
        if project_dir is not None:  # only a failure to release the lock can come this late
            shown = f" Warnings: {'; '.join(warnings)}." if warnings else ""
            raise WorkspaceError(
                f"the follow-up project {project_dir} was created, but then: {error}.{shown}"
            ) from error
        raise WorkspaceError(f"follow-up refused: {error}") from error
    return project_dir, warnings


VALIDATION_FAILED = "candidate validation failed:"
OUTPUT_ERROR = re.compile(r"^(?P<owner>task \S+ output #\d+): (?P<rest>.+)$")


def _group_by_output(message: str) -> str:
    """Regroup the bullets of a candidate-validation refusal per output, keeping its first line.

    Bullets that name one output of one task move under a single heading, so a finish with several
    problems on an output reads as one fix. Any other bullet, and a message that is not a validation
    refusal, is returned as it was.
    """
    if not message.startswith(VALIDATION_FAILED + "\n- "):
        return message
    bullets = message[len(VALIDATION_FAILED) + 1 :].split("\n- ")
    bullets[0] = bullets[0][2:]
    groups: dict[str, list[str]] = {}
    plain: list[str] = []
    for bullet in bullets:
        found = OUTPUT_ERROR.match(bullet)
        if found:
            groups.setdefault(found["owner"], []).append(found["rest"])
        else:
            plain.append(bullet)
    lines = [VALIDATION_FAILED]
    for owner, rests in groups.items():
        lines.append(f"- {owner}: " + "; ".join(rests))
    lines += [f"- {bullet}" for bullet in plain]
    return "\n".join(lines)


def finish_and_checkpoint(
    project_dir: Path,
    task_id: str,
    *,
    expected_revision: int,
    evidence_record_ids: list[str] | None = None,
    start_next: str | None = None,
    lock_timeout: float = 5.0,
    backfill: bool = False,
    note: str | None = None,
    observation_record_ids: list[str] | None = None,
    replan: str | None = None,
    replan_reason: str | None = None,
) -> dict[str, Any]:
    """Finish a task, regrouping a validation refusal per output, and save a checkpoint after `--start-next`.

    The task operation runs once, whichever path it takes, and the checkpoint follows it, so a failed
    checkpoint can never be mistaken for a failed finish. The checkpoint writes `handoff.md` only and uses no
    revision, so the revision in the result still fits the next `task start`.
    """
    project_dir = project_dir.resolve()
    try:
        result = task_operation(
            project_dir, "finish", task_id, expected_revision=expected_revision,
            evidence_record_ids=evidence_record_ids, start_next=start_next, lock_timeout=lock_timeout,
            backfill=backfill, note=note, observation_record_ids=observation_record_ids,
            replan=replan, replan_reason=replan_reason,
        )
    except WorkspaceError as error:
        regrouped = _group_by_output(str(error))
        if regrouped == str(error):
            raise
        raise WorkspaceError(regrouped) from error
    from workspace_context import applicability_failure, evidence_applicability

    # After the commit and never failing it: the report says whether each cited check still applies.
    try:
        state = _load_state(project_dir)
        if task_packets_enabled(state):
            result["evidence_applicability"] = evidence_applicability(project_dir, state, evidence_record_ids or [])
    except (WorkspaceError, OSError, ValueError) as error:
        result["evidence_applicability"] = applicability_failure(error)
    if start_next is None:
        return result
    from workspace_context import checkpoint_data
    from workspace_workflows import workflow

    existing, _ = snapshot(project_dir / "handoff.md")
    if existing and checkpoint_data(project_dir) is None:
        result["checkpoint_skipped"] = "legacy handoff"
        return result
    request = {
        "id": re.sub(r"[^a-z0-9-]", "-", f"finish-{task_id}-{result['revision']}".lower())[:64],
        "expected_revision": result["revision"],
        "tokens": {"handoff": document_tokens(project_dir)["handoff"]},
        "continuation": {"next": result["move_detail"], "session": "working"},
    }
    try:
        workflow(project_dir, "checkpoint", request)
    except (WorkspaceError, OSError) as error:
        failed = {
            **result, "operation": "task.finish", "committed": True, "saved": ["finish"],
            "recovery": "run workflow checkpoint", "error": str(error),
        }
        raise OperationError(str(error), failed) from error
    result["checkpoint"] = "saved"
    return result
