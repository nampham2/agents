"""Explicit argv batches with durable attempts and evidence-only recovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workspace_context import fingerprint
from workspace_journal import OperationError, ensure_writable, journal_path, list_input, object_input, string_input
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


def verify(project: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run each caller-supplied command at most once; retries only repair saved evidence."""
    object_input(request, {"id", "checks", "continue_on_failure", "fingerprints"}, {"id", "checks"})
    checks = list_input(request["checks"], 20)
    if not checks or type(request.get("continue_on_failure", False)) is not bool:
        raise WorkspaceError("checks must be nonempty and continue_on_failure boolean")
    for check in checks:
        object_input(check, {"task", "step", "argv", "timeout"}, {"argv"})
        argv = list_input(check["argv"], 100)
        if not argv or ("task" in check) == ("step" in check):
            raise WorkspaceError("each check requires argv and exactly one of task or step")
        for argument in argv:
            string_input(argument)
        timeout = check.get("timeout", 300)
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise WorkspaceError("timeout must be positive and at most 3600 seconds")
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
                attempts = list_input(journal.get("attempts"), len(checks))
                for attempt in attempts:
                    object_input(
                        attempt,
                        {"index", "status", "working_directory", "error", "stdout", "stderr", "result", "lines"},
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
            for index, check in enumerate(checks):
                attempt: dict[str, Any]
                if index < len(journal["attempts"]):
                    attempt = journal["attempts"][index]
                    if attempt["status"] == "running":
                        raise WorkspaceError("command outcome unknown; inspect host/processes; this ID never reruns it")
                else:
                    attempt = {"index": index, "status": "not_started"}
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
            return {
                "operation_id": request["id"],
                "complete": True,
                "attempts": attempts,
                "passed": all(item["status"] == "recorded" and item["result"]["exit_code"] == 0 for item in attempts),
                "omitted_checks": len(checks) - len(attempts),
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
