"""Named lifecycle compositions. Agents supply judgment; scripts persist and validate facts."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from memory_search import memory_health
from workspace_context import checkpoint_freshness, fingerprint, render_checkpoint, resume_bundle, selected_read
from workspace_documents import _replace_sections, _section_span
from workspace_journal import (
    document_path,
    journal_path,
    list_input,
    object_input,
    recover_operation,
    run_operation,
    snapshot,
    string_input,
)
from workspace_lib import (
    ARCHITECTURE_STATUS_PATTERN,
    PROJECT_TRANSITIONS,
    WorkspaceError,
    check_state_candidate,
    document_sha256,
    now_iso,
    validate_project,
)
from workspace_operations import _task
from workspace_session import _load_state, prepare_update

COMMON = {"id", "expected_revision", "tokens"}
FIELDS = {
    "checkpoint": {"continuation"},
    "round": {"sections", "decisions", "architecture", "continuation"},
    "confirm": {"kind", "proposal_sha256", "review_id", "response", "source", "scope", "continuation"},
    "correct": {"task", "replacement", "reason", "continuation"},
    "reconcile": {"patch", "decision", "continuation"},
    "worker-event": {"task", "handle", "event", "scope", "observation", "assignment_revision"},
    "authorize": {"task", "authorization"},
    "receipt": {"task", "receipt"},
    "review": {"reviewer", "version", "scope", "findings", "status", "evidence", "required"},
    "finalize": {"reflection", "continuation"},
    "report": {"sections", "graph"},
    "maintenance": {"tasks", "reason", "continuation"},
    "cancel": {"tasks", "reason", "continuation"},
    "assess": {"topic", "disposition", "reason", "application"},
}
READ_ACTIONS = {
    "resume",
    "packet",
    "read",
    "freshness",
    "fingerprint",
    "preview",
    "impact",
    "readiness",
    "memory-health",
    "recover",
    "verify",
    "triage",
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
        "warnings": report.warnings,
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
    object_input(request, {"tasks", "paths"}, {"tasks"})
    state = _load_state(project)
    ids = {string_input(value) for value in list_input(request["tasks"])}
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
        string_input(ref["root"])
        string_input(ref["path"])
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


def readiness(project: Path) -> dict[str, Any]:
    """Group existing closure findings without introducing a second gate."""
    report = validate_project(project, close=True, check_index=True)
    groups: dict[str, list[dict[str, str]]] = {}
    for level, findings in (("error", report.errors), ("warning", report.warnings)):
        for finding in findings:
            group = next(
                (
                    key
                    for key in ("receipt", "review", "reflection", "memory", "index", "output", "task")
                    if key in finding.lower()
                ),
                "project",
            )
            groups.setdefault(group, []).append({"level": level, "finding": finding})
    return {"valid": report.valid, "groups": groups}


def worker_events(project: Path) -> list[dict[str, Any]]:
    """Project the last observation per task/handle; opaque host liveness remains unknown."""
    latest = {}
    for task in _load_state(project)["tasks"]:
        text, _ = snapshot(document_path(project, "tasks/" + task["id"]))
        for match in re.finditer(r"^<!-- research-worker (.+) -->$", text, re.MULTILINE):
            try:
                event = json.loads(match[1])
            except ValueError as error:
                raise WorkspaceError("invalid worker event JSON") from error
            object_input(event, FIELDS["worker-event"] | {"recorded"}, FIELDS["worker-event"])
            for field in ("task", "handle", "event", "scope", "observation"):
                string_input(event[field])
            latest[(event["task"], event["handle"])] = event
    return [item for item in latest.values() if item["event"] not in ("completed", "stopped", "failed")]


def _build(
    project: Path, action: str, request: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    documents: dict[str, str] = {}
    patch: dict[str, Any] | None = None
    result: dict[str, Any] = {}

    def read(name: str) -> str:
        return documents.get(name, snapshot(document_path(project, name))[0])

    def decision(body: str) -> None:
        documents["spec"] = _entry(read("spec"), request["id"], body, decision=True)

    if action == "round":
        if "sections" in request:
            if not isinstance(request["sections"], dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in request["sections"].items()
            ):
                raise WorkspaceError("sections must map headings to text")
            documents["spec"] = _replace_sections(read("spec"), request["sections"])
        if "architecture" in request:
            documents["architecture"] = string_input(request["architecture"]).strip() + "\n"
        for item in list_input(request.get("decisions", [])):
            object_input(item, {"id", "body"}, {"id", "body"})
            documents["spec"] = _entry(read("spec"), item["id"], item["body"], decision=True)
    elif action == "confirm":
        kind = string_input(request.get("kind"))
        if kind not in ("requirements", "architecture"):
            raise WorkspaceError("confirmation kind must be requirements or architecture")
        name = "spec" if kind == "requirements" else "architecture"
        if snapshot(document_path(project, name))[1] != string_input(request.get("proposal_sha256")):
            raise WorkspaceError("proposal token changed; confirmation must cover the current proposal")
        for field in ("review_id", "response", "source", "scope"):
            string_input(request.get(field))
        body = "\n".join(
            f"{key}: {request[key]}" for key in ("kind", "review_id", "proposal_sha256", "response", "source", "scope")
        )
        decision(body)
        if kind == "architecture":
            architecture = read("architecture")
            match = ARCHITECTURE_STATUS_PATTERN.search(architecture)
            if match is None or request["review_id"] not in architecture:
                raise WorkspaceError("architecture must declare its status and reviewed identifier")
            documents["architecture"] = (
                architecture[: match.start("status")] + "agreed" + architecture[match.end("status") :]
            )
            documents["architecture"] += f"\nConfirmation ({now_iso()}):\n\n{body}\n"
            heading = "Constraints and important assumptions"
            _, _, start, end = _section_span(read("spec"), heading)
            documents["spec"] = _replace_sections(
                read("spec"),
                {
                    heading: read("spec")[start:end].rstrip()
                    + f"\n\nAgreed architecture: [revision {request['review_id']}](architecture.md)."
                },
            )
    elif action == "correct":
        old = _task(state, string_input(request.get("task")))
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
        decision(string_input(request.get("decision")))
    elif action == "worker-event":
        _task(state, string_input(request.get("task")))
        for field in ("handle", "scope", "observation"):
            string_input(request.get(field))
        if request.get("event") not in ("intent", "launched", "observed", "completed", "stopped", "failed"):
            raise WorkspaceError("unsupported worker observation")
        if (
            type(request.get("assignment_revision")) is not int
            or not 0 <= request["assignment_revision"] <= state["revision"]
        ):
            raise WorkspaceError("invalid assignment revision")
        event = {key: request[key] for key in FIELDS[action]}
        event["recorded"] = now_iso()
        name = "tasks/" + request["task"]
        documents[name] = _entry(
            read(name), request["id"], "<!-- research-worker " + json.dumps(event, sort_keys=True) + " -->"
        )
    elif action in ("authorize", "receipt"):
        task = _task(state, string_input(request.get("task")))
        if action == "authorize":
            value = object_input(
                request.get("authorization"), {"required", "status", "scope", "source", "authorized_at"}
            )
            change = {"authorization": value}
        else:
            value = object_input(request.get("receipt"), {"kind", "value", "destination", "timestamp"})
            change = {"receipts": task["receipts"] + ([] if value in task["receipts"] else [value])}
        patch = {"tasks": [{"id": task["id"], **change}]}
    elif action == "review":
        for field in ("reviewer", "version", "scope", "findings", "status"):
            string_input(request.get(field))
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
    elif action == "finalize":
        documents["reflection"] = string_input(request.get("reflection")).strip() + "\n"
        patch = {"status": "DONE"}
        result["_after_commit"] = ["handoff"]
    elif action in ("maintenance", "cancel"):
        reason = string_input(request.get("reason"))
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

        topic = read_memory_topic(project.parent, string_input(request.get("topic")))
        if request.get("disposition") not in ("apply", "reject", "defer"):
            raise WorkspaceError("lesson disposition must be apply, reject or defer")
        body = f"Lesson {request['topic']} ({topic['sha256']}): {request['disposition']}\n"
        body += string_input(request.get("reason")) + "\n" + string_input(request.get("application"))
        decision(body)
        result["topic"] = {"name": request["topic"], "sha256": topic["sha256"]}
    elif action == "report":
        from workspace_reports import render_report

        documents["artifacts/report"], result = render_report(project, state, request)
    if "continuation" in request or action in ("checkpoint", "finalize", "maintenance", "cancel", "reconcile"):
        target = prepare_update(state, patch) if patch else copy.deepcopy(state)
        target["revision"] += int(patch is not None)
        documents["handoff"], metadata = render_checkpoint(project, target, request.get("continuation", {}), documents)
        result["resume_prompt"] = (
            f"Use the project skill to resume {project}. Run workflow resume with selected sources; "
            f"reconcile ownership first. Checkpoint revision {target['revision']}; "
            f"handoff SHA-256 {document_sha256(documents['handoff'])}. Next: {metadata['continuation']['next']}"
        )
    return documents, patch, result


def workflow(project: Path, action: str, request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a closed vocabulary of operations; never evaluate workspace instructions."""
    try:
        return _workflow(project, action, request)
    except OSError as error:
        raise WorkspaceError(f"workflow filesystem failure: {error}") from error


def _workflow(project: Path, action: str, request: dict[str, Any]) -> dict[str, Any]:
    project = project.resolve()
    if action in FIELDS:
        object_input(request, COMMON | FIELDS[action], {"id", "expected_revision"})
        return run_operation(project, action, request, lambda state: _build(project, action, request, state))
    if action == "verify":
        from workspace_checks import verify

        return verify(project, request)
    if action == "triage":
        from workspace_lessons import triage

        return triage(project, request)
    if action in ("resume", "packet"):
        result = resume_bundle(project, request)
        events = worker_events(project)
        result["unresolved_workers"] = events[:20]
        result["omitted_workers"] = max(0, len(events) - 20)
        return result
    if action == "read":
        return selected_read(project, request)
    if action == "impact":
        return impact(project, request)
    if action == "preview":
        object_input(request, {"patch", "expected_revision"}, {"patch", "expected_revision"})
        return preview(project, request["patch"], request["expected_revision"])
    if action == "fingerprint":
        object_input(request, {"references"}, {"references"})
        return {"fingerprints": fingerprint(project, request["references"])}
    if action == "recover":
        object_input(request, {"id", "apply"}, {"id"})
        if "apply" in request and type(request["apply"]) is not bool:
            raise WorkspaceError("recover apply must be boolean")
        return recover_operation(project, request["id"], apply=request.get("apply", False))
    object_input(request, set())
    if action == "freshness":
        return checkpoint_freshness(project)
    if action == "readiness":
        return readiness(project)
    if action == "memory-health":
        return memory_health(project.parent, _load_state(project)["title"])
    raise WorkspaceError(f"unknown workflow action: {action}")
