"""High-level project operations built on the canonical validation and commit path."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from workspace_documents import append_record, edit_document
from workspace_evidence import evidence_entries, evidence_findings, selected_evidence_references
from workspace_journal import OperationError, snapshot
from workspace_lib import (
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    authorization_findings,
    commit_state,
    now_iso,
    reason_reference_warnings,
    receipt_findings,
    staged_lesson_lines,
    task_stamps_enabled,
    validate_project,
    vcs_warnings,
)
from workspace_session import _load_state, document_tokens, next_move
from workspace_similar import check_new_project


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
) -> dict[str, Any]:
    """Start, finish, block, or skip a named task with one guarded state commit.

    `backfill` finishes a task whose work already happened without `task start`: it is a start and
    a finish, each an ordinary guarded commit, with the reason saved between them.
    """
    project_dir = project_dir.resolve()
    if backfill:
        return _backfill_finish(
            project_dir, action, task_id, expected_revision=expected_revision,
            evidence_record_ids=evidence_record_ids, start_next=start_next, lock_timeout=lock_timeout, note=note,
            observation_record_ids=observation_record_ids,
        )
    state = copy.deepcopy(_load_state(project_dir))
    before_tasks = copy.deepcopy(state["tasks"])
    selected = _task(state, task_id)
    changed = [task_id]
    locked_guard = None
    stamps = task_stamps_enabled(state)
    if action == "start":
        _refuse_unmet(task_id, "start", task_preconditions(state, selected, "start", None, [], []))
        selected.update(status="RUNNING", block_reason=None, skip_reason=None)
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
        _refuse_unmet(
            task_id, "finish", task_preconditions(state, selected, "finish", successor, record_ids, observation_ids)
        )
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
) -> tuple[Path, list[str]]:
    """Allocate a project and return its directory with the warnings to show; `init` and `start` share it."""
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
    )
    # Warned at init as well as at validation, because this is the moment someone chooses where the record
    # of the work will live. Checked after allocation, since create_root means the directory to check may not
    # have existed a moment ago.
    return project_dir, [*similar_warnings, *vcs_warnings(workspace_root)]


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
        )
    except WorkspaceError as error:
        regrouped = _group_by_output(str(error))
        if regrouped == str(error):
            raise
        raise WorkspaceError(regrouped) from error
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
