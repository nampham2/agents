#!/usr/bin/env python3
"""Shared state, validation, locking, migration, and index helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:  # TypeGuard is 3.10+; the scripts must still import on system python 3.9.
    from typing import TypeGuard

PROJECT_STATUSES = {"ALIGNING", "PLANNING", "EXECUTING", "REVIEW", "BLOCKED", "DONE", "CANCELLED"}
TASK_STATUSES = {"TODO", "RUNNING", "DONE", "BLOCKED", "SKIPPED"}
TERMINAL_TASK_STATUSES = {"DONE", "SKIPPED"}
EFFECT_KINDS = {"none", "local_write", "destructive", "external"}
AUTHORIZATION_STATUSES = {"not_required", "pending", "explicit", "denied", "deferred"}
REVIEW_STATUSES = {"not_required", "pending", "accepted", "recorded"}
REFERENCE_ROOTS = {"workspace", "target", "external"}

# Project directories are allocated as YYYY-MM-DD-NNN. A directory that does not match was made by
# hand or by an older tool: `project` must equal the directory name, so a malformed name becomes a
# malformed canonical ID that no amount of committing can rename.
PROJECT_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{3}$")

WORKSPACE_ROOT_ENV_VAR = "RESEARCH_WORKSPACE"

PROJECT_TRANSITIONS = {
    "ALIGNING": {"ALIGNING", "PLANNING", "BLOCKED", "CANCELLED"},
    "PLANNING": {"PLANNING", "EXECUTING", "BLOCKED", "CANCELLED"},
    "EXECUTING": {"EXECUTING", "REVIEW", "DONE", "BLOCKED", "CANCELLED"},
    "REVIEW": {"REVIEW", "EXECUTING", "DONE", "BLOCKED", "CANCELLED"},
    "BLOCKED": {"BLOCKED", "ALIGNING", "PLANNING", "EXECUTING", "CANCELLED"},
    "DONE": {"DONE", "PLANNING"},
    "CANCELLED": {"CANCELLED"},
}

TASK_TRANSITIONS = {
    "TODO": {"TODO", "RUNNING", "BLOCKED", "SKIPPED"},
    "RUNNING": {"RUNNING", "TODO", "DONE", "BLOCKED", "SKIPPED"},
    "BLOCKED": {"BLOCKED", "TODO", "RUNNING", "SKIPPED"},
    "DONE": {"DONE"},
    "SKIPPED": {"SKIPPED"},
}

PROJECT_FIELDS = {
    "schema_version",
    "project",
    "title",
    "status",
    "created",
    "updated",
    "working_directory",
    "revision",
    "current_tasks",
    "review",
    "cancellation_reason",
    "tasks",
    "predecessor",
}
TASK_FIELDS = {
    "id",
    "name",
    "status",
    "depends_on",
    "outputs",
    "success_criteria",
    "verification",
    "evidence",
    "effect",
    "authorization",
    "receipts",
    "skip_reason",
    "block_reason",
}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def is_canonical_project_id(name: str) -> bool:
    """Report whether a project directory name is a canonical YYYY-MM-DD-NNN identifier."""
    return bool(PROJECT_ID_PATTERN.match(name))


def resolve_workspace_root(explicit: "Path | None") -> Path:
    """Resolve the workspace root from an explicit path, else the environment.

    Deliberately has no third fallback: inferring a root from the current working directory is what
    produced two workspaces holding divergent copies of the same project. When neither source is
    present the caller must ask.
    """
    if explicit is not None:
        return explicit.expanduser()
    configured = os.environ.get(WORKSPACE_ROOT_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    raise WorkspaceError(
        "no workspace root: pass it as an argument or set "
        f"{WORKSPACE_ROOT_ENV_VAR}; the current working directory is never assumed"
    )


class WorkspaceError(RuntimeError):
    """Base class for actionable workspace errors."""


class WorkspaceConflict(WorkspaceError):
    """Raised when a lock or revision prevents a safe write."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _non_empty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _enum_string(value: object, choices: set[str]) -> TypeGuard[str]:
    return isinstance(value, str) and value in choices


def _is_timestamp(value: object) -> bool:
    if not _non_empty_string(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def is_external_reference(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        # urllib rejects malformed bracketed hosts (for example ``http://[``) by raising rather
        # than returning a parse result. References are untrusted workspace data, so malformed
        # URLs are validation failures, not exceptions that may escape the CLI.
        return False
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)
    return bool(re.fullmatch(r"(?:receipt|deployment|message|purchase|publish):\S+", value))


def read_text(path: Path) -> str:
    """Read UTF-8 text, turning both I/O and decode failures into WorkspaceError.

    `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so a file holding invalid UTF-8
    bytes slips straight through an `except OSError` guard. Every text read in this module goes
    through here so a corrupt file is reported, not raised as a traceback at the caller.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkspaceError(f"cannot read {path}: {error}") from error


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{path} must contain a JSON object")
    return raw


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except UnicodeEncodeError as error:
        temporary_path.unlink(missing_ok=True)
        raise WorkspaceError(f"cannot write {path} as UTF-8: {error}") from error
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


class DirectoryLock(AbstractContextManager["DirectoryLock"]):
    """A cross-process lock based on atomic directory creation."""

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self.acquired = False

    def __enter__(self) -> "DirectoryLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.path.mkdir()
                self.acquired = True
                owner = {
                    "pid": os.getpid(),
                    "created": now_iso(),
                }
                try:
                    atomic_write_json(self.path / "owner.json", owner)
                except BaseException:
                    self.acquired = False
                    self.path.rmdir()
                    raise
                return self
            except FileExistsError as error:
                if time.monotonic() >= deadline:
                    raise WorkspaceConflict(
                        f"workspace lock is busy: {self.path}; inspect owner.json before removing a stale lock"
                    ) from error
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.acquired:
            (self.path / "owner.json").unlink(missing_ok=True)
            try:
                self.path.rmdir()
            except OSError as error:
                # Never mask an exception raised inside the `with` body: that error is what
                # the caller needs to see, and it is often the reason the lock directory is
                # not empty in the first place.
                if exc_type is None:
                    raise WorkspaceError(f"cannot release workspace lock {self.path}: {error}") from error
                return
            self.acquired = False


def _unexpected_fields(value: dict[str, Any], allowed: set[str], label: str, report: ValidationReport) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        report.errors.append(f"{label}: unexpected fields: {', '.join(extras)}")


def _missing_fields(value: dict[str, Any], required: set[str], label: str, report: ValidationReport) -> None:
    missing = sorted(required - set(value))
    if missing:
        report.errors.append(f"{label}: missing fields: {', '.join(missing)}")


def _resolve_local_reference(
    project_dir: Path, working_directory: Path, root: str, path: str
) -> tuple[Path | None, str | None]:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None, "local reference paths must be relative and cannot contain '..'"
    base = project_dir if root == "workspace" else working_directory
    try:
        resolved_base = base.resolve()
        resolved = (base / relative).resolve()
        resolved.relative_to(resolved_base)
    except (OSError, ValueError):
        return None, f"reference escapes its {root} root"
    return resolved, None


def _validate_output_reference(
    value: object,
    label: str,
    project_dir: Path,
    working_directory: Path,
    require_exists: bool,
    report: ValidationReport,
) -> None:
    if not isinstance(value, dict):
        report.errors.append(f"{label}: output must be an object")
        return
    _missing_fields(value, {"root", "path", "required"}, label, report)
    _unexpected_fields(value, {"root", "path", "required"}, label, report)
    root = value.get("root")
    path = value.get("path")
    required = value.get("required")
    if not _enum_string(root, REFERENCE_ROOTS):
        report.errors.append(f"{label}: invalid root {root!r}")
        return
    if not _non_empty_string(path):
        report.errors.append(f"{label}: path must be a non-empty string")
        return
    if not isinstance(required, bool):
        report.errors.append(f"{label}: required must be a boolean")
        return
    if root == "external":
        if not is_external_reference(path):
            report.errors.append(f"{label}: invalid external reference {path!r}")
        return
    resolved, error = _resolve_local_reference(project_dir, working_directory, root, path)
    if error:
        report.errors.append(f"{label}: {error}: {path}")
    elif require_exists and required and resolved is not None and not resolved.exists():
        report.errors.append(f"{label}: required output does not exist: {root}:{path}")


def _validate_evidence_reference(
    value: object,
    label: str,
    project_dir: Path,
    working_directory: Path,
    require_exists: bool,
    report: ValidationReport,
) -> None:
    if not isinstance(value, dict):
        report.errors.append(f"{label}: evidence must be an object")
        return
    _missing_fields(value, {"root", "path", "anchor"}, label, report)
    _unexpected_fields(value, {"root", "path", "anchor"}, label, report)
    root = value.get("root")
    path = value.get("path")
    anchor = value.get("anchor")
    if not _enum_string(root, REFERENCE_ROOTS):
        report.errors.append(f"{label}: invalid root {root!r}")
        return
    if not _non_empty_string(path):
        report.errors.append(f"{label}: path must be a non-empty string")
        return
    if anchor is not None and not _non_empty_string(anchor):
        report.errors.append(f"{label}: anchor must be null or a non-empty string")
    if root == "external":
        if not is_external_reference(path):
            report.errors.append(f"{label}: invalid external evidence reference {path!r}")
        return
    resolved, error = _resolve_local_reference(project_dir, working_directory, root, path)
    if error:
        report.errors.append(f"{label}: {error}: {path}")
    elif require_exists and resolved is not None and not resolved.is_file():
        report.errors.append(f"{label}: evidence file does not exist: {root}:{path}")


def _validate_effect(value: object, label: str, report: ValidationReport) -> str | None:
    if not isinstance(value, dict):
        report.errors.append(f"{label}: effect must be an object")
        return None
    _missing_fields(value, {"kind", "description"}, label, report)
    _unexpected_fields(value, {"kind", "description"}, label, report)
    kind = value.get("kind")
    description = value.get("description")
    if not _enum_string(kind, EFFECT_KINDS):
        report.errors.append(f"{label}: invalid effect kind {kind!r}")
        return None
    if kind != "none" and not _non_empty_string(description):
        report.errors.append(f"{label}: non-none effects require a description")
    if kind == "none" and description is not None:
        report.errors.append(f"{label}: a none effect must have a null description")
    return kind


def _validate_authorization(
    value: object,
    label: str,
    effect_kind: str | None,
    task_status: str | None,
    report: ValidationReport,
) -> None:
    if not isinstance(value, dict):
        report.errors.append(f"{label}: authorization must be an object")
        return
    fields = {"required", "status", "scope", "source", "authorized_at"}
    _missing_fields(value, fields, label, report)
    _unexpected_fields(value, fields, label, report)
    required = value.get("required")
    status = value.get("status")
    if not isinstance(required, bool):
        report.errors.append(f"{label}: required must be a boolean")
        return
    if not _enum_string(status, AUTHORIZATION_STATUSES):
        report.errors.append(f"{label}: invalid status {status!r}")
        return
    if effect_kind in {"destructive", "external"} and not required:
        report.errors.append(f"{label}: {effect_kind} effects must require authorization")
    if not required and status != "not_required":
        report.errors.append(f"{label}: non-required authorization must use status 'not_required'")
    if required and status == "not_required":
        report.errors.append(f"{label}: required authorization cannot use status 'not_required'")
    if status == "explicit":
        for field_name in ("scope", "source"):
            if not _non_empty_string(value.get(field_name)):
                report.errors.append(f"{label}: explicit authorization requires {field_name}")
        if not _is_timestamp(value.get("authorized_at")):
            report.errors.append(f"{label}: explicit authorization requires a timezone-aware authorized_at")
    elif any(value.get(field_name) is not None for field_name in ("source", "authorized_at")):
        report.errors.append(f"{label}: source and authorized_at must be null unless status is explicit")
    if isinstance(task_status, str) and task_status in {"RUNNING", "DONE"} and required and status != "explicit":
        report.errors.append(f"{label}: {task_status} task requires explicit authorization")


def _validate_receipt(value: object, label: str, report: ValidationReport) -> None:
    if not isinstance(value, dict):
        report.errors.append(f"{label}: receipt must be an object")
        return
    fields = {"kind", "value", "destination", "timestamp"}
    _missing_fields(value, fields, label, report)
    _unexpected_fields(value, fields, label, report)
    for field_name in ("kind", "value", "destination"):
        if not _non_empty_string(value.get(field_name)):
            report.errors.append(f"{label}: {field_name} must be a non-empty string")
    receipt_value = value.get("value")
    if _non_empty_string(receipt_value) and not is_external_reference(receipt_value):
        report.errors.append(f"{label}: value must be a valid URL or a durable prefixed identifier")
    if not _is_timestamp(value.get("timestamp")):
        report.errors.append(f"{label}: timestamp must be timezone-aware ISO-8601")


def _check_dependencies(tasks_by_id: dict[str, dict[str, Any]], report: ValidationReport) -> None:
    graph: dict[str, list[str]] = {}
    for task_id, task in tasks_by_id.items():
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or not all(_non_empty_string(item) for item in dependencies):
            report.errors.append(f"task {task_id}: depends_on must be a list of non-empty task IDs")
            dependencies = []
        elif len(set(dependencies)) != len(dependencies):
            report.errors.append(f"task {task_id}: depends_on contains duplicates")
        graph[task_id] = dependencies
        for dependency in dependencies:
            if dependency not in tasks_by_id:
                report.errors.append(f"task {task_id}: unknown dependency {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, path: list[str]) -> None:
        if task_id in visiting:
            cycle_start = path.index(task_id)
            report.errors.append(f"dependency cycle: {' -> '.join(path[cycle_start:])}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph.get(task_id, []):
            if dependency in graph:
                visit(dependency, [*path, dependency])
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id, [task_id])

    for task_id, task in tasks_by_id.items():
        if not _enum_string(task.get("status"), {"RUNNING", "DONE"}):
            continue
        for dependency in graph.get(task_id, []):
            dependency_status = tasks_by_id.get(dependency, {}).get("status")
            if dependency_status != "DONE":
                report.errors.append(
                    f"task {task_id}: {task.get('status')} task has unsatisfied dependency "
                    f"{dependency} ({dependency_status})"
                )


def _read_nonempty(path: Path, label: str, report: ValidationReport) -> str:
    try:
        content = read_text(path)
    except WorkspaceError as error:
        report.errors.append(f"cannot read {label}: {error}")
        return ""
    if not content.strip():
        report.errors.append(f"{label} must not be empty")
    return content


def _section_content(markdown: str, heading: str) -> str | None:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(markdown)
    if not match:
        return None
    remainder = markdown[match.end() :]
    next_heading = re.search(r"^##\s+", remainder, re.MULTILINE)
    return remainder[: next_heading.start() if next_heading else None].strip()


def validate_v3_state(
    state: dict[str, Any],
    project_dir: Path,
    *,
    close: bool = False,
    check_files: bool = True,
) -> ValidationReport:
    report = ValidationReport()
    _missing_fields(state, PROJECT_FIELDS - {"predecessor"}, "project", report)
    _unexpected_fields(state, PROJECT_FIELDS, "project", report)

    if state.get("schema_version") != 3:
        report.errors.append("schema_version must be 3")
    for field_name in ("project", "title", "working_directory"):
        if not _non_empty_string(state.get(field_name)):
            report.errors.append(f"project: {field_name} must be a non-empty string")
    if _non_empty_string(state.get("project")) and state.get("project") != project_dir.name:
        report.errors.append("project field must match the project directory name")
    if not is_canonical_project_id(project_dir.name):
        report.warnings.append(
            f"project directory name is not a canonical YYYY-MM-DD-NNN identifier: {project_dir.name}"
        )
    if "predecessor" in state and not _non_empty_string(state.get("predecessor")):
        report.errors.append("project: predecessor must be a non-empty project ID when present")
    for field_name in ("created", "updated"):
        if not _is_timestamp(state.get(field_name)):
            report.errors.append(f"project: {field_name} must be timezone-aware ISO-8601")
    if (
        not isinstance(state.get("revision"), int)
        or isinstance(state.get("revision"), bool)
        or state.get("revision", -1) < 0
    ):
        report.errors.append("project: revision must be a non-negative integer")

    status = state.get("status")
    if not _enum_string(status, PROJECT_STATUSES):
        report.errors.append(f"project: invalid status {status!r}")

    working_directory_value = state.get("working_directory")
    working_directory = Path(working_directory_value) if _non_empty_string(working_directory_value) else project_dir
    if not working_directory.is_absolute():
        report.errors.append("project: working_directory must be absolute")
    elif check_files and not working_directory.is_dir():
        report.errors.append(f"project: working_directory does not exist: {working_directory}")

    current_tasks = state.get("current_tasks")
    if not isinstance(current_tasks, list) or not all(_non_empty_string(item) for item in current_tasks):
        report.errors.append("project: current_tasks must be a list of non-empty task IDs")
        current_tasks = []
    elif len(set(current_tasks)) != len(current_tasks):
        report.errors.append("project: current_tasks contains duplicates")

    review = state.get("review")
    if not isinstance(review, dict):
        report.errors.append("project: review must be an object")
        review = {}
    review_fields = {"cycle", "required", "status", "evidence"}
    _missing_fields(review, review_fields, "review", report)
    _unexpected_fields(review, review_fields, "review", report)
    review_cycle = review.get("cycle")
    if not isinstance(review_cycle, int) or isinstance(review_cycle, bool) or review_cycle < 0:
        report.errors.append("review: cycle must be a non-negative integer")
        review_cycle = 0
    review_required = review.get("required")
    if not isinstance(review_required, bool):
        report.errors.append("review: required must be a boolean")
    review_status = review.get("status")
    if not _enum_string(review_status, REVIEW_STATUSES):
        report.errors.append(f"review: invalid status {review_status!r}")
    if review_required is True and review_status == "not_required":
        report.errors.append("review: required review cannot use status 'not_required'")
    review_evidence = review.get("evidence")
    if not isinstance(review_evidence, list):
        report.errors.append("review: evidence must be a list")
        review_evidence = []
    for index, item in enumerate(review_evidence, start=1):
        _validate_evidence_reference(
            item,
            f"review evidence #{index}",
            project_dir,
            working_directory,
            require_exists=check_files,
            report=report,
        )
    if _enum_string(review_status, {"accepted", "recorded"}) and (review_cycle < 1 or not review_evidence):
        report.errors.append(f"review: status {review_status!r} requires a cycle and evidence")
    if close and review_required is True and review_status != "accepted":
        report.errors.append("project cannot close before the required review is accepted")
    if close and review_status == "pending":
        report.errors.append("project cannot close with a pending review")

    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        report.errors.append("project: tasks must be a list")
        tasks = []
    if close and not tasks:
        report.errors.append("project cannot close without at least one task")

    tasks_by_id: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(tasks, start=1):
        label = f"task #{index}"
        if not isinstance(task, dict):
            report.errors.append(f"{label}: task must be an object")
            continue
        _missing_fields(task, TASK_FIELDS, label, report)
        _unexpected_fields(task, TASK_FIELDS, label, report)
        task_id = task.get("id")
        if not _non_empty_string(task_id):
            report.errors.append(f"{label}: id must be a non-empty string")
            continue
        if task_id in tasks_by_id:
            report.errors.append(f"duplicate task ID: {task_id}")
            continue
        tasks_by_id[task_id] = task

    _check_dependencies(tasks_by_id, report)

    for task_id, task in tasks_by_id.items():
        label = f"task {task_id}"
        task_status = task.get("status")
        if not _enum_string(task_status, TASK_STATUSES):
            report.errors.append(f"{label}: invalid status {task_status!r}")
            task_status = None
        for field_name in ("name", "success_criteria", "verification"):
            if not _non_empty_string(task.get(field_name)):
                report.errors.append(f"{label}: {field_name} must be a non-empty string")

        outputs = task.get("outputs")
        if not isinstance(outputs, list):
            report.errors.append(f"{label}: outputs must be a list")
            outputs = []
        for index, output in enumerate(outputs, start=1):
            _validate_output_reference(
                output,
                f"{label} output #{index}",
                project_dir,
                working_directory,
                require_exists=check_files and task_status == "DONE",
                report=report,
            )

        evidence = task.get("evidence")
        if not isinstance(evidence, list):
            report.errors.append(f"{label}: evidence must be a list")
            evidence = []
        for index, item in enumerate(evidence, start=1):
            _validate_evidence_reference(
                item,
                f"{label} evidence #{index}",
                project_dir,
                working_directory,
                require_exists=check_files and task_status == "DONE",
                report=report,
            )
        if task_status == "DONE" and not evidence:
            report.errors.append(f"{label}: DONE task requires evidence")

        effect_kind = _validate_effect(task.get("effect"), label, report)
        _validate_authorization(task.get("authorization"), label, effect_kind, task_status, report)

        receipts = task.get("receipts")
        if not isinstance(receipts, list):
            report.errors.append(f"{label}: receipts must be a list")
            receipts = []
        for index, receipt in enumerate(receipts, start=1):
            _validate_receipt(receipt, f"{label} receipt #{index}", report)
        if task_status == "DONE" and effect_kind == "external" and not receipts:
            report.errors.append(f"{label}: completed external effect requires a durable receipt")

        skip_reason = task.get("skip_reason")
        block_reason = task.get("block_reason")
        if task_status == "SKIPPED" and not _non_empty_string(skip_reason):
            report.errors.append(f"{label}: SKIPPED task requires skip_reason")
        elif task_status != "SKIPPED" and skip_reason is not None:
            report.errors.append(f"{label}: skip_reason must be null unless task is SKIPPED")
        if task_status == "BLOCKED" and not _non_empty_string(block_reason):
            report.errors.append(f"{label}: BLOCKED task requires block_reason")
        elif task_status != "BLOCKED" and block_reason is not None:
            report.errors.append(f"{label}: block_reason must be null unless task is BLOCKED")

    running_ids = sorted(task_id for task_id, task in tasks_by_id.items() if task.get("status") == "RUNNING")
    recorded_running = sorted(current_tasks) if all(isinstance(item, str) for item in current_tasks) else []
    if running_ids != recorded_running:
        report.errors.append(f"current_tasks {recorded_running!r} does not match RUNNING tasks {running_ids!r}")
    unknown_current = sorted(set(recorded_running) - set(tasks_by_id))
    if unknown_current:
        report.errors.append(f"current_tasks references unknown tasks: {', '.join(unknown_current)}")

    cancellation_reason = state.get("cancellation_reason")
    if status == "CANCELLED":
        if not _non_empty_string(cancellation_reason):
            report.errors.append("CANCELLED project requires cancellation_reason")
        if running_ids:
            report.errors.append("CANCELLED project cannot have RUNNING tasks")
    elif cancellation_reason is not None:
        report.errors.append("cancellation_reason must be null unless project is CANCELLED")
    if _enum_string(status, {"ALIGNING", "PLANNING", "REVIEW", "BLOCKED", "DONE"}) and running_ids:
        report.errors.append(f"{status} project cannot have RUNNING tasks")
    if status == "BLOCKED" and not any(task.get("status") == "BLOCKED" for task in tasks_by_id.values()):
        report.errors.append("BLOCKED project must contain at least one BLOCKED task")

    if close or status == "DONE":
        incomplete = sorted(
            task_id
            for task_id, task in tasks_by_id.items()
            if not _enum_string(task.get("status"), TERMINAL_TASK_STATUSES)
        )
        if incomplete:
            report.errors.append(f"project cannot close with non-terminal tasks: {', '.join(incomplete)}")
        if check_files:
            spec = _read_nonempty(project_dir / "spec.md", "spec.md", report)
            for section in ("Current specification", "Decision history"):
                content = _section_content(spec, section)
                if content is None:
                    report.errors.append(f"spec.md is missing '## {section}'")
                elif not content:
                    report.errors.append(f"spec.md section '## {section}' must not be empty")
            _read_nonempty(project_dir / "evidence.md", "evidence.md", report)
            _read_nonempty(project_dir / "reflection.md", "reflection.md", report)
            for cycle in range(1, review_cycle + 1):
                _read_nonempty(
                    project_dir / "reviews" / f"review_{cycle:02d}.md", f"reviews/review_{cycle:02d}.md", report
                )

    return report


def validate_v2_state(
    state: dict[str, Any], project_dir: Path, *, close: bool = False, check_files: bool = True
) -> ValidationReport:
    """Validate the documented v2 shape without claiming v3 guarantees."""
    report = ValidationReport(
        warnings=["schema v2 has limited concurrency and authorization guarantees; migrate to v3"]
    )
    required_project_fields = {
        "schema_version",
        "project",
        "title",
        "status",
        "created",
        "updated",
        "working_directory",
        "current_task",
        "review_cycle",
        "tasks",
    }
    _missing_fields(state, required_project_fields, "project", report)
    if state.get("schema_version") != 2:
        report.errors.append("schema_version must be 2")
    for field_name in ("project", "title", "created", "updated", "working_directory"):
        if not _non_empty_string(state.get(field_name)):
            report.errors.append(f"project: {field_name} must be a non-empty string")
    if not _enum_string(state.get("status"), PROJECT_STATUSES):
        report.errors.append(f"project: invalid status {state.get('status')!r}")
    if (
        not isinstance(state.get("review_cycle"), int)
        or isinstance(state.get("review_cycle"), bool)
        or state.get("review_cycle", -1) < 0
    ):
        report.errors.append("project: review_cycle must be a non-negative integer")

    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        report.errors.append("project: tasks must be a list")
        tasks = []
    tasks_by_id: dict[str, dict[str, Any]] = {}
    required_task_fields = {
        "id",
        "name",
        "status",
        "depends_on",
        "outputs",
        "success_criteria",
        "verification",
        "evidence",
        "external_effect",
        "authorization",
        "skip_reason",
    }
    for index, task in enumerate(tasks, start=1):
        label = f"task #{index}"
        if not isinstance(task, dict):
            report.errors.append(f"{label}: task must be an object")
            continue
        _missing_fields(task, required_task_fields, label, report)
        task_id = task.get("id")
        if not _non_empty_string(task_id):
            report.errors.append(f"{label}: id must be a non-empty string")
            continue
        if task_id in tasks_by_id:
            report.errors.append(f"duplicate task ID: {task_id}")
            continue
        tasks_by_id[task_id] = task
        if not _enum_string(task.get("status"), TASK_STATUSES):
            report.errors.append(f"task {task_id}: invalid status {task.get('status')!r}")
        if not isinstance(task.get("external_effect"), bool):
            report.errors.append(f"task {task_id}: external_effect must be a boolean")
        for field_name in ("name", "success_criteria", "verification"):
            if not _non_empty_string(task.get(field_name)):
                report.errors.append(f"task {task_id}: {field_name} must be a non-empty string")
        for field_name in ("depends_on", "outputs", "evidence"):
            value = task.get(field_name)
            if not isinstance(value, list) or not all(_non_empty_string(item) for item in value):
                report.errors.append(f"task {task_id}: {field_name} must be a list of non-empty strings")
        if task.get("status") == "DONE":
            if not task.get("evidence"):
                report.errors.append(f"task {task_id}: DONE task requires evidence")
            if task.get("external_effect") is True and task.get("authorization") != "explicit":
                report.errors.append(f"task {task_id}: external effect lacks explicit authorization")
        if task.get("status") == "SKIPPED" and not _non_empty_string(task.get("skip_reason")):
            report.errors.append(f"task {task_id}: SKIPPED task requires skip_reason")

    _check_dependencies(tasks_by_id, report)
    current = state.get("current_task")
    if current is None:
        recorded_running: list[str] = []
    elif isinstance(current, str):
        recorded_running = [current]
    elif isinstance(current, list) and all(_non_empty_string(item) for item in current):
        recorded_running = list(current)
    else:
        report.errors.append("project: current_task must be null, a task ID, or a list of task IDs")
        recorded_running = []
    running_ids = sorted(task_id for task_id, task in tasks_by_id.items() if task.get("status") == "RUNNING")
    if sorted(recorded_running) != running_ids:
        report.errors.append(f"current_task {sorted(recorded_running)!r} does not match RUNNING tasks {running_ids!r}")

    if close or state.get("status") == "DONE":
        incomplete = sorted(
            task_id
            for task_id, task in tasks_by_id.items()
            if not _enum_string(task.get("status"), TERMINAL_TASK_STATUSES)
        )
        if incomplete:
            report.errors.append(f"project cannot close with non-terminal tasks: {', '.join(incomplete)}")
        if check_files:
            for filename in ("spec.md", "evidence.md", "reflection.md"):
                _read_nonempty(project_dir / filename, filename, report)
            working_directory_value = state.get("working_directory")
            working_directory = (
                Path(working_directory_value) if isinstance(working_directory_value, str) else project_dir
            )
            for task_id, task in tasks_by_id.items():
                if task.get("status") != "DONE":
                    continue
                outputs = task.get("outputs")
                if not isinstance(outputs, list) or not all(isinstance(output, str) for output in outputs):
                    continue
                for output in outputs:
                    if is_external_reference(output):
                        continue
                    output_path = Path(output).expanduser()
                    if output_path.is_absolute():
                        candidates = [output_path]
                    else:
                        candidates = [project_dir / output_path, working_directory / output_path]
                    if not any(candidate.exists() for candidate in candidates):
                        report.errors.append(f"task {task_id}: output does not exist: {output}")
    return report


def _has_readable_content(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return bool(read_text(path).strip())
    except WorkspaceError:
        return False


def validate_legacy_v1(project_dir: Path, *, close: bool, allow_legacy_close: bool) -> ValidationReport:
    report = ValidationReport(
        warnings=["legacy schema v1 has no canonical machine-readable task state; validation is necessarily limited"]
    )
    required = [project_dir / "00_meta.yaml", project_dir / "02_task_plan.md"]
    for path in required:
        if not _has_readable_content(path):
            report.errors.append(f"legacy workspace is missing readable content: {path.name}")
    if close and not allow_legacy_close:
        report.errors.append(
            "legacy v1 closure requires either explicit migration to v3 or --allow-legacy-close "
            "to acknowledge limited validation"
        )
    if close and allow_legacy_close:
        reflection = project_dir / "reflection.md"
        if not _has_readable_content(reflection):
            report.errors.append("legacy closure requires a non-empty, readable reflection.md")
    return report


def detect_schema(project_dir: Path) -> int:
    state_path = project_dir / "project.json"
    if state_path.is_file():
        state = load_json(state_path)
        version = state.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise WorkspaceError("project.json has no integer schema_version")
        return version
    if (project_dir / "00_meta.yaml").is_file() and (project_dir / "02_task_plan.md").is_file():
        return 1
    raise WorkspaceError(f"cannot detect workspace schema in {project_dir}")


def validate_project(
    project_dir: Path,
    *,
    close: bool = False,
    check_index: bool = False,
    allow_legacy_close: bool = False,
) -> ValidationReport:
    project_dir = project_dir.resolve()
    try:
        version = detect_schema(project_dir)
    except WorkspaceError as error:
        return ValidationReport(errors=[str(error)])
    try:
        if version == 1:
            report = validate_legacy_v1(project_dir, close=close, allow_legacy_close=allow_legacy_close)
        elif version == 2:
            report = validate_v2_state(load_json(project_dir / "project.json"), project_dir, close=close)
        elif version == 3:
            report = validate_v3_state(load_json(project_dir / "project.json"), project_dir, close=close)
        else:
            report = ValidationReport(errors=[f"unsupported schema_version: {version}"])
    except WorkspaceError as error:
        return ValidationReport(errors=[str(error)])
    if check_index and version in {2, 3}:
        expected = render_index(project_dir.parent)
        index_path = project_dir.parent / "INDEX.md"
        try:
            actual = read_text(index_path) if index_path.is_file() else ""
        except WorkspaceError as error:
            report.errors.append(f"derived index is unreadable: {error}")
        else:
            if actual != expected:
                report.errors.append(f"derived index is stale: {index_path}")
    return report


def _escape_table(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _legacy_title(project_dir: Path) -> str:
    plan_path = project_dir / "02_task_plan.md"
    try:
        first_heading = next(
            line.lstrip("# ").strip() for line in read_text(plan_path).splitlines() if line.startswith("#")
        )
    except (WorkspaceError, StopIteration):
        return project_dir.name
    return first_heading.removeprefix("Task Plan — ").removeprefix("Task Plan - ")


def render_index(workspace_root: Path) -> str:
    rows: list[tuple[str, str, str, str, str]] = []
    if workspace_root.is_dir():
        for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            state_path = child / "project.json"
            if state_path.is_file():
                try:
                    state = load_json(state_path)
                    rows.append(
                        (
                            state.get("project", child.name),
                            state.get("title", "Untitled"),
                            state.get("status", "INVALID"),
                            state.get("updated", ""),
                            state.get("working_directory", ""),
                        )
                    )
                except WorkspaceError:
                    rows.append((child.name, "Unreadable project.json", "INVALID", "", ""))
            elif (child / "00_meta.yaml").is_file() and (child / "02_task_plan.md").is_file():
                rows.append((child.name, _legacy_title(child), "LEGACY", "", str(child)))
    header = (
        "# Agentic workspace projects\n\n"
        "<!-- Generated by manage_workspace.py; do not edit manually. -->\n\n"
        "| Project | Title | Status | Updated | Working directory |\n"
        "|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {_escape_table(project)} | {_escape_table(title)} | {_escape_table(status)} | "
        f"{_escape_table(updated)} | {_escape_table(working_directory)} |\n"
        for project, title, status, updated, working_directory in rows
    )
    return header + body


def _rebuild_index_after_commit(workspace_root: Path, committed: str, lock_timeout: float) -> None:
    """Rebuild the index, making clear that a failure here did not undo the committed write.

    The index rebuild happens after the state file lands and takes a *workspace*-wide lock, so a
    concurrent rebuild is enough to fail it. Reporting that as a bare lock conflict made a
    committed migration look like a failed one, and the obvious retry then refused because the
    project was already v3. Say what actually happened and name the command that finishes it.

    Filesystem failures arrive as WorkspaceError because rebuild_index normalises them at the
    source; that matters most here, after a commit, where a bare OSError traceback out of the CLI
    would invite a retry of work that already landed.
    """
    try:
        rebuild_index(workspace_root, lock_timeout=lock_timeout)
    except WorkspaceError as error:
        raise WorkspaceError(
            f"{committed}, but INDEX.md was not rebuilt: {error}\n"
            f"nothing is lost and nothing needs redoing: finish with "
            f"'manage_workspace.py rebuild-index {workspace_root}'"
        ) from error


def rebuild_index(workspace_root: Path, *, lock_timeout: float = 5.0) -> Path:
    # Filesystem failures are normalised here rather than at the call sites, because the CLIs
    # translate WorkspaceError alone and this is both the step every commit ends with and the
    # command the post-commit error tells the operator to run. A read-only workspace root fails in
    # the lock's own mkdir, before any write, so the whole body is covered, not just the write.
    try:
        workspace_root = workspace_root.resolve()
        index_path = workspace_root / "INDEX.md"
        workspace_root.mkdir(parents=True, exist_ok=True)
        with DirectoryLock(workspace_root / ".index.lock", timeout=lock_timeout):
            atomic_write_text(index_path, render_index(workspace_root))
        return index_path
    except OSError as error:
        raise WorkspaceError(f"cannot rebuild {workspace_root / 'INDEX.md'}: {error}") from error


def _task_index(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a state's tasks by id, skipping anything that is not a task with a usable id.

    `candidate` reaches check_state_transition *before* validate_v3_state has vetted it, so its
    task ids are arbitrary JSON. Keying a dict on them directly crashed on an unhashable id
    (`"id": []` raised TypeError), and the CLIs only translate WorkspaceError — so the user saw
    a traceback instead of a validation error. A task with no usable id has no counterpart to
    compare against anyway; skip it here and let validation report the real problem.
    """
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        return {}
    return {task["id"]: task for task in tasks if isinstance(task, dict) and _non_empty_string(task.get("id"))}


def check_state_transition(previous: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    previous_status = previous.get("status")
    candidate_status = candidate.get("status")
    if (
        isinstance(previous_status, str)
        and previous_status in PROJECT_TRANSITIONS
        and not _enum_string(candidate_status, PROJECT_TRANSITIONS[previous_status])
    ):
        errors.append(f"invalid project transition: {previous_status} -> {candidate_status}")
    previous_tasks = _task_index(previous)
    candidate_tasks = _task_index(candidate)
    removed = sorted(task_id for task_id in previous_tasks if task_id not in candidate_tasks)
    if removed:
        errors.append(f"tasks cannot be removed: {', '.join(removed)}")
    for task_id, old_task in previous_tasks.items():
        new_task = candidate_tasks.get(task_id)
        if new_task is None:
            continue
        old_status = old_task.get("status")
        new_status = new_task.get("status")
        if (
            isinstance(old_status, str)
            and old_status in TASK_TRANSITIONS
            and not _enum_string(new_status, TASK_TRANSITIONS[old_status])
        ):
            errors.append(f"task {task_id}: invalid transition {old_status} -> {new_status}")
        if isinstance(old_status, str) and old_status in TERMINAL_TASK_STATUSES and old_task != new_task:
            errors.append(f"task {task_id}: terminal task history is immutable; append a correction task instead")
    return errors


def commit_candidate(
    project_dir: Path,
    candidate_path: Path,
    *,
    expected_revision: int,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    candidate = load_json(candidate_path.resolve())
    with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
        current = load_json(project_dir / "project.json")
        if current.get("schema_version") != 3:
            raise WorkspaceError("transactional commits require schema v3; migrate the project first")
        if current.get("revision") != expected_revision:
            raise WorkspaceConflict(
                f"revision conflict: expected {expected_revision}, found {current.get('revision')}; reload and reconcile"
            )
        if candidate.get("revision") != expected_revision:
            raise WorkspaceConflict(
                f"candidate was built from revision {candidate.get('revision')}, expected {expected_revision}; "
                "reload and reconcile"
            )
        for immutable in ("schema_version", "project", "created"):
            if candidate.get(immutable) != current.get(immutable):
                raise WorkspaceError(f"candidate cannot change immutable field: {immutable}")
        transition_errors = check_state_transition(current, candidate)
        if transition_errors:
            raise WorkspaceError("; ".join(transition_errors))
        candidate["revision"] = expected_revision + 1
        candidate["updated"] = now_iso()
        report = validate_v3_state(
            candidate,
            project_dir,
            close=candidate.get("status") == "DONE",
            check_files=True,
        )
        if report.errors:
            raise WorkspaceError("candidate validation failed:\n- " + "\n- ".join(report.errors))
        atomic_write_json(project_dir / "project.json", candidate)
    # The commit already landed; a failed index rebuild must not read as a failed commit, or the
    # retry reloads and reports a revision conflict against the write that actually succeeded.
    _rebuild_index_after_commit(project_dir.parent, f"revision {candidate['revision']} is committed", lock_timeout)
    return candidate


def allocate_project(
    workspace_root: Path,
    *,
    title: str,
    working_directory: Path,
    lock_timeout: float = 5.0,
    create_root: bool = False,
) -> Path:
    workspace_root = workspace_root.expanduser()
    if not _non_empty_string(title):
        raise WorkspaceError("title must be non-empty")
    working_directory = working_directory.resolve()
    if not working_directory.is_dir():
        raise WorkspaceError(f"working directory does not exist: {working_directory}")
    # Creating the root on demand is how a typo, or a guess, silently becomes a second workspace
    # holding a divergent copy of a project that already exists elsewhere.
    if workspace_root.exists() and not workspace_root.is_dir():
        raise WorkspaceError(f"workspace root is not a directory: {workspace_root}")
    if not workspace_root.exists():
        if not create_root:
            raise WorkspaceError(
                f"workspace root does not exist: {workspace_root}; pass --create-root to create it, "
                "or point at the existing workspace"
            )
        workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_root = workspace_root.resolve()
    date_prefix = datetime.now().astimezone().date().isoformat()
    sequence = 1
    while True:
        candidate_id = f"{date_prefix}-{sequence:03d}"
        if not is_canonical_project_id(candidate_id):
            raise WorkspaceError(f"refusing to create a non-canonical project ID: {candidate_id}")
        project_dir = workspace_root / candidate_id
        try:
            project_dir.mkdir()
            break
        except FileExistsError:
            sequence += 1
    try:
        for directory in ("tasks", "artifacts", "reviews"):
            (project_dir / directory).mkdir()
        timestamp = now_iso()
        state = {
            "schema_version": 3,
            "project": project_dir.name,
            "title": title.strip(),
            "status": "ALIGNING",
            "created": timestamp,
            "updated": timestamp,
            "working_directory": str(working_directory),
            "revision": 0,
            "current_tasks": [],
            "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
            "cancellation_reason": None,
            "tasks": [],
        }
        # project.json is the commit point: write the skeleton first so an interrupted init leaves
        # an inert directory that detect_schema does not recognise and render_index skips, rather
        # than a project that reports schema v3 while missing the files v3 requires.
        atomic_write_text(
            project_dir / "spec.md",
            f"# {title.strip()}\n\n## Current specification\n\nAlignment in progress.\n\n"
            "## Decision history\n\n- Project initialized; requirements pending alignment.\n",
        )
        atomic_write_text(project_dir / "evidence.md", "# Evidence\n\nNo task evidence recorded yet.\n")
        if not (workspace_root / "reflection.md").exists():
            atomic_write_text(workspace_root / "reflection.md", "# Cross-project reflection\n")
        atomic_write_json(project_dir / "project.json", state)
        _rebuild_index_after_commit(workspace_root, f"project {project_dir} is initialized", lock_timeout)
    except BaseException:
        # The newly allocated directory is private to this failed initialization. Leave it intact
        # for inspection instead of performing a potentially broad cleanup.
        raise
    return project_dir


def _reference_from_v2(
    value: str,
    project_dir: Path,
    working_directory: Path,
    *,
    evidence: bool,
) -> dict[str, Any]:
    path_value, _separator, anchor = value.partition("#") if evidence else (value, "", "")
    result: dict[str, Any]
    if is_external_reference(value):
        result = {"root": "external", "path": value}
    else:
        old_path = Path(path_value).expanduser()
        if old_path.is_absolute():
            resolved = old_path.resolve()
            try:
                relative = resolved.relative_to(project_dir.resolve())
                result = {"root": "workspace", "path": str(relative)}
            except ValueError:
                try:
                    relative = resolved.relative_to(working_directory.resolve())
                    result = {"root": "target", "path": str(relative)}
                except ValueError as error:
                    raise WorkspaceError(
                        f"cannot migrate output outside workspace and target roots: {value}"
                    ) from error
        else:
            workspace_candidate = project_dir / old_path
            target_candidate = working_directory / old_path
            if project_dir.resolve() == working_directory.resolve() or (
                workspace_candidate.exists() and not target_candidate.exists()
            ):
                root = "workspace"
            elif target_candidate.exists() and not workspace_candidate.exists():
                root = "target"
            elif workspace_candidate.exists() and target_candidate.exists():
                raise WorkspaceError(f"ambiguous v2 reference exists under both roots: {value}")
            elif path_value.startswith(("artifacts/", "tasks/", "reviews/")) or path_value in {
                "spec.md",
                "evidence.md",
                "reflection.md",
            }:
                root = "workspace"
            else:
                root = "target"
            result = {"root": root, "path": path_value}
    if evidence:
        result["anchor"] = anchor or None
    else:
        result["required"] = True
    return result


def migrate_v2_state(state: dict[str, Any], project_dir: Path) -> dict[str, Any]:
    working_directory = Path(state.get("working_directory", project_dir)).expanduser().resolve()
    migrated_tasks: list[dict[str, Any]] = []
    reconciled_running: list[Any] = []
    for task in state.get("tasks", []):
        if not isinstance(task, dict):
            raise WorkspaceError("cannot migrate non-object task")
        external = task.get("external_effect") is True
        # v2's `authorization: "explicit"` was a single coarse marker; v3 authorization is
        # per-action, carrying a scope, a source and a timestamp. Replaying that marker onto a
        # task whose external effect has NOT run yet would hand it pre-granted, action-shaped
        # consent for something nobody approved in v3 terms — and v3 validation would report no
        # problem at all. SKILL.md commits a task to RUNNING *before* performing its action, so
        # RUNNING says nothing about whether the effect happened; only DONE does, and v2 already
        # refused to call a task DONE without the marker. A RUNNING external task therefore parks
        # as BLOCKED for reconciliation: that avoids both fabricating consent and — because v3
        # requires explicit authorization on RUNNING tasks — making a valid v2 project
        # unmigratable, which is what leaving it RUNNING with `pending` would do.
        legacy_marker = task.get("authorization") == "explicit"
        completed = task.get("status") == "DONE"
        if not external:
            authorization_status = "not_required"
        elif legacy_marker and completed:
            authorization_status = "explicit"
        else:
            authorization_status = "pending"
        task_status = task.get("status")
        if external and task_status == "RUNNING":
            task_status = "BLOCKED"
            block_reason = (
                "Migrated from schema v2 while RUNNING: v2 recorded no per-action authorization, so "
                "confirm whether the external effect already ran, then re-authorize before resuming."
            )
            reconciled_running.append(task.get("id"))
        elif task_status == "BLOCKED":
            block_reason = "Migrated blocked task; reconcile the original blocker."
        else:
            block_reason = None
        external_outputs = [output for output in task.get("outputs", []) if is_external_reference(output)]
        has_local_outputs = any(not is_external_reference(output) for output in task.get("outputs", []))
        authorized_at = (
            state.get("updated") if authorization_status == "explicit" and _is_timestamp(state.get("updated")) else None
        )
        receipts = []
        if external and task.get("status") == "DONE":
            for output in external_outputs:
                receipts.append(
                    {
                        "kind": "legacy",
                        "value": output,
                        "destination": output,
                        "timestamp": authorized_at or now_iso(),
                    }
                )
        migrated_tasks.append(
            {
                "id": task.get("id"),
                "name": task.get("name"),
                "status": task_status,
                "depends_on": list(task.get("depends_on", [])),
                "outputs": [
                    _reference_from_v2(output, project_dir, working_directory, evidence=False)
                    for output in task.get("outputs", [])
                ],
                "success_criteria": task.get("success_criteria"),
                "verification": task.get("verification"),
                "evidence": [
                    _reference_from_v2(item, project_dir, working_directory, evidence=True)
                    for item in task.get("evidence", [])
                ],
                "effect": {
                    "kind": "external" if external else "local_write" if has_local_outputs else "none",
                    "description": task.get("name") if external or has_local_outputs else None,
                },
                "authorization": {
                    "required": external,
                    "status": authorization_status,
                    "scope": task.get("name") if authorization_status == "explicit" else None,
                    "source": "migrated v2 authorization record" if authorization_status == "explicit" else None,
                    "authorized_at": authorized_at,
                },
                "receipts": receipts,
                "skip_reason": task.get("skip_reason"),
                "block_reason": block_reason,
            }
        )
    review_cycle = state.get("review_cycle", 0)
    review_evidence = []
    if isinstance(review_cycle, int) and review_cycle > 0:
        review_evidence.append({"root": "workspace", "path": f"reviews/review_{review_cycle:02d}.md", "anchor": None})
    current = state.get("current_task")
    if current is None:
        current_tasks: list[str] = []
    elif isinstance(current, str):
        current_tasks = [current]
    elif isinstance(current, list) and all(isinstance(item, str) for item in current):
        current_tasks = current
    else:
        raise WorkspaceError("cannot migrate malformed current_task")
    # A task parked for authorization reconciliation is no longer RUNNING, and current_tasks must
    # match the RUNNING set exactly or v3 validation rejects the candidate.
    current_tasks = [task_id for task_id in current_tasks if task_id not in reconciled_running]
    created = state.get("created")
    if not _is_timestamp(created):
        try:
            created = datetime.fromisoformat(str(created)).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            created = now_iso()
    updated = state.get("updated") if _is_timestamp(state.get("updated")) else now_iso()
    return {
        "schema_version": 3,
        "project": state.get("project", project_dir.name),
        "title": state.get("title", project_dir.name),
        "status": state.get("status", "ALIGNING"),
        "created": created,
        "updated": updated,
        "working_directory": str(working_directory),
        "revision": 0,
        "current_tasks": current_tasks,
        "review": {
            "cycle": review_cycle,
            "required": False,
            "status": "recorded" if review_cycle else "not_required",
            "evidence": review_evidence,
        },
        "cancellation_reason": "Migrated cancelled project; original reason unavailable."
        if state.get("status") == "CANCELLED"
        else None,
        "tasks": migrated_tasks,
    }


def _legacy_value(meta: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", meta, re.MULTILINE)
    return match.group(1).strip(" \"'") if match else None


def migrate_v1_state(project_dir: Path) -> dict[str, Any]:
    meta = read_text(project_dir / "00_meta.yaml")
    plan = read_text(project_dir / "02_task_plan.md")
    title = _legacy_title(project_dir)
    task_matches = list(re.finditer(r"^##\s+Task\s+(\d+)\s+[—-]\s+(.+?)\s*$", plan, re.MULTILINE))
    tasks: list[dict[str, Any]] = []
    for index, match in enumerate(task_matches):
        section_start = match.end()
        section_end = task_matches[index + 1].start() if index + 1 < len(task_matches) else len(plan)
        section = plan[section_start:section_end]
        outputs_match = re.search(r"^-\s+\*\*Outputs:\*\*\s*(.+)$", section, re.MULTILINE)
        success_match = re.search(r"^-\s+\*\*Success criteria:\*\*\s*(.+)$", section, re.MULTILINE)
        output_values = re.findall(r"`([^`]+)`", outputs_match.group(1)) if outputs_match else []
        task_id = f"T{int(match.group(1)):02d}"
        tasks.append(
            {
                "id": task_id,
                "name": match.group(2).strip(),
                "status": "TODO",
                "depends_on": [],
                "outputs": [{"root": "workspace", "path": output, "required": True} for output in output_values],
                "success_criteria": success_match.group(1).strip()
                if success_match
                else "Reconcile legacy success criteria.",
                "verification": "Define verification after reconciling the legacy task state.",
                "evidence": [],
                "effect": {"kind": "none", "description": None},
                "authorization": {
                    "required": False,
                    "status": "not_required",
                    "scope": None,
                    "source": None,
                    "authorized_at": None,
                },
                "receipts": [],
                "skip_reason": None,
                "block_reason": None,
            }
        )
    created_value = _legacy_value(meta, "created")
    try:
        created = datetime.fromisoformat(created_value or "").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        created = now_iso()
    return {
        "schema_version": 3,
        "project": _legacy_value(meta, "project") or project_dir.name,
        "title": title,
        "status": "ALIGNING",
        "created": created,
        "updated": now_iso(),
        "working_directory": str(project_dir.resolve()),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": tasks,
    }


def migration_candidate(project_dir: Path) -> dict[str, Any]:
    version = detect_schema(project_dir)
    if version == 1:
        return migrate_v1_state(project_dir)
    if version == 2:
        state = load_json(project_dir / "project.json")
        source_report = validate_v2_state(state, project_dir, close=False, check_files=False)
        if source_report.errors:
            raise WorkspaceError("cannot migrate invalid v2 state:\n- " + "\n- ".join(source_report.errors))
        return migrate_v2_state(state, project_dir)
    if version == 3:
        raise WorkspaceError("project already uses schema v3")
    raise WorkspaceError(f"unsupported migration source schema: {version}")


def apply_migration(project_dir: Path, *, lock_timeout: float = 5.0) -> Path:
    project_dir = project_dir.resolve()
    state_path = project_dir / "project.json"
    with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
        version = detect_schema(project_dir)
        candidate = migration_candidate(project_dir)
        report = validate_v3_state(candidate, project_dir, close=candidate.get("status") == "DONE", check_files=True)
        if report.errors:
            raise WorkspaceError("migration candidate is not valid:\n- " + "\n- ".join(report.errors))
        # Order matters. Everything below is idempotent or existence-guarded, and the v3 state
        # file is written LAST: until that write lands, detect_schema still reports the old
        # version, so a failure part-way through leaves a project a plain re-run can migrate.
        # Writing the state first stranded the project at v3 without its v3 files, and the
        # retry then refused with "project already uses schema v3" — unrecoverable by hand.
        if version == 1 and not (project_dir / "spec.md").exists():
            problem_path = project_dir / "01_problem_statement.md"
            problem = (
                read_text(problem_path).strip() if problem_path.is_file() else "Reconcile the legacy problem statement."
            )
            atomic_write_text(
                project_dir / "spec.md",
                f"# {candidate['title']}\n\n## Current specification\n\n{problem}\n\n"
                "## Decision history\n\n- Migrated from schema v1; historical completion was not inferred.\n",
            )
        if not (project_dir / "evidence.md").exists():
            atomic_write_text(project_dir / "evidence.md", "# Evidence\n\nNo v3 evidence recorded yet.\n")
        (project_dir / "tasks").mkdir(exist_ok=True)
        (project_dir / "artifacts").mkdir(exist_ok=True)
        (project_dir / "reviews").mkdir(exist_ok=True)
        if version == 2:
            backup_path = project_dir / "project.v2.json"
            legacy_state = read_text(state_path)
            # A backup already holding this exact still-unmigrated state is our own interrupted
            # run, so a retry may proceed. Any other content is a file we must not clobber.
            if backup_path.exists() and read_text(backup_path) != legacy_state:
                raise WorkspaceError(f"migration backup already exists: {backup_path}")
            atomic_write_text(backup_path, legacy_state)
        atomic_write_json(state_path, candidate)
    _rebuild_index_after_commit(project_dir.parent, "migration is committed", lock_timeout)
    return state_path
