"""Named lifecycle compositions. Agents supply judgment; scripts persist and validate facts."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from memory_search import memory_health
from workspace_context import (
    checkpoint_freshness,
    fingerprint,
    render_checkpoint,
    resume_bundle,
    selected_read,
    worktree_closure_findings,
    worktree_observation,
    worktree_registered,
)
from workspace_documents import _replace_sections, _section_span
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
    ARCHITECTURE_STATUS_PATTERN,
    PROJECT_TRANSITIONS,
    WorkspaceError,
    check_state_candidate,
    confirmation_marker,
    document_sha256,
    memory_staging_warnings,
    now_iso,
    reason_reference_warnings,
    validate_project,
)
from workspace_operations import _task
from workspace_session import _headings, _load_state, prepare_update

# Shortest reflection `finalize` accepts: enough for an outcome and a limitation, not a bare token.
REFLECTION_MIN_CHARS = 200
COMMON = {"id", "expected_revision", "tokens"}
# Every write action needs these two; per-action rules are checked when the operation runs.
WRITE_REQUIRED = {"id", "expected_revision"}
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
    "worktree": {
        "operation", "kind", "role", "repository", "path", "branch", "confirmation", "decision", "continuation",
    },
}
# Derived, not listed twice: an action is a read action exactly when it has an input table.
READ_ACTIONS = set(READ_INPUTS)


def input_schema(action: str) -> dict[str, Any]:
    """Allowed and required top-level keys of a workflow action, from the tables validation uses."""
    if action in FIELDS:
        allowed, required = COMMON | FIELDS[action], WRITE_REQUIRED
        note = "write action: further per-action rules are checked when it runs (see references/automation-records.md)"
    else:
        allowed, required = READ_INPUTS[action]
        note = "read action: the keys below are the whole request"
    return {"action": action, "allowed": sorted(allowed), "required": sorted(required), "note": note}


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


def readiness(project: Path) -> dict[str, Any]:
    """Group existing closure findings without introducing a second gate."""
    report = validate_project(project, close=True, check_index=True)
    errors, warnings = worktree_closure_findings(_load_state(project))
    report.errors.extend(errors)
    report.warnings.extend(warnings)
    groups: dict[str, list[dict[str, str]]] = {}
    for level, findings in (("error", report.errors), ("warning", report.warnings)):
        for finding in findings:
            group = next(
                (
                    key
                    for key in ("worktree", "receipt", "review", "reflection", "memory", "index", "output", "task")
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
                string_input(event[field], field)
            latest[(event["task"], event["handle"])] = event
    return [item for item in latest.values() if item["event"] not in ("completed", "stopped", "failed")]


def _confirmation(value: object) -> dict[str, str]:
    confirmation = object_input(value, {"source", "response"}, {"source", "response"})
    return {key: string_input(confirmation[key], f"confirmation.{key}") for key in ("source", "response")}


def _worktree(state: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record or close a repository worktree from what Git reports, never from what was claimed.

    `record` stores a worktree the agent created after the user confirmed it (or the linked
    worktree the target already was, or the fact that the target is no repository) and repoints
    the target root at it. `close` stores the user's keep/remove/accept-dirty decision beside the
    dirty snapshot observed at that moment. Neither runs a Git write: creation and removal are the
    agent's, under the user's authorization, and this only refuses to record what Git contradicts.
    """
    worktrees = copy.deepcopy(state.get("worktrees", []))
    path = string_input(request.get("path"), "path")
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
                raise WorkspaceError(f"repository mismatch: requested {repository}, observed {seen['repository']}")
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
    if operation != "close":
        raise WorkspaceError("worktree operation must be record or close")
    decision = request.get("decision")
    if decision not in ("keep", "accept_dirty", "remove"):
        raise WorkspaceError("worktree decision must be keep, accept_dirty or remove")
    entry = next((item for item in worktrees if item["path"] == path), None)
    # A kept worktree can still be removed afterwards, by the user and outside the tool; recording
    # that is the one decision that may follow an earlier closure. Keeping or accepting dirty paths a
    # second time would only overwrite the first decision, so those still need an active entry.
    closable = ("active", "kept") if decision == "remove" else ("active",)
    if entry is None or entry["status"] not in closable or entry["kind"] == "none":
        raise WorkspaceError(f"no {' or '.join(closable)} repository worktree is recorded at {path}")
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
    return patch, {"worktree": entry, "observed": seen}


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
            documents["architecture"] = string_input(request["architecture"], "architecture").strip() + "\n"
        for item in list_input(request.get("decisions", []), field="decisions"):
            object_input(item, {"id", "body"}, {"id", "body"})
            documents["spec"] = _entry(read("spec"), item["id"], item["body"], decision=True)
    elif action == "confirm":
        kind = string_input(request.get("kind"), "kind")
        if kind not in ("requirements", "architecture"):
            raise WorkspaceError("confirmation kind must be requirements or architecture")
        name = "spec" if kind == "requirements" else "architecture"
        if snapshot(document_path(project, name))[1] != string_input(request.get("proposal_sha256")):
            raise WorkspaceError("proposal token changed; confirmation must cover the current proposal")
        for field in ("review_id", "response", "source", "scope"):
            string_input(request.get(field), field)
        body = confirmation_marker(kind, str(request["review_id"]), str(request["proposal_sha256"])) + "\n" + "\n".join(
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
        _task(state, string_input(request.get("task"), "task"))
        for field in ("handle", "scope", "observation"):
            string_input(request.get(field), field)
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
        task = _task(state, string_input(request.get("task"), "task"))
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
        documents["reflection"] = reflection + "\n"
        result["memory_staging_warnings"] = memory_staging_warnings(project)
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
        patch, result = _worktree(state, request)
    elif action == "report":
        from workspace_reports import render_report

        documents["artifacts/report"], result = render_report(project, state, request)
    if "continuation" in request or action in ("checkpoint", "finalize", "maintenance", "cancel", "reconcile"):
        target = prepare_update(state, patch, internal=True) if patch else copy.deepcopy(state)
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
    if action not in FIELDS and action not in READ_INPUTS:
        raise WorkspaceError(f"unknown workflow action: {action}")
    if action in FIELDS:
        object_input(request, COMMON | FIELDS[action], WRITE_REQUIRED, where=f"workflow {action}")
        return run_operation(project, action, request, lambda state: _build(project, action, request, state))
    if action == "verify":
        from workspace_checks import verify

        return verify(project, request)
    if action == "triage":
        from workspace_lessons import triage

        return triage(project, request)
    if action in ("resume", "packet"):
        schema_input(request, action)
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
        return readiness(project)
    # Every other action returned above, and the unknown ones were refused at the top.
    return memory_health(project.parent, _load_state(project)["title"])
