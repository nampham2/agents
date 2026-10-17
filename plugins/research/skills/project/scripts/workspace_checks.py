"""Explicit argv batches with durable attempts and evidence-only recovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workspace_context import fingerprint
from workspace_journal import (
    OperationError,
    ensure_writable,
    journal_path,
    list_input,
    object_input,
    schema_input,
    string_input,
)
from workspace_lib import (
    DirectoryLock,
    WorkspaceError,
    _append_evidence_entry,
    atomic_write_json,
    document_sha256,
    load_json,
    record_evidence_result,
)
from workspace_session import _load_state


def _command_fields(command: dict[str, Any], where: str, prefix: str = "each check") -> dict[str, Any]:
    """Validate one check-shaped command: nonempty argv, exactly one owner, bounded timeout, safe cwd."""
    object_input(command, {"task", "step", "argv", "timeout", "cwd"}, {"argv"}, where=where)
    try:
        argv = list_input(command["argv"], 100, "argv")
        if not argv or ("task" in command) == ("step" in command):
            raise WorkspaceError(f"{prefix} requires argv and exactly one of task or step")
        for argument in argv:
            string_input(argument)
        if "cwd" in command:
            string_input(command["cwd"], "cwd")
        timeout = command.get("timeout", 300)
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise WorkspaceError("timeout must be positive and at most 3600 seconds")
    except WorkspaceError as error:
        # A malformed control has to read as the control's failure, not as an anonymous check's.
        raise WorkspaceError(f"{where}: {error}") from None
    return command


def verify(project: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run each caller-supplied command at most once; retries only repair saved evidence."""
    schema_input(request, "verify")
    checks = list_input(request["checks"], 20, "checks")
    if not checks or type(request.get("continue_on_failure", False)) is not bool:
        raise WorkspaceError("checks must be nonempty and continue_on_failure boolean")
    for check in checks:
        _command_fields(check, "workflow verify check")
    # The control is declared to fail on the current content. Validate it in the same pre-pass as
    # the checks, before the journal exists, so a malformed batch never records anything.
    control = (
        None
        if "control" not in request
        else _command_fields(request["control"], "workflow verify control", "the control")
    )
    project = project.resolve()
    path = journal_path(project, request["id"])
    signature = document_sha256(json.dumps(request, sort_keys=True))
    journal: dict[str, Any] = {}
    try:
        ensure_writable(_load_state(project))
        path.parent.mkdir(exist_ok=True)
        with DirectoryLock(path.with_suffix(".lock")):
            ensure_writable(_load_state(project))
            if path.exists():
                journal = load_json(path)
                if journal.get("kind") != "checks" or journal.get("signature") != signature:
                    raise WorkspaceError("operation ID already used with different input")
                attempts = list_input(journal.get("attempts"), len(checks) + (1 if control is not None else 0))
                for attempt in attempts:
                    object_input(
                        attempt,
                        {
                            "index",
                            "status",
                            "working_directory",
                            "error",
                            "stdout",
                            "stderr",
                            "result",
                            "lines",
                            "control",
                        },
                        {"index", "status"},
                    )
                    if attempt["status"] not in (
                        "running",
                        "rejected",
                        "launch_error",
                        "timeout",
                        "persistable",
                        "recorded",
                    ):
                        raise WorkspaceError("damaged command attempt; never replay it")
                    if attempt["status"] in ("persistable", "recorded"):
                        result = attempt.get("result")
                        if not isinstance(result, dict) or type(result.get("exit_code")) is not int:
                            raise WorkspaceError("damaged command result; never replay it")
                        if not isinstance(attempt.get("lines"), list) or not all(
                            isinstance(line, str) for line in attempt["lines"]
                        ):
                            raise WorkspaceError("damaged saved evidence; never replay it")
            else:
                journal = {
                    "kind": "checks",
                    "id": request["id"],
                    "signature": signature,
                    "complete": False,
                    "attempts": [],
                    "fingerprints": fingerprint(project, request.get("fingerprints", [])),
                }
                atomic_write_json(path, journal)
            # The control runs last so a genuinely broken check surfaces first; the existing
            # short-circuit may skip it, and a short-circuited batch never reports passed anyway.
            commands = checks if control is None else [*checks, control]
            for index, check in enumerate(commands):
                attempt: dict[str, Any]
                if index < len(journal["attempts"]):
                    attempt = journal["attempts"][index]
                    if attempt["status"] == "running":
                        raise WorkspaceError("command outcome unknown; inspect host/processes; this ID never reruns it")
                else:
                    attempt = {"index": index, "status": "not_started"}
                    if control is not None and index == len(checks):
                        attempt["control"] = True
                    journal["attempts"].append(attempt)

                    def observe(observation: dict[str, Any], selected: dict[str, Any] = attempt) -> None:
                        selected.update(observation)
                        atomic_write_json(path, journal)

                    try:
                        record_evidence_result(
                            project,
                            check.get("task"),
                            check["argv"],
                            step=check.get("step"),
                            timeout=check.get("timeout", 300),
                            observe=observe,
                            cwd=Path(check["cwd"]) if "cwd" in check else None,
                        )
                    except WorkspaceError as error:
                        attempt["error"] = str(error)
                        if attempt["status"] == "not_started":
                            attempt["status"] = "rejected"
                        atomic_write_json(path, journal)
                        if attempt["status"] == "persistable":
                            raise
                    else:
                        attempt["status"] = "recorded"
                        atomic_write_json(path, journal)
                if attempt["status"] == "persistable":
                    _append_evidence_entry(project, attempt["lines"], lock_timeout=5)
                    attempt["status"] = "recorded"
                    attempt.pop("error", None)
                    atomic_write_json(path, journal)
                failed = attempt["status"] != "recorded" or attempt["result"]["exit_code"] != 0
                if failed and (attempt["status"] != "recorded" or not request.get("continue_on_failure", False)):
                    break
            journal["complete"] = True
            atomic_write_json(path, journal)
            attempts = [
                {key: value for key, value in attempt.items() if key != "lines"} for attempt in journal["attempts"]
            ]
            # Everything before the control slot belongs to the declared checks; beyond it there can
            # only be the control attempt. omitted_checks therefore never counts the control.
            normal = attempts[: len(checks)]
            control_attempt = attempts[len(checks)] if len(attempts) > len(checks) else None
            checks_passed = len(normal) == len(checks) and all(
                item["status"] == "recorded" and item["result"]["exit_code"] == 0 for item in normal
            )
            # "Fired" means observed failing: a recorded non-zero exit. Timeout, launch error and
            # rejection carry no fabricated exit code, so they are not firings - the batch fails
            # instead, because a control that cannot be observed failing proves nothing.
            fired = (
                control_attempt is not None
                and control_attempt["status"] == "recorded"
                and control_attempt["result"]["exit_code"] != 0
            )
            passed = (
                all(item["status"] == "recorded" and item["result"]["exit_code"] == 0 for item in attempts)
                if control is None
                else checks_passed and fired
            )
            return {
                "operation_id": request["id"],
                "complete": True,
                "attempts": attempts,
                "passed": passed,
                "controls_fired": f"{'1' if fired else '0'}/{'1' if control is not None else '0'}",
                "omitted_checks": len(checks) - len(normal),
                "fingerprints": journal["fingerprints"],
            }
    except (OSError, WorkspaceError) as error:
        raise OperationError(
            str(error),
            {
                "operation_id": request["id"],
                "complete": False,
                "attempts": [
                    {key: value for key, value in item.items() if key != "lines"}
                    for item in journal.get("attempts", [])
                ],
                "error": str(error),
                "recovery": "retry identical verify ID/input to repair evidence; unknown attempts never rerun",
            },
        ) from error


def control_warnings(project: Path) -> list[str]:
    """Warn at finalize when a project verified content but never observed a control failing.

    Like memory_staging_warnings, this is a non-blocking warning, not an error: it tells the
    caller the gate the project never exercised, without refusing closure that is otherwise
    sound. Only a recorded non-zero exit counts as observed failing; a timeout, launch error or
    rejection carries no exit code, and journals without a control slot (legacy layouts) count
    as never observed. Unreadable journals are skipped rather than repaired at finalize.
    """
    project = project.resolve()
    batches = 0
    for path in sorted((project / ".operations").glob("*.json")):
        try:
            journal = load_json(path)
        except (OSError, WorkspaceError, ValueError):
            continue
        if journal.get("kind") != "checks" or not isinstance(journal.get("attempts"), list):
            continue
        batches += 1
        for attempt in journal["attempts"]:
            if not isinstance(attempt, dict):
                continue
            result = attempt.get("result")
            if (
                attempt.get("control") is True
                and attempt.get("status") == "recorded"
                and isinstance(result, dict)
                and type(result.get("exit_code")) is int
                and result["exit_code"] != 0
            ):
                return []
    if not batches:
        return []
    return [
        "verify batches ran but no control was ever observed failing; "
        "add a control to a verify batch so the batch proves its checks can fail"
    ]
