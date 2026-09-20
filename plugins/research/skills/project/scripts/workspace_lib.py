#!/usr/bin/env python3
"""Shared state, validation, locking, migration, and index helpers."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:  # TypeGuard is 3.10+; the scripts must still import on system python 3.9.
    from collections.abc import Sequence
    from typing import TypeGuard

PROJECT_STATUSES = {"ALIGNING", "PLANNING", "EXECUTING", "REVIEW", "BLOCKED", "DONE", "CANCELLED"}
TASK_STATUSES = {"TODO", "RUNNING", "DONE", "BLOCKED", "SKIPPED"}
TERMINAL_TASK_STATUSES = {"DONE", "SKIPPED"}
EFFECT_KINDS = {"none", "local_write", "destructive", "external"}
AUTHORIZATION_STATUSES = {"not_required", "pending", "explicit", "denied", "deferred"}
REVIEW_STATUSES = {"not_required", "pending", "accepted", "recorded"}
# "workspace" is the project directory and "workspace_root" is the directory holding every project.
# The second exists because shared records — the cross-project reflection, INDEX.md — belong to no
# single project, and declaring one of them as an output previously meant either lying about its root
# or leaving it undeclared.
REFERENCE_ROOTS = {"workspace", "workspace_root", "target", "external"}
# The set is closed on purpose: an invented or mistyped prefix must be caught rather than
# accepted, so widening it is a deliberate edit here and nowhere else.
# "commit" is here because landing a change is a delivery whose durable identifier is the commit
# SHA, and a plan that makes integration a task of its own had nowhere to record that.
EXTERNAL_IDENTIFIER_PREFIXES = ("receipt", "deployment", "message", "purchase", "publish", "commit")

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
PROJECT_V4_FIELDS = PROJECT_FIELDS | {"execution"}
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
TASK_V4_FIELDS = TASK_FIELDS | {"reads"}


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


ROOT_SEARCH_MAX_DEPTH = 4
# Directories that never hold a workspace root and are expensive to walk. Pruning them is what
# keeps a $HOME search a second rather than a minute.
ROOT_SEARCH_PRUNED_NAMES = frozenset(
    {
        "Applications",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "__pycache__",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)


def _is_workspace_root(candidate: Path) -> bool:
    """A root holds INDEX.md and at least one canonically named project directory.

    Both conditions matter. `INDEX.md` alone matches any documentation directory, and a
    date-named directory alone matches dated notes that were never a workspace.
    """
    try:
        if not (candidate / "INDEX.md").is_file():
            return False
        return any(child.is_dir() and is_canonical_project_id(child.name) for child in candidate.iterdir())
    except OSError:
        # A $HOME search crosses directories it cannot read; one of them must not end the search.
        # `is_file` itself raises here on Python 3.12, not only `iterdir`.
        return False


def find_workspace_roots(
    search_paths: "Sequence[Path] | None" = None,
    *,
    max_depth: int = ROOT_SEARCH_MAX_DEPTH,
) -> "list[Path]":
    """Search for established workspace roots, deepest-bounded and breadth-first.

    `resolve_workspace_root` deliberately refuses to guess, which leaves the caller asking the user
    for a path they have already used. This finds the established root instead. Offering a menu of
    plausible roots is itself a form of guessing, so the search reports what exists — including the
    fact that two roots exist, which is a defect to resolve and not a choice to make silently.
    """
    if search_paths is None:
        search_paths = [Path.home()]
    found: list[Path] = []
    seen: set[Path] = set()
    for search_path in search_paths:
        # `resolve` is non-strict, so a search path that does not exist needs no guard here: it
        # matches nothing and its `iterdir` is skipped below like any unreadable directory.
        frontier = [(search_path.expanduser().resolve(), 0)]
        while frontier:
            directory, depth = frontier.pop(0)
            if directory in seen:
                continue
            seen.add(directory)
            if _is_workspace_root(directory):
                found.append(directory)
                # A root's own project directories cannot contain another root.
                continue
            if depth >= max_depth:
                continue
            try:
                children = sorted(directory.iterdir())
            except OSError:
                # An unreadable directory is skipped, not fatal: a $HOME search crosses plenty.
                continue
            for child in children:
                if child.name.startswith(".") or child.name in ROOT_SEARCH_PRUNED_NAMES:
                    continue
                if child.is_symlink() or not child.is_dir():
                    continue
                frontier.append((child, depth + 1))
    return found


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
    prefixes = "|".join(EXTERNAL_IDENTIFIER_PREFIXES)
    return bool(re.fullmatch(rf"(?:{prefixes}):\S+", value))


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
        report.errors.append(f"{label}: unexpected fields: {', '.join(extras)}; allowed: {', '.join(sorted(allowed))}")


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
    # Enumerated rather than defaulted: the previous form resolved anything that was not
    # "workspace" against working_directory, so a new root would have silently pointed at the
    # target repository and a mistyped one would have resolved instead of being refused.
    if root == "workspace":
        base = project_dir
    elif root == "workspace_root":
        base = project_dir.parent
    elif root == "target":
        base = working_directory
    else:
        return None, f"unknown local reference root {root!r}"
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
    *,
    missing_is_error: bool = True,
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
        report.errors.append(f"{label}: invalid root {root!r}; allowed: {', '.join(sorted(REFERENCE_ROOTS))}")
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
        missing = f"required output does not exist: {root}:{path}"
        if missing_is_error:
            report.errors.append(f"{label}: {missing}")
        else:
            # The task was already terminal before this commit, so the record is not claiming
            # something untrue — the deliverable has moved or gone since. Erroring here would make
            # the project uncommittable, and the dated correction the skill prescribes for exactly
            # this situation is itself a commit.
            report.warnings.append(
                f"{label}: {missing}; the task was already terminal, so this is history that has "
                "moved rather than an unfinished task. Record where it went in a dated correction."
            )


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
        report.errors.append(f"{label}: invalid root {root!r}; allowed: {', '.join(sorted(REFERENCE_ROOTS))}")
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
        report.errors.append(f"{label}: invalid effect kind {kind!r}; allowed: {', '.join(sorted(EFFECT_KINDS))}")
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
        report.errors.append(
            f"{label}: invalid authorization status {status!r}; allowed: {', '.join(sorted(AUTHORIZATION_STATUSES))}"
        )
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
        report.errors.append(
            f"{label}: value must be an http(s) URL with a host, or a durable identifier "
            f"prefixed with one of: " + ", ".join(f"{prefix}:" for prefix in EXTERNAL_IDENTIFIER_PREFIXES)
        )
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


# The specification sections the skill asks for, each with the wordings already in use across
# existing projects. Three closed projects spell these three different ways, so a strict heading
# check would fail history that was correct when it was written: the check warns, and the canonical
# name in the first element is what it names. A recogniser matches, so "Scope" satisfies both scope
# sections and "Deliverables and authorization" satisfies deliverables and authorization alike.
SPEC_CANONICAL_SECTIONS: "tuple[tuple[str, str], ...]" = (
    ("Objective and audience", r"objective"),
    ("In scope", r"in scope|^scope\b"),
    ("Out of scope", r"out of scope|^scope\b"),
    ("Constraints and important assumptions", r"constraint|assumption"),
    ("Success and verification criteria", r"success|verification"),
    ("Deliverables and roots", r"deliverable"),
    ("Destructive and external actions", r"destructive|external|authoriz"),
)


# The briefing sections the skill asks for, in the order `init` writes them. Unlike the
# specification table below, no historical project predates this contract, so the recognisers exist
# to tolerate a reworded heading rather than to excuse one already in use.
BRIEFING_CANONICAL_SECTIONS: "tuple[tuple[str, str], ...]" = (
    ("Stated requirements", r"stated requirement|requirement"),
    ("Verified facts", r"verified|fact"),
    ("Corrected assumptions", r"corrected|assumption"),
    ("Background", r"background|context"),
    ("Open questions for grill", r"open question|question"),
)

# Each skeleton body is one line beginning with this marker, so an untouched briefing is
# distinguishable from a written one. A section whose every line still starts here has no content,
# which is the whole failure the briefing step exists to prevent: a file that looks structured and
# says nothing.
BRIEFING_PLACEHOLDER_PREFIX = "_Not yet written"

# What each skeleton section prompts for. Keyed by canonical name so a heading and its prompt cannot
# drift apart, and rendered by `_briefing_skeleton` in the order the contract declares.
BRIEFING_SECTION_PROMPTS = {
    "Stated requirements": "the user's requirements in their own words, recorded before anything is checked",
    "Verified facts": "each claim that was checked, with the source that establishes it",
    "Corrected assumptions": "what was assumed, what is actually true, and the source that settles it",
    "Background": "how the affected system works today, for context the user may not have",
    "Open questions for grill": "what the briefing could not settle, which becomes grill's first frontier",
}


def _briefing_skeleton(title: str) -> str:
    """The briefing every new project starts from: the contract's headings, none of them written.

    Writing the headings rather than an empty file makes the step's shape discoverable from the
    file itself, and the placeholder prefix is what lets validation tell this apart from a briefing
    somebody actually wrote.
    """
    sections = "".join(
        f"## {canonical}\n\n{BRIEFING_PLACEHOLDER_PREFIX}: {BRIEFING_SECTION_PROMPTS[canonical]}._\n\n"
        for canonical, _ in BRIEFING_CANONICAL_SECTIONS
    )
    return f"# {title} \u2014 briefing\n\n{sections}"


def _level_two_sections(markdown: str) -> "list[tuple[str, str]]":
    r"""Every `##` heading in document order, paired with the body that follows it.

    `###` and deeper do not match, because `\s` cannot consume the third `#`.
    """
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, re.MULTILINE))
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        sections.append((heading.group(1).strip(), markdown[heading.end() : end].strip()))
    return sections


def _is_unwritten(body: str) -> bool:
    """Whether a section body is still nothing but skeleton placeholder lines."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return not lines or all(line.startswith(BRIEFING_PLACEHOLDER_PREFIX) for line in lines)


def briefing_section_warnings(briefing_markdown: str) -> "list[str]":
    """Name each canonical briefing section the file does not cover, or leaves unwritten.

    Warnings only, and never errors: `briefing.md` postdates every project already in a workspace,
    so a missing one must not retroactively invalidate history or block a reopened project. An
    empty body warns as well as a missing heading, because the skeleton `init` writes already has
    every heading.
    """
    sections = _level_two_sections(briefing_markdown)
    if not sections:
        return ["briefing.md has no '##' sections to check"]
    warnings = []
    for canonical, recogniser in BRIEFING_CANONICAL_SECTIONS:
        matched = [body for heading, body in sections if re.search(recogniser, heading, re.IGNORECASE)]
        if not matched:
            warnings.append(f"briefing.md appears to have no '## {canonical}' section")
        elif all(_is_unwritten(body) for body in matched):
            warnings.append(f"briefing.md section '## {canonical}' is still unwritten")
    return warnings


# Where the closing report lives, and the sections it carries. Two authored files rather than one
# generated from the other: the Markdown is the record a terminal, `grep` and `diff` can read, and the
# HTML is the same findings presented, with charts. Deriving either from the other would mean shipping
# a Markdown-to-HTML converter in a stdlib-only package and would cost the charts outright.
# The closure steps evidence may be recorded against. A closure step runs after the last task is
# terminal, so it has no task id to record under, and `record_evidence` refuses an id absent from
# `project.json` — correctly, since a typo'd task id must not silently open a new heading. A fixed
# vocabulary gives the step a heading without loosening that guard: the set is closed, so a typo here
# is refused too.
CLOSURE_STEPS = ("report",)

REPORT_DIRECTORY = "artifacts"
REPORT_MARKDOWN_FILENAME = "report.md"
REPORT_HTML_FILENAME = "report.html"

# The five sections both report files carry, with the same recogniser mechanism the briefing uses.
# `## Limitations and what was not proven` is the section a report is most tempted to omit and the
# one that makes the rest trustworthy, so it is named in the contract rather than left to judgement.
REPORT_CANONICAL_SECTIONS: "tuple[tuple[str, str], ...]" = (
    ("Summary", r"summary|abstract"),
    ("What was done", r"what was done|method|approach"),
    ("Findings and evidence", r"finding|evidence|result"),
    ("Limitations and what was not proven", r"limitation|not proven|caveat"),
    ("Open work", r"open work|follow.?up|next step|remaining"),
)

# The one `###` subsection the contract names, and the section it hangs beneath. It is a subsection
# rather than a sixth `##` because the spine above is closed, and because a graph is subordinate to a
# method narrative rather than a replacement for one. Recognised by heading, with the same reworded
# tolerance the five sections get: a structural check can find a subsection by its heading or not at
# all, since it cannot read the report's meaning.
REPORT_GRAPH_PARENT = "What was done"
REPORT_GRAPH_SUBSECTION = ("Task graph", r"task graph|dependency graph|plan and execution graph")


def _report_paths(project_dir: Path) -> "tuple[Path, Path]":
    """The Markdown and HTML report paths for a project, in that order."""
    directory = project_dir / REPORT_DIRECTORY
    return directory / REPORT_MARKDOWN_FILENAME, directory / REPORT_HTML_FILENAME


_HTML_STRIPPED_ELEMENTS = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_HTML_H2 = re.compile(r"<h2\b[^>]*>(.*?)</h2\s*>", re.DOTALL | re.IGNORECASE)
_HTML_H3 = re.compile(r"<h([3-6])\b[^>]*>(.*?)</h\1\s*>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def _html_headings_as_markdown(html: str) -> str:
    """Reduce an HTML report to the heading structure `_level_two_sections` reads.

    The section contract is one contract over two files, so the HTML is translated into the shape the
    Markdown reader already understands rather than the reader growing a second parser. `<h2>` becomes
    `##`, and `<h3>` through `<h6>` all become `###` because the Markdown reader they feed accepts
    `###`-or-deeper; translating only `<h3>` would let one report pass as Markdown and fail as HTML
    over a heading depth neither contract cares about. Every other tag is dropped, which leaves `<h1>`
    as prose and keeps it from passing as a section. `<style>` and `<script>` bodies are removed
    whole, so a stylesheet cannot contribute text to a section body and make an unwritten section look
    written.

    `###` cannot be mistaken for a `##` section, because `_level_two_sections` requires whitespace
    after the second `#` and finds a third there instead.

    Entities are left as they are: this text is only ever matched against heading recognisers and
    tested for emptiness, and `&amp;` is as non-empty as `&`.
    """
    text = _HTML_STRIPPED_ELEMENTS.sub(" ", html)
    text = _HTML_H2.sub(lambda match: f"\n\n## {_HTML_TAG.sub('', match.group(1)).strip()}\n\n", text)
    text = _HTML_H3.sub(lambda match: f"\n\n### {_HTML_TAG.sub('', match.group(2)).strip()}\n\n", text)
    return _HTML_TAG.sub(" ", text)


def _report_section_findings(markdown: str, label: str) -> "list[str]":
    """Name each canonical report section a file does not cover, or leaves unwritten.

    Shared by the close-time warning and the mechanical `--report` check, so the two can never
    disagree about what the contract is. HTML goes through the same reader: `_level_two_sections`
    matches Markdown `##` headings, and the HTML report's headings are `<h2>`, so the caller
    translates before calling rather than this growing a second parser.
    """
    sections = _level_two_sections(markdown)
    if not sections:
        return [f"{label} has no '##' sections to check"]
    findings = []
    for canonical, recogniser in REPORT_CANONICAL_SECTIONS:
        matched = [body for heading, body in sections if re.search(recogniser, heading, re.IGNORECASE)]
        if not matched:
            findings.append(f"{label} appears to have no '## {canonical}' section")
        elif all(_is_unwritten(body) for body in matched):
            findings.append(f"{label} section '## {canonical}' is still unwritten")
    return findings


def _report_graph_findings(markdown: str, label: str) -> "list[str]":
    """Whether the report carries a written task-graph subsection under its method section.

    Silent when the parent section is itself absent: `_report_section_findings` already reports that,
    and two findings for one missing heading would read as two problems. The subsection is looked for
    only inside the parent's body, so a `### Task graph` filed under `## Open work` does not satisfy
    the contract — where it sits is part of what was agreed.
    """
    canonical, recogniser = REPORT_GRAPH_SUBSECTION
    parent = next(pattern for name, pattern in REPORT_CANONICAL_SECTIONS if name == REPORT_GRAPH_PARENT)
    bodies = [body for heading, body in _level_two_sections(markdown) if re.search(parent, heading, re.IGNORECASE)]
    if not bodies:
        return []
    for body in bodies:
        for heading, subsection in _level_three_sections(body):
            if re.search(recogniser, heading, re.IGNORECASE) and not _is_unwritten(subsection):
                return []
    return [f"{label} has no written '### {canonical}' subsection under '## {REPORT_GRAPH_PARENT}'"]


def report_warnings(project_dir: Path) -> "list[str]":
    """Warn about incomplete existing closing reports, at close and never before.

    Warnings only, on the same reasoning as `briefing.md`: the report postdates every project already
    in a workspace, and a closed project must stay valid and stay reopenable. The trigger differs
    though. The briefing is checked from the moment a project leaves `ALIGNING`, because it is written
    in that phase; a report cannot exist before the work it reports on, so checking it any earlier
    than close would warn every project through its entire working life about a file it is correct not
    to have yet.
    """
    markdown_path, html_path = _report_paths(project_dir)
    warnings = []
    for path, label in ((markdown_path, REPORT_MARKDOWN_FILENAME), (html_path, REPORT_HTML_FILENAME)):
        if not path.exists():
            continue  # Each report format is independently optional.
        relative = f"{REPORT_DIRECTORY}/{label}"
        if not path.is_file():
            warnings.append(f"no closing report at {relative}")
            continue
        try:
            content = read_text(path)
        except WorkspaceError as error:
            warnings.append(f"{relative} is unreadable: {error}")
            continue
        if path is html_path:
            content = _html_headings_as_markdown(content)
        warnings.extend(_report_section_findings(content, relative))
        warnings.extend(_report_graph_findings(content, relative))
    return warnings


# Elements with no closing tag, so the balance check below does not wait for one. HTML's rules are
# larger than this — `<p>` may be closed implicitly too — but a report is authored, not scraped, and
# an unclosed `<p>` in an authored document is a mistake worth naming rather than a shape to tolerate.
_VOID_ELEMENTS = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr")
)

_HTML_ELEMENT = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)\b[^>]*?(/?)>", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE)
_EXTERNAL_SCRIPT = re.compile(r"<script\b[^>]*\bsrc\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
_STYLESHEET_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF = re.compile(r"\bhref\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\b", re.IGNORECASE)
_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"\b([A-Za-z-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)')")
_FIGURE_BLOCK = re.compile(r"<figure\b[^>]*>(.*?)</figure\s*>", re.DOTALL | re.IGNORECASE)
_FIGCAPTION_BLOCK = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption\s*>", re.DOTALL | re.IGNORECASE)
_CSS_DECLARATION = re.compile(r"([-A-Za-z]+)\s*:\s*([^;{}]+)")

# The one network dependency the reference report allows, and the only one this check allows: a web
# font stylesheet. Everything else must be inline, because a report is read after the work is
# finished and a dead CDN link is a report that renders wrong for reasons nobody will diagnose.
ALLOWED_STYLESHEET_HOSTS = ("https://fonts.googleapis.com/",)

# Colour literals outside a token definition. Deliberately a lint and not a CSS parser: it catches
# the shapes an author actually writes, and a value it cannot classify is left alone rather than
# guessed at. The named set is the common ones, not all 148 — a report using `lightgoldenrodyellow`
# is not the failure this guards against.
_COLOUR_LITERAL = re.compile(
    r"(#[0-9A-Fa-f]{3,8}\b|\b(?:rgba?|hsla?|color-mix|lab|lch|oklab|oklch)\s*\(|"
    r"\b(?:white|black|red|blue|green|gray|grey|orange|yellow|purple|pink|brown|cyan|magenta|"
    r"silver|gold|navy|teal|olive|maroon|lime|aqua|fuchsia|beige|ivory|coral|salmon|khaki|indigo|"
    r"violet|crimson|turquoise|tan|plum|orchid|tomato|wheat)\b)"
)

# Properties whose values carry colour. Checking these rather than every declaration keeps the lint
# off `font-family: Georgia` and `grid-template-areas`, where a colour word is not a colour.
_COLOUR_PROPERTIES = (
    "color",
    "background",
    "border",
    "outline",
    "fill",
    "stroke",
    "box-shadow",
    "text-shadow",
    "text-decoration",
    "caret-color",
    "accent-color",
    "column-rule",
    "stop-color",
    "flood-color",
    "lighting-color",
)

# The three blocks a theme-aware report needs, each with the selector that makes it work. A palette
# defined only inside a media query borrows the host's theme in every other state, and one that
# omits the `[data-theme]` blocks ignores an explicit choice the reader has made.
REPORT_THEME_BLOCKS: "tuple[tuple[str, str], ...]" = (
    ("the light palette on bare ':root'", r":root\s*(?:,[^{]*)?\{"),
    (
        "a 'prefers-color-scheme: dark' block guarded as ':root:not([data-theme=\"light\"])'",
        r"prefers-color-scheme\s*:\s*dark",
    ),
    ("an explicit ':root[data-theme=\"dark\"]' block", r":root\[data-theme\s*=\s*[\"']?dark[\"']?\]"),
)


def _attributes(tag: str) -> "dict[str, str]":
    """The attributes of one start tag, lowercased by name, unquoted by value."""
    found = {}
    for match in _ATTRIBUTE.finditer(tag):
        found[match.group(1).lower()] = match.group(3) if match.group(3) is not None else match.group(4) or ""
    return found


def _tag_balance_errors(html: str, label: str) -> "list[str]":
    """Name the first unbalanced tag, if any. One finding, because the rest cascade from it."""
    stripped = _HTML_COMMENT.sub(" ", html)
    stack: "list[str]" = []
    for match in _HTML_ELEMENT.finditer(stripped):
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if name in _VOID_ELEMENTS or self_closing:
            continue
        if not closing:
            stack.append(name)
            continue
        if not stack:
            return [f"{label}: </{name}> closes a tag that was never opened"]
        if stack[-1] != name:
            return [f"{label}: </{name}> closes while <{stack[-1]}> is still open"]
        stack.pop()
    if stack:
        return [f"{label}: <{stack[-1]}> is never closed"]
    return []


def _external_resource_errors(html: str, label: str) -> "list[str]":
    """Name every external script and every stylesheet that is not the allowed font link."""
    errors = []
    for match in _EXTERNAL_SCRIPT.finditer(html):
        errors.append(f"{label}: loads an external script ({match.group(1).strip(chr(34) + chr(39))})")
    for match in _STYLESHEET_LINK.finditer(html):
        attributes = _attributes(match.group(0))
        if "stylesheet" not in attributes.get("rel", "").lower():
            continue
        href = attributes.get("href", "")
        if not href.startswith(ALLOWED_STYLESHEET_HOSTS):
            allowed = ", ".join(ALLOWED_STYLESHEET_HOSTS)
            errors.append(f"{label}: loads an external stylesheet ({href or 'no href'}); only {allowed} is allowed")
    for style in _STYLE_BLOCK.findall(html):
        if _CSS_IMPORT.search(style):
            errors.append(f"{label}: an inline stylesheet uses @import, which fetches at render time")
            break
    return errors


def _colour_token_errors(html: str, label: str) -> "list[str]":
    """Name colour literals used outside a custom-property definition.

    The palette lives in tokens so the three theme blocks can redefine it; a literal anywhere else is
    a colour that cannot follow the theme. Definitions of `--*` are where literals belong, so they
    are skipped, and only properties that actually carry colour are examined.
    """
    errors = []
    for style in _STYLE_BLOCK.findall(html):
        for match in _CSS_DECLARATION.finditer(_HTML_COMMENT.sub(" ", style)):
            prop, value = match.group(1).lower(), match.group(2).strip()
            if prop.startswith("--") or not prop.endswith(_COLOUR_PROPERTIES):
                continue
            literal = _COLOUR_LITERAL.search(value)
            if literal:
                errors.append(f"{label}: '{prop}: {value}' uses the colour literal '{literal.group(0)}'; use var(--…)")
    return errors


def _theme_block_errors(html: str, label: str) -> "list[str]":
    """Name each of the three theme blocks the document does not define."""
    styles = "\n".join(_STYLE_BLOCK.findall(html))
    return [
        f"{label}: no {description}"
        for description, recogniser in REPORT_THEME_BLOCKS
        if not re.search(recogniser, styles, re.IGNORECASE)
    ]


def _chart_errors(html: str, label: str) -> "list[str]":
    """Name every chart that cannot be read without seeing it, or read without its interpretation.

    A chart carries the same finding twice — once as geometry and once as text — so a reader using a
    screen reader, a terminal, or a printout is not reading a hole. `role` and `aria-label` carry the
    first; the `<figcaption>` carries the second, which is why a bare `<svg>` outside a `<figure>` is
    a finding even when its labels are perfect.
    """
    errors = []
    for index, match in enumerate(_SVG_OPEN.finditer(html), start=1):
        attributes = _attributes(match.group(0))
        if attributes.get("role", "").strip() != "img":
            errors.append(f'{label}: <svg> #{index} has no role="img"')
        if not attributes.get("aria-label", "").strip():
            errors.append(f"{label}: <svg> #{index} has no non-empty aria-label")
    figures = _FIGURE_BLOCK.findall(html)
    charted = sum(1 for figure in figures if _SVG_OPEN.search(figure))
    total = len(_SVG_OPEN.findall(html))
    if total > charted:
        errors.append(f"{label}: {total - charted} <svg> of {total} is outside a <figure>, so it can carry no caption")
    for index, figure in enumerate(figures, start=1):
        if not _SVG_OPEN.search(figure):
            continue
        # The caption's *text* has to be non-empty, not just its element. Matching for any
        # non-whitespace after the start tag would accept `<figcaption> </figcaption>`, because the
        # `<` of the closing tag is itself non-whitespace — which is how a blank caption first got
        # past this check.
        captions = [_HTML_TAG.sub("", body).strip() for body in _FIGCAPTION_BLOCK.findall(figure)]
        if not any(captions):
            errors.append(f"{label}: <figure> #{index} holds a chart with no non-empty <figcaption>")
    return errors


def report_findings(project_dir: Path, report_format: str = "both") -> ValidationReport:
    """Check the requested report format(s) mechanically, as errors rather than warnings.

    This is what `--report` runs, and it is deliberately harsher than the close-time warning: the
    warning checks only existing reports, while this also requires the requested files. The step runs
    through `record-evidence` so its exit code becomes a record instead of a claim. What it can
    check is structure — sections, tags, tokens, themes, labels, captions. What it cannot check is
    whether the report is true, or whether a chart's bars are the length its numbers imply; the
    first is the reader's job and the second is arithmetic the author verifies against the `viewBox`.
    """
    report = ValidationReport()
    if report_format not in {"markdown", "html", "both"}:
        report.errors.append(f"unknown report format: {report_format}")
        return report
    markdown_path, html_path = _report_paths(project_dir)
    for format_name, path, label in (
        ("markdown", markdown_path, REPORT_MARKDOWN_FILENAME),
        ("html", html_path, REPORT_HTML_FILENAME),
    ):
        if report_format not in {format_name, "both"}:
            continue
        relative = f"{REPORT_DIRECTORY}/{label}"
        if not path.is_file():
            report.errors.append(f"no report at {relative}")
            continue
        try:
            content = read_text(path)
        except WorkspaceError as error:
            report.errors.append(f"{relative} is unreadable: {error}")
            continue
        if path is html_path:
            headings = _html_headings_as_markdown(content)
            report.errors.extend(_report_section_findings(headings, relative))
            report.errors.extend(_report_graph_findings(headings, relative))
            report.errors.extend(_tag_balance_errors(content, relative))
            report.errors.extend(_external_resource_errors(content, relative))
            report.errors.extend(_colour_token_errors(content, relative))
            report.errors.extend(_theme_block_errors(content, relative))
            report.errors.extend(_chart_errors(content, relative))
        else:
            report.errors.extend(_report_section_findings(content, relative))
            report.errors.extend(_report_graph_findings(content, relative))
    return report


def _level_three_sections(markdown: str) -> "list[tuple[str, str]]":
    """Every `###`-or-deeper heading in document order, paired with the body that follows it.

    Deeper levels count because a spec that nests its sections under an extra heading is still
    covering them, and that shape is already in use.
    """
    headings = list(re.finditer(r"^#{3,}\s+(.+?)\s*$", markdown, re.MULTILINE))
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        sections.append((heading.group(1).strip(), markdown[heading.end() : end].strip()))
    return sections


def spec_section_warnings(spec_markdown: str) -> "list[str]":
    """Name each canonical specification section the spec does not cover, or leaves unwritten.

    Warnings only. The skill states what `## Current specification` must contain and nothing
    checked, so a project could leave `ALIGNING` with no recorded constraints or authorization
    states at all. Wording is not the subject here — a missing section is.

    An empty section body warns too, on the same mechanism as `briefing.md`: a heading with nothing
    under it satisfied the original check while saying exactly as little as a missing one, and the
    skill's instruction to write down that a branch is ungrilled rather than delete its heading made
    that the likely shape of the failure rather than an unlikely one.
    """
    specification = _section_content(spec_markdown, "Current specification")
    if specification is None:
        return ["spec.md has no '## Current specification' section"]
    sections = _level_three_sections(specification)
    if not sections:
        return ["spec.md '## Current specification' has no '###' sections to check"]
    warnings = []
    for canonical, recogniser in SPEC_CANONICAL_SECTIONS:
        matched = [body for heading, body in sections if re.search(recogniser, heading, re.IGNORECASE)]
        if not matched:
            warnings.append(f"spec.md '## Current specification' appears to have no '### {canonical}' section")
        elif all(_is_unwritten(body) for body in matched):
            warnings.append(f"spec.md section '### {canonical}' is still unwritten")
    return warnings


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
    already_done: set[str] | None = None,
) -> ValidationReport:
    """Validate a v3 candidate. `already_done` names tasks that were DONE before this candidate.

    A missing required output means two different things depending on when the task finished. For a
    task going DONE now it means the work is not done, and that must fail. For a task that finished
    long ago it usually means the deliverable was renamed — and failing there makes the project
    permanently uncommittable, including the dated correction that would explain the rename. So
    callers holding the previous state pass it; callers without one (the standalone validator, the
    migration preview) pass nothing and every DONE task is held to the stricter rule, which keeps
    their reports unchanged.
    """
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
        report.errors.append(f"project: invalid status {status!r}; allowed: {', '.join(sorted(PROJECT_STATUSES))}")

    working_directory_value = state.get("working_directory")
    working_directory = Path(working_directory_value) if _non_empty_string(working_directory_value) else project_dir
    if not working_directory.is_absolute():
        report.errors.append("project: working_directory must be absolute")
    elif check_files and not working_directory.is_dir():
        report.errors.append(f"project: working_directory does not exist: {working_directory}")
    elif check_files:
        report.warnings.extend(self_location_warnings(working_directory))

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
        report.errors.append(f"review: invalid status {review_status!r}; allowed: {', '.join(sorted(REVIEW_STATUSES))}")
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
            report.errors.append(
                f"{label}: invalid task status {task_status!r}; allowed: {', '.join(sorted(TASK_STATUSES))}"
            )
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
                missing_is_error=already_done is None or task_id not in already_done,
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

    # A specification is meant to be complete before the project leaves ALIGNING, where it is still
    # the skeleton `init` wrote, so the section check starts once it has left. The briefing is
    # written in the same phase and is checked on the same trigger, but only ever as warnings, and
    # only when the file exists: every project created before the briefing step lacks one, and a
    # closed project must stay valid and stay reopenable.
    if check_files and status != "ALIGNING":
        spec_path = project_dir / "spec.md"
        if spec_path.exists():
            report.warnings.extend(spec_section_warnings(read_text(spec_path)))
        briefing_path = project_dir / "briefing.md"
        if briefing_path.exists():
            report.warnings.extend(briefing_section_warnings(read_text(briefing_path)))

    # The closing report is checked at close and for a project already past it, and at no other
    # point. `CANCELLED` counts: the step runs on that path too, reporting what was abandoned and
    # why. Unlike the section check above this cannot key off "has left ALIGNING", because a report
    # cannot exist before the work it reports on.
    if check_files and (close or _enum_string(status, {"DONE", "CANCELLED"})):
        report.warnings.extend(report_warnings(project_dir))

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


EXECUTION_FIELDS = {"protocol_version", "coordinator_run", "ownership_generation", "attempts"}


def validate_v4_state(
    state: dict[str, Any],
    project_dir: Path,
    *,
    close: bool = False,
    check_files: bool = True,
    already_done: set[str] | None = None,
) -> ValidationReport:
    """Validate a v4 candidate. Mirrors v3 validation but accepts the execution block."""
    report = ValidationReport()
    _missing_fields(state, PROJECT_V4_FIELDS - {"predecessor"}, "project", report)
    _unexpected_fields(state, PROJECT_V4_FIELDS, "project", report)

    if state.get("schema_version") != 4:
        report.errors.append("schema_version must be 4")

    execution = state.get("execution")
    if not isinstance(execution, dict):
        report.errors.append("project: execution must be an object")
    else:
        _missing_fields(execution, EXECUTION_FIELDS, "execution", report)
        _unexpected_fields(execution, EXECUTION_FIELDS, "execution", report)
        protocol_version = execution.get("protocol_version")
        if not isinstance(protocol_version, int) or isinstance(protocol_version, bool) or protocol_version < 1:
            report.errors.append("execution: protocol_version must be a positive integer")
        coordinator_run = execution.get("coordinator_run")
        if coordinator_run is not None and not _non_empty_string(coordinator_run):
            report.errors.append("execution: coordinator_run must be null or a non-empty string")
        ownership_generation = execution.get("ownership_generation")
        if (
            not isinstance(ownership_generation, int)
            or isinstance(ownership_generation, bool)
            or ownership_generation < 0
        ):
            report.errors.append("execution: ownership_generation must be a non-negative integer")
        attempts = execution.get("attempts")
        if not isinstance(attempts, dict):
            report.errors.append("execution: attempts must be an object")

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
        report.errors.append(f"project: invalid status {status!r}; allowed: {', '.join(sorted(PROJECT_STATUSES))}")

    working_directory_value = state.get("working_directory")
    working_directory = Path(working_directory_value) if _non_empty_string(working_directory_value) else project_dir
    if not working_directory.is_absolute():
        report.errors.append("project: working_directory must be absolute")
    elif check_files and not working_directory.is_dir():
        report.errors.append(f"project: working_directory does not exist: {working_directory}")
    elif check_files:
        report.warnings.extend(self_location_warnings(working_directory))

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
        report.errors.append(f"review: invalid status {review_status!r}; allowed: {', '.join(sorted(REVIEW_STATUSES))}")
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
        _unexpected_fields(task, TASK_V4_FIELDS, label, report)
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
            report.errors.append(
                f"{label}: invalid task status {task_status!r}; allowed: {', '.join(sorted(TASK_STATUSES))}"
            )
            task_status = None
        for field_name in ("name", "success_criteria", "verification"):
            if not _non_empty_string(task.get(field_name)):
                report.errors.append(f"{label}: {field_name} must be a non-empty string")

        if "reads" in task:
            reads = task.get("reads")
            if not isinstance(reads, list) or not all(_non_empty_string(item) for item in reads):
                report.errors.append(f"{label}: reads must be a list of non-empty target-relative paths")
            elif len(reads) != len(set(reads)):
                report.errors.append(f"{label}: reads contains duplicates")
            else:
                for read in reads:
                    _resolved, error = _resolve_local_reference(
                        project_dir,
                        working_directory,
                        "target",
                        read,
                    )
                    if error is not None:
                        report.errors.append(f"{label}: invalid read {read!r}: {error}")

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
                missing_is_error=already_done is None or task_id not in already_done,
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

    if check_files and status != "ALIGNING":
        spec_path = project_dir / "spec.md"
        if spec_path.exists():
            report.warnings.extend(spec_section_warnings(read_text(spec_path)))
        briefing_path = project_dir / "briefing.md"
        if briefing_path.exists():
            report.warnings.extend(briefing_section_warnings(read_text(briefing_path)))

    if check_files and (close or _enum_string(status, {"DONE", "CANCELLED"})):
        report.warnings.extend(report_warnings(project_dir))

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


# Advisory memory is only useful if it is read, and a file that keeps growing gets skimmed instead.
# Twenty entries is a judgement, not a measurement: it is the scale at which the file stopped fitting
# on a screen, which is when merging and retiring needs to happen rather than one more append.
REFLECTION_MAX_ENTRIES = 20

# Two shapes are in use, and both are entries. The bulleted form carries its provenance in a leading
# bracket; the dated form is a `## YYYY-MM-DD — lesson` section carrying provenance on a `Source:`
# line. Counting only the first is how a 47-entry file sat silently under a threshold of 20.
REFLECTION_BULLET_ENTRY_PATTERN = re.compile(r"^- \[(?P<meta>[^\]]*)\]", re.MULTILINE)
REFLECTION_HEADING_PATTERN = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.*?)\s*$", re.MULTILINE)
REFLECTION_DATED_TITLE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
REFLECTION_SOURCE_PATTERN = re.compile(r"^Source:(?P<meta>.*)$", re.MULTILINE)


def _dated_section_body(content: str, headings: "list[re.Match[str]]", index: int) -> str:
    """The text under one heading, ending where a heading of the same or shallower level begins.

    Depth matters rather than an exact `##`: a dated lesson that subdivides itself keeps its
    subsections, while the next lesson — or the next top-level section — ends it.
    """
    level = len(headings[index].group("hashes"))
    for later in headings[index + 1 :]:
        if len(later.group("hashes")) <= level:
            return content[headings[index].end() : later.start()]
    return content[headings[index].end() :]


def reflection_entries(content: str) -> "list[str]":
    """Every reflection entry, as its provenance text, in document order.

    The list length is what the scale warning counts, and each element is where that entry says it
    came from: a bullet's bracketed metadata, or the `Source:` lines of a dated section joined
    together. Prose is deliberately not searched for project ids — a lesson that mentions a project
    in passing is not citing it as a source, which is how the bulleted form has always been read.
    """
    found = [(match.start(), match.group("meta")) for match in REFLECTION_BULLET_ENTRY_PATTERN.finditer(content)]
    headings = list(REFLECTION_HEADING_PATTERN.finditer(content))
    for index, heading in enumerate(headings):
        if not REFLECTION_DATED_TITLE_PATTERN.match(heading.group("title")):
            continue
        body = _dated_section_body(content, headings, index)
        sources = " ".join(match.group("meta") for match in REFLECTION_SOURCE_PATTERN.finditer(body))
        found.append((heading.start(), sources))
    return [meta for _, meta in sorted(found)]


# `.git` is a file, not a directory, inside a worktree or submodule, so presence is what is checked.
VCS_MARKERS = (".git", ".hg", ".jj", ".svn")


def vcs_warnings(workspace_root: Path) -> "list[str]":
    """Warn when nothing above the workspace root is under version control.

    The workspace is the record of what was decided and verified, and a mistaken delete or a bad
    hand-edit of `project.json` has no recovery path without history. This only reports: no
    repository is created here, and nothing outside a project's own directory is written.
    """
    root = workspace_root.expanduser()
    try:
        root = root.resolve()
        if not root.is_dir():
            return []
    except OSError:
        return []
    for directory in (root, *root.parents):
        if any((directory / marker).exists() for marker in VCS_MARKERS):
            return []
    return [
        f"{root} is not under version control; a mistaken delete or hand-edit of project.json has "
        "no recovery path. Consider running 'git init' there yourself — these tools will not."
    ]


# The path of this module inside the plugin, used to recognise a working directory that holds the
# very tools being run.
PLUGIN_SELF_PATH = Path("plugins/research/skills/project/scripts/workspace_lib.py")


def self_location_warnings(working_directory: Path) -> "list[str]":
    """Warn when the tools running are not the copy of the tools being edited.

    Hosts install this plugin into a version-keyed cache, so `research-project` on PATH is a frozen
    copy. A project whose working directory is the plugin's own repository therefore edits one file
    and verifies another: the edit appears to have no effect, or worse, a test appears to pass
    against code that does not contain the change. The failure is silent, which is why this is a
    warning rather than a note in the documentation — that note already existed and did not help.

    Advisory only, and deliberately narrow: it fires solely when the working directory really does
    contain this module, so ordinary projects never see it.
    """
    try:
        working_directory = working_directory.expanduser().resolve()
        under_test = working_directory / PLUGIN_SELF_PATH
        if not under_test.is_file():
            return []
        running = Path(__file__).resolve()
        try:
            running.relative_to(working_directory)
        except ValueError:
            return [
                f"the running tools are {running}, which is not the copy under {working_directory}. "
                f"Edits to {under_test} will not affect this command, and a passing verification "
                "would be evidence about the installed copy instead. Invoke the working-tree "
                "launchers at plugins/research/skills/project/scripts/ for anything verifying this "
                "plugin."
            ]
    except OSError:
        return []
    return []


def reflection_warnings(workspace_root: Path) -> "list[str]":
    """Warn about a cross-project reflection that has outgrown its readers or cites what is gone.

    Two failure modes, both advisory. A file long enough to skim stops being memory, and an entry
    whose source project is no longer in the workspace cannot be checked against the evidence that
    produced it — its provenance has to be repaired or the entry retired.

    Both checks read `reflection_entries`, so both see the dated `## YYYY-MM-DD` sections as well as
    the bulleted entries. The threshold is unchanged: it was never wrong, it was just being compared
    against a fraction of the file.
    """
    reflection_path = workspace_root / "reflection.md"
    if not reflection_path.is_file():
        return []
    try:
        content = read_text(reflection_path)
    except WorkspaceError:
        # An unreadable reflection is not a reason to fail a project's validation.
        return []

    warnings: list[str] = []
    entries = reflection_entries(content)
    if len(entries) > REFLECTION_MAX_ENTRIES:
        warnings.append(
            f"{reflection_path} holds {len(entries)} entries, above the {REFLECTION_MAX_ENTRIES} "
            "this file stays readable at; merge or retire entries rather than appending"
        )

    try:
        present = {child.name for child in workspace_root.iterdir() if child.is_dir()}
    except OSError:
        return warnings
    cited = {project_id for meta in entries for project_id in re.findall(r"\d{4}-\d{2}-\d{2}-\d{3}", meta)}
    dangling = sorted(cited - present)
    if dangling:
        warnings.append(
            f"{reflection_path} cites source projects absent from the workspace: "
            f"{', '.join(dangling)}; repair the provenance or retire those entries"
        )
    return warnings


# --- Cross-project memory ------------------------------------------------------------------------
#
# Memory is layered by how often each layer is read, and only the always-read layer carries a budget.
# `MEMORY.md` is a generated index of pointers; `memory/<slug>.md` holds the lessons themselves at any
# length, because nobody pays for those bytes until a pointer says to open one. The flat
# `reflection.md` this replaces reached 61 KB while counting as 15 of an allowed 20 "entries": the
# guardrail measured entries and the reader pays in bytes, and the merge discipline that same file
# prescribed drove the two apart, since merging lowers the entry count while raising the byte count.
# So the budget here is bytes and lines, applied to the file that is always read.
#
# The always-read file carries only the layer a person can compress. Post-mortem pointers used to
# sit in it too, one per project, generated from canonical state and never retired — so the only
# remedy the budget error can name, "merge or retire topics", drained a finite pool against a term
# that grows once per project. Measured on a real root at the point it first went over: 25 topic
# pointers held 5480 bytes and 49 post-mortem pointers held 6577, so a merge bought about 200 bytes
# against 134 bytes per future project, and the instruction ran out before the workspace did. The
# same numbers show the two bounds were never calibrated to each other: at a real mean line width
# the byte budget binds around 74 lines, so the 120-line bound could not be reached and the design's
# "40 topics plus 40 post-mortems" capacity was never available. Post-mortems now render into
# `POSTMORTEMS.md`, which is read on demand and therefore unbudgeted, and `MEMORY.md` links to it in
# one line. Growth moves out of the budgeted file; what stays in it is what merging can actually fix.
#
# See references/memory-architecture.md for the design, including what it deliberately leaves out.

MEMORY_INDEX_FILENAME = "MEMORY.md"
POSTMORTEM_INDEX_FILENAME = "POSTMORTEMS.md"
MEMORY_DIRECTORY = "memory"
MEMORY_STAGING_FILENAME = "memory-staging.md"
MEMORY_STAGING_PLACEHOLDER = "_No candidate lessons staged yet._"
MEMORY_STAGING_SKELETON = (
    "# Staged lessons\n"
    "\n"
    "Append a line whenever something surprises you, mid-project, without stopping to decide where it\n"
    "belongs. At close, drain this file: promote each line into `memory/<slug>.md` with\n"
    "`research-project promote-memory`, fold it into this project's `reflection.md`, or drop it.\n"
    "\n"
    f"{MEMORY_STAGING_PLACEHOLDER}\n"
)

# Both bounds bind. A file can be short and wide or narrow and long, and either way it stops being
# something a session can afford to read in full before doing anything else.
MEMORY_INDEX_MAX_LINES = 120
MEMORY_INDEX_MAX_BYTES = 12 * 1024

# Closed like every other enum here, so a mistyped kind is refused rather than silently opening a
# fourth group that nothing renders and nobody reads. These three are the sections the flat
# cross-project file had already grown on its own.
MEMORY_KINDS = ("preference", "environment", "method")
MEMORY_KIND_TITLES = {
    "preference": "Confirmed user preferences",
    "environment": "Environment and tooling",
    "method": "Method",
}
MEMORY_FRONTMATTER_FIELDS = ("name", "description", "kind", "scope", "sources", "updated")
# `sources` alone may be empty: a lesson can predate the projects that would cite it. Every other
# field is load-bearing for either retrieval or provenance, so an empty one is malformed.
MEMORY_OPTIONAL_FRONTMATTER_FIELDS = ("sources",)
MEMORY_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEMORY_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEMORY_FRONTMATTER_DELIMITER = "---"


@dataclass
class MemoryTopic:
    """One parsed topic file: the pointer `MEMORY.md` renders, plus where the lesson came from."""

    path: Path
    name: str
    description: str
    kind: str
    scope: str
    sources: list[str] = field(default_factory=list)
    updated: str = ""


def parse_memory_frontmatter(content: str) -> "tuple[dict[str, str], list[str]]":
    """Split a topic file's leading `---` block into fields, reporting whatever is malformed.

    Deliberately not YAML. The shipped scripts are stdlib-only, and the contract is six flat
    `key: value` lines; a topic file that needs more structure than that is doing something this
    design does not ask of it.
    """
    problems: list[str] = []
    lines = content.splitlines()
    if not lines or lines[0].strip() != MEMORY_FRONTMATTER_DELIMITER:
        return {}, [f"must open with a '{MEMORY_FRONTMATTER_DELIMITER}' frontmatter line"]
    fields: dict[str, str] = {}
    closed = False
    for number, line in enumerate(lines[1:], start=2):
        if line.strip() == MEMORY_FRONTMATTER_DELIMITER:
            closed = True
            break
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            problems.append(f"line {number} is not a 'key: value' pair: {line.strip()!r}")
            continue
        key = key.strip()
        if key in fields:
            problems.append(f"duplicate frontmatter field: {key!r}")
        fields[key] = value.strip()
    if not closed:
        problems.append(f"frontmatter is never closed by a '{MEMORY_FRONTMATTER_DELIMITER}' line")
    return fields, problems


def parse_memory_topic(path: Path, content: str) -> "tuple[MemoryTopic | None, list[str]]":
    """Parse one topic file, returning no topic when anything is wrong and always saying what.

    Never raises. A half-written topic file reaches this function on the commit path, where an
    exception would refuse to record work that is already finished.
    """
    fields, problems = parse_memory_frontmatter(content)
    slug = path.stem
    if not MEMORY_SLUG_PATTERN.match(slug):
        problems.append(f"filename {path.name!r} is not a lowercase-hyphenated slug")
    for required in MEMORY_FRONTMATTER_FIELDS:
        if required not in fields:
            problems.append(f"missing frontmatter field: {required!r}")
        elif not fields[required] and required not in MEMORY_OPTIONAL_FRONTMATTER_FIELDS:
            problems.append(f"empty frontmatter field: {required!r}")
    unknown = sorted(set(fields) - set(MEMORY_FRONTMATTER_FIELDS))
    if unknown:
        problems.append(f"unknown frontmatter field(s): {', '.join(repr(name) for name in unknown)}")
    name = fields.get("name", "")
    if name and name != slug:
        problems.append(f"frontmatter name {name!r} does not match the filename slug {slug!r}")
    kind = fields.get("kind", "")
    if kind and kind not in MEMORY_KINDS:
        problems.append(f"kind {kind!r} is not one of: {', '.join(MEMORY_KINDS)}")
    updated = fields.get("updated", "")
    if updated and not MEMORY_DATE_PATTERN.match(updated):
        problems.append(f"updated {updated!r} is not a YYYY-MM-DD date")
    sources = [item.strip() for item in fields.get("sources", "").split(",") if item.strip()]
    for source in sources:
        if not PROJECT_ID_PATTERN.match(source):
            problems.append(f"source {source!r} is not a YYYY-MM-DD-NNN project id")
    if problems:
        return None, problems
    return (
        MemoryTopic(
            path=path,
            name=name,
            description=fields["description"],
            kind=kind,
            scope=fields["scope"],
            sources=sources,
            updated=updated,
        ),
        [],
    )


def _memory_topic_paths(workspace_root: Path) -> "list[Path]":
    directory = workspace_root / MEMORY_DIRECTORY
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix == ".md")


def load_memory_topics(workspace_root: Path) -> "tuple[list[MemoryTopic], list[str]]":
    """Every parseable topic file, plus one problem string per file that is not parseable.

    The split return is what lets generation and validation disagree about severity for the same
    file: generation skips a malformed topic and warns, validation calls it an error.
    """
    topics: list[MemoryTopic] = []
    problems: list[str] = []
    for path in _memory_topic_paths(workspace_root):
        try:
            content = read_text(path)
        except WorkspaceError as error:
            problems.append(str(error))
            continue
        topic, found = parse_memory_topic(path, content)
        problems.extend(f"{path}: {problem}" for problem in found)
        if topic is not None:
            topics.append(topic)
    return topics, problems


def _memory_postmortem_rows(workspace_root: Path) -> "list[str]":
    """One pointer per project that actually has a post-mortem, titled from canonical state.

    Titles come from `project.json`, never from the post-mortem's own heading. Across this
    workspace those headings carry four different prefixes and two of them name no project at all,
    so an index built from them would be wrong in a way nothing downstream could detect.
    """
    rows: list[str] = []
    if not workspace_root.is_dir():
        return rows
    for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not _has_readable_content(child / "reflection.md"):
            continue
        state_path = child / "project.json"
        if not state_path.is_file():
            continue
        try:
            state = load_json(state_path)
        except WorkspaceError:
            continue
        title = _escape_table(state.get("title", "Untitled"))
        status = _escape_table(state.get("status", "INVALID"))
        rows.append(f"- [{child.name}]({child.name}/reflection.md) — {title} ({status})")
    return rows


def render_memory_index(workspace_root: Path) -> str:
    """Render `MEMORY.md`: the topic pointers, plus one pointer to `POSTMORTEMS.md`.

    A full render rather than a delta, which is what makes concurrent rebuilds safe: whichever of
    two racing rebuilds writes last writes from a filesystem that already holds the other's work.
    """
    topics, _ = load_memory_topics(workspace_root)
    parts = [
        "# Cross-project memory\n",
        "\n",
        "<!-- Generated by manage_workspace.py; do not edit manually. -->\n",
        "\n",
        "Pointers only. Open a topic file when its description and scope match the work in hand,\n",
        "and run `research-project search-memory` when they do not.\n",
    ]
    for kind in MEMORY_KINDS:
        chosen = sorted((topic for topic in topics if topic.kind == kind), key=lambda topic: topic.name)
        if not chosen:
            continue
        parts.append(f"\n## {MEMORY_KIND_TITLES[kind]}\n\n")
        for topic in chosen:
            parts.append(
                f"- [{topic.name}]({MEMORY_DIRECTORY}/{topic.name}.md) — "
                f"{_escape_table(topic.description)} _(scope: {_escape_table(topic.scope)})_\n"
            )
    rows = _memory_postmortem_rows(workspace_root)
    if rows:
        # One line, not one per project. The count is what makes the pointer worth following, and it
        # is the only part of this section that grows.
        parts.append("\n## Project post-mortems\n\n")
        parts.append(
            f"- [{POSTMORTEM_INDEX_FILENAME}]({POSTMORTEM_INDEX_FILENAME}) — {len(rows)} project "
            "post-mortem(s), each titled from canonical state _(scope: looking for how a named "
            "project went, or what it decided)_\n"
        )
    return "".join(parts)


def render_postmortem_index(workspace_root: Path) -> str:
    """Render `POSTMORTEMS.md`: one pointer per project that has a post-mortem.

    Read on demand rather than at discovery, so it carries no budget — which is the whole reason
    these rows are not in `MEMORY.md`. A full render, for the same reason `render_memory_index` is
    one: whichever of two racing rebuilds writes last writes from a filesystem that already holds
    the other's work.
    """
    parts = [
        "# Project post-mortems\n",
        "\n",
        "<!-- Generated by manage_workspace.py; do not edit manually. -->\n",
        "\n",
        "One line per project that wrote a post-mortem, titled from canonical state. Open one when\n",
        "its title matches the work in hand, and run `research-project search-memory` when none\n",
        "does — it searches these bodies as well as the topic files.\n",
        "\n",
    ]
    parts.extend(f"{row}\n" for row in _memory_postmortem_rows(workspace_root))
    return "".join(parts)


def memory_index_path(workspace_root: Path) -> Path:
    return workspace_root / MEMORY_INDEX_FILENAME


def postmortem_index_path(workspace_root: Path) -> Path:
    return workspace_root / POSTMORTEM_INDEX_FILENAME


def memory_lock(workspace_root: Path, *, timeout: float = 5.0) -> DirectoryLock:
    """The lock guarding read-modify-write of `memory/<slug>.md`.

    Its own lock rather than `.index.lock`, because the commit path already takes that one to
    regenerate the index: amending a topic and then rebuilding under one lock would deadlock on a
    non-reentrant `mkdir` lock. Nothing here is ever held across a rebuild.
    """
    return DirectoryLock(workspace_root / ".memory.lock", timeout=timeout)


def render_memory_topic(topic: MemoryTopic, body: str) -> str:
    lines = [
        MEMORY_FRONTMATTER_DELIMITER,
        f"name: {topic.name}",
        f"description: {topic.description}",
        f"kind: {topic.kind}",
        f"scope: {topic.scope}",
        f"sources: {', '.join(topic.sources)}",
        f"updated: {topic.updated}",
        MEMORY_FRONTMATTER_DELIMITER,
    ]
    return "\n".join(lines) + "\n\n" + body.strip("\n") + "\n"


def memory_topic_body(content: str) -> str:
    """Everything below the closing frontmatter delimiter."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != MEMORY_FRONTMATTER_DELIMITER:
        return content.strip("\n")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == MEMORY_FRONTMATTER_DELIMITER:
            return "\n".join(lines[index + 1 :]).strip("\n")
    return ""


def amend_memory_topic(
    workspace_root: Path,
    slug: str,
    *,
    body: str,
    description: str = "",
    kind: str = "",
    scope: str = "",
    sources: "Sequence[str]" = (),
    updated: str = "",
    lock_timeout: float = 5.0,
) -> Path:
    """Create or amend one topic file under `.memory.lock`, never rewriting its body from scratch.

    Amending rather than replacing is the point. A rewrite loses the incident that made the lesson
    credible, and provenance is the only thing separating a recorded lesson from an opinion, so
    `sources` accumulates and new text lands below what is already there.

    The lock is what makes this safe when two projects close at the same moment and reach for the
    same topic. Without it the later write carries none of the earlier one's new source id, and
    nothing reports that it happened.
    """
    if not MEMORY_SLUG_PATTERN.match(slug):
        raise WorkspaceError(f"topic slug must be lowercase-hyphenated: {slug!r}")
    if kind and kind not in MEMORY_KINDS:
        raise WorkspaceError(f"kind {kind!r} is not one of: {', '.join(MEMORY_KINDS)}")
    for source in sources:
        if not PROJECT_ID_PATTERN.match(source):
            raise WorkspaceError(f"source {source!r} is not a YYYY-MM-DD-NNN project id")
    stamp = updated or datetime.now().astimezone().date().isoformat()
    if not MEMORY_DATE_PATTERN.match(stamp):
        raise WorkspaceError(f"updated {stamp!r} is not a YYYY-MM-DD date")
    if not body.strip():
        raise WorkspaceError("a topic amendment needs a non-empty body")
    path = workspace_root / MEMORY_DIRECTORY / f"{slug}.md"
    try:
        with memory_lock(workspace_root, timeout=lock_timeout):
            if path.is_file():
                content = read_text(path)
                topic, problems = parse_memory_topic(path, content)
                if topic is None:
                    raise WorkspaceError(f"refusing to amend an unparseable topic file {path}: " + "; ".join(problems))
                existing_body = memory_topic_body(content)
                for source in sources:
                    if source not in topic.sources:
                        topic.sources.append(source)
                topic.description = description or topic.description
                topic.kind = kind or topic.kind
                topic.scope = scope or topic.scope
                topic.updated = stamp
            else:
                missing = [
                    label
                    for label, value in (("description", description), ("kind", kind), ("scope", scope))
                    if not value
                ]
                if missing:
                    raise WorkspaceError(f"a new topic file needs {', '.join(missing)}: {path} does not exist yet")
                existing_body = ""
                topic = MemoryTopic(
                    path=path,
                    name=slug,
                    description=description,
                    kind=kind,
                    scope=scope,
                    sources=list(sources),
                    updated=stamp,
                )
            combined = f"{existing_body}\n\n{body.strip()}" if existing_body else body.strip()
            atomic_write_text(path, render_memory_topic(topic, combined))
    except OSError as error:
        raise WorkspaceError(f"cannot amend {path}: {error}") from error
    return path


def memory_staging_warnings(project_dir: Path) -> "list[str]":
    """Warn when a project closes with candidate lessons still staged and untriaged.

    A warning and never an error. "Triaged" is not mechanically checkable, and refusing to close a
    project whose actual deliverables are all done because a note is still sitting in a scratch file
    would punish exactly the discipline this file exists to encourage.
    """
    path = project_dir / MEMORY_STAGING_FILENAME
    if not path.is_file():
        return []
    try:
        content = read_text(path)
    except WorkspaceError:
        return []
    # Only a line the skeleton did not put there is a staged lesson. The skeleton explains in prose
    # how to drain the file, so subtracting just the placeholder would make every freshly initialized
    # project warn at close about the very instructions telling it there is nothing to drain.
    scaffolding = set(MEMORY_STAGING_SKELETON.splitlines())
    staged = [
        line
        for line in content.splitlines()
        if line.strip() and line not in scaffolding and not line.lstrip().startswith("#")
    ]
    if not staged:
        return []
    return [
        f"{path} still holds staged lessons at close; promote each one into "
        f"{MEMORY_DIRECTORY}/<slug>.md, fold it into this project's reflection.md, or drop it"
    ]


def memory_findings(workspace_root: Path, *, check_index: bool = False) -> ValidationReport:
    """Validate a workspace root's memory layer.

    Absence is never a finding. A root with no `MEMORY.md` and no `memory/` is valid, and so is
    every project in it: every project created before this layer existed has to stay valid and stay
    reopenable, which is the same reason `briefing.md` is not required at close.
    """
    report = ValidationReport()
    if (workspace_root / "reflection.md").is_file():
        report.warnings.append(
            f"{workspace_root / 'reflection.md'} is the legacy flat cross-project reflection; migrate its "
            f"entries into {MEMORY_DIRECTORY}/<slug>.md topic files and delete it"
        )
    index_path = memory_index_path(workspace_root)
    directory = workspace_root / MEMORY_DIRECTORY
    if not index_path.is_file() and not directory.is_dir():
        return report

    topics, problems = load_memory_topics(workspace_root)
    report.errors.extend(problems)

    if index_path.is_file():
        try:
            content = read_text(index_path)
        except WorkspaceError as error:
            report.errors.append(f"cross-project memory index is unreadable: {error}")
        else:
            lines = len(content.splitlines())
            size = len(content.encode("utf-8"))
            # Named separately so the error says which bound was crossed. Reporting both budgets
            # against one breach reads as though the file were over on both counts, which sends the
            # reader looking for bytes to cut when the file is merely long, or the reverse.
            exceeded = []
            if lines > MEMORY_INDEX_MAX_LINES:
                exceeded.append(f"{lines} lines, above the {MEMORY_INDEX_MAX_LINES} allowed")
            if size > MEMORY_INDEX_MAX_BYTES:
                exceeded.append(f"{size} bytes, above the {MEMORY_INDEX_MAX_BYTES} allowed")
            if exceeded:
                report.errors.append(
                    f"{index_path} is {' and '.join(exceeded)}; this file is read in full every "
                    "session, so merge or retire topics rather than appending pointers"
                )

    cited = {source for topic in topics for source in topic.sources}
    if cited:
        present = {child.name for child in workspace_root.iterdir() if child.is_dir()}
        dangling = sorted(cited - present)
        if dangling:
            report.warnings.append(
                f"{directory} cites source projects absent from the workspace: "
                f"{', '.join(dangling)}; repair the provenance or retire those topics"
            )

    if check_index:
        # Both memory files are checked the same way and for the same reason: a stale one is a
        # reader following a pointer to something that is no longer true.
        for path, expected in (
            (index_path, render_memory_index(workspace_root)),
            (postmortem_index_path(workspace_root), render_postmortem_index(workspace_root)),
        ):
            actual = ""
            if path.is_file():
                try:
                    actual = read_text(path)
                except WorkspaceError:
                    actual = ""
            if actual != expected:
                report.errors.append(f"derived cross-project memory index is stale: {path}")
    return report


def search_memory(workspace_root: Path, query: str) -> "list[tuple[Path, int, str]]":
    """Find `query` in topic frontmatter first, then in per-project post-mortems.

    Returns locations rather than contents — `(path, line number, line)` — so a wide search costs
    the caller in proportion to the number of hits rather than the size of what was hit, and the
    decision about what to actually load stays with the reader.

    Frontmatter before bodies because a frontmatter hit means the topic is *about* the query, while
    a body hit may only mention it in passing.
    """
    if not query.strip():
        raise WorkspaceError("search-memory needs a non-empty query")
    needle = query.strip().lower()
    hits: list[tuple[Path, int, str]] = []
    for path in _memory_topic_paths(workspace_root):
        try:
            content = read_text(path)
        except WorkspaceError:
            continue
        lines = content.splitlines()
        end = len(lines)
        if lines and lines[0].strip() == MEMORY_FRONTMATTER_DELIMITER:
            end = 1
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == MEMORY_FRONTMATTER_DELIMITER:
                    end = index
                    break
        for number, line in enumerate(lines[:end], start=1):
            if needle in line.lower():
                hits.append((path, number, line.strip()))
    if workspace_root.is_dir():
        for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
            postmortem = child / "reflection.md"
            if not child.is_dir() or child.name.startswith(".") or not postmortem.is_file():
                continue
            try:
                content = read_text(postmortem)
            except WorkspaceError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    hits.append((postmortem, number, line.strip()))
    return hits


def validate_project(
    project_dir: Path,
    *,
    close: bool = False,
    check_index: bool = False,
    check_report: bool = False,
    allow_legacy_close: bool = False,
    report_format: str | None = None,
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
        elif version == 4:
            report = validate_v4_state(load_json(project_dir / "project.json"), project_dir, close=close)
        else:
            report = ValidationReport(errors=[f"unsupported schema_version: {version}"])
    except WorkspaceError as error:
        return ValidationReport(errors=[str(error)])
    report.warnings.extend(reflection_warnings(project_dir.parent))
    report.warnings.extend(vcs_warnings(project_dir.parent))
    # The memory layer is validated from any project in the root, because it is the root's state and
    # a project is the only thing anyone validates. Its errors are reachable here and nowhere on the
    # commit path: `commit_candidate` validates the candidate state with `validate_v3_state`, so a
    # malformed topic file can never refuse to record work that is already finished.
    report.extend(memory_findings(project_dir.parent, check_index=check_index))
    if close:
        report.warnings.extend(memory_staging_warnings(project_dir))
    # Opt-in, and additive: it adds the mechanical report check to whatever mode was asked for rather
    # than replacing it, so `--close --report` is one run and `--report` alone still validates the
    # project around the report. Errors, not warnings, because this is the check the closure step runs
    # through `record-evidence` for an exit code.
    # Explicit format selection enables the check itself; legacy check_report requests both.
    if check_report or report_format is not None:
        report.extend(report_findings(project_dir, report_format if report_format is not None else "both"))
    if check_index and version in {2, 3, 4}:
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


EVIDENCE_TAIL_LINES = 20
EVIDENCE_PLACEHOLDER = "No task evidence recorded yet.\n"
EVIDENCE_HEADING_MAX_CHARS = 100


def _fence_for(text: str) -> str:
    """Return a backtick fence long enough to contain `text`.

    A command or an output tail may itself contain a fence. CommonMark closes a fenced block on the
    first line whose fence is at least as long as the opening one, so a three-backtick fence around
    text containing three backticks ends the block early and lets the rest of the entry render as
    prose. The fence has to be longer than anything inside it.
    """
    longest = 0
    run = 0
    for character in text:
        run = run + 1 if character == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _evidence_heading_command(joined: str) -> str:
    """Reduce a command to one short line for a Markdown heading.

    A heading is a single line by construction, so a command containing newlines — a `python3 -c`
    script, a shell one-liner with a `for` loop — silently spills its remainder into the document as
    prose. Long commands are no better: a 400-character heading is unreadable in a rendered file and
    in a table of contents. Collapse whitespace, then truncate. The full command is written into the
    entry body whenever this changes it, so nothing is lost.
    """
    collapsed = " ".join(joined.split())
    if len(collapsed) <= EVIDENCE_HEADING_MAX_CHARS:
        return collapsed
    return collapsed[: EVIDENCE_HEADING_MAX_CHARS - 1].rstrip() + "\u2026"


def _format_output_tail(stream: str, label: str, tail_lines: int) -> list[str]:
    text = stream.strip("\n")
    if not text:
        return []
    lines = text.split("\n")
    elided = len(lines) - tail_lines
    kept = lines[-tail_lines:] if elided > 0 else lines
    # Sized for the same reason the command block is: a command that prints a fence would
    # otherwise close this block early, and everything below it in the entry — the exit code of the
    # next entry included — would render as prose.
    fence = _fence_for(text)
    body = [f"{label}:", "", fence]
    if elided > 0:
        body.append(f"[{elided} earlier line(s) elided]")
    body.extend(kept)
    body.extend([fence, ""])
    return body


# Operators that only mean anything to a shell. Recognised as whole argv elements rather than as
# substrings: a quoted argument containing one — a `-c` program, a regex, a commit message — is
# ordinary data and must keep working, while a standalone one can only have come from a caller who
# expected a shell to be there.
SHELL_OPERATORS = ("|", "||", "&&", ";", ">", ">>")


def _shell_operator_arguments(command: Sequence[str]) -> "list[tuple[int, str]]":
    """Argv elements that are bare shell operators, with their 1-based positions."""
    return [(position, argument) for position, argument in enumerate(command, start=1) if argument in SHELL_OPERATORS]


def _refuse_shell_operators(command: Sequence[str]) -> None:
    """Refuse an argv that was written as if a shell would interpret it.

    `record-evidence` runs the command with `shell=False`, so a `|` in argv is handed to the program
    as a literal argument. The result is not an error message about the pipeline: it is whatever that
    program makes of an unexpected argument, which has read as the verification itself failing. The
    refusal names the wrapper that does work, because the fix is not obvious from the symptom.
    """
    operators = _shell_operator_arguments(command)
    if not operators:
        return
    found = "\n".join(f"  argv element #{position}: {argument!r}" for position, argument in operators)
    pipeline = " ".join(command)
    raise WorkspaceError(
        "record-evidence runs the command directly, with no shell, so these arguments are passed "
        f"through as literal text instead of composing a pipeline:\n{found}\n"
        "Ask for a shell explicitly instead:\n"
        f"  -- bash -lc {shlex.quote(pipeline)}"
    )


def _misrooted_command_arguments(
    command: Sequence[str], project_dir: Path, working_directory: Path
) -> list[tuple[str, Path]]:
    """Arguments that name a real file under the project directory and nothing under the target.

    `record-evidence` runs the command in `working_directory`, which is the target repository, not
    the project directory. A relative path to something inside the project — a script in
    `artifacts/`, the project's own `spec.md` — therefore resolves to nothing, and the command fails
    for a reason that looks like the command's fault. It is the most repeated mistake in this
    workspace's history, and it survived being written down, so it is detected here instead.

    Only an argument that resolves under the project directory *and* not under the target is
    reported: one that resolves under the target is what the caller asked for, and one that resolves
    under neither is the command's own problem to report.
    """
    misrooted: list[tuple[str, Path]] = []
    for argument in command:
        if not argument or argument.startswith("-") or Path(argument).is_absolute():
            continue
        if ".." in Path(argument).parts:
            continue
        in_project = project_dir / argument
        try:
            # An argument is far more often prose than a path — a `-c` program, a commit message, a
            # regex. Probing those raises rather than returning False, so a failed probe means "not
            # a path I can reason about" and the argument is left to the command.
            if in_project.exists() and not (working_directory / argument).exists():
                misrooted.append((argument, in_project))
        except (OSError, ValueError):
            continue
    return misrooted


def record_evidence(
    project_dir: Path,
    task_id: "str | None",
    command: Sequence[str],
    *,
    step: "str | None" = None,
    tail_lines: int = EVIDENCE_TAIL_LINES,
    timeout: float | None = None,
    lock_timeout: float = 5.0,
) -> int:
    """Run `command`, append what it actually did to `evidence.md`, and return its exit code.

    The point of this function is that the recorded result cannot disagree with the run. Evidence
    written from recollection is how a project came to claim that `git log --follow` had reached
    pre-rename history when the command had in fact returned nothing. So the entry is composed from
    the completed process: its real exit code, and a tail of its real output.

    The command is executed exactly as given, with no shell, so nothing here can expand, quote, or
    compose it. A non-zero exit is still recorded — a failure is evidence too — but it is recorded
    as a failure and returned as one, so a caller cannot mark a task done on the strength of it.
    Canonical state is never touched: transitions stay with `commit_candidate` and its revision check.

    Evidence belongs to a task or to a closure step, never both and never neither. A task id must
    exist in `project.json`; a step name must come from `CLOSURE_STEPS`. Both are closed sets, so
    neither route can open a heading for something that does not exist.
    """
    project_dir = project_dir.resolve()
    if task_id is not None and step is not None:
        raise WorkspaceError("record-evidence takes --task or --step, not both")
    if task_id is None and step is None:
        raise WorkspaceError("record-evidence needs --task <id> or --step <name>")
    if step is not None and step not in CLOSURE_STEPS:
        allowed = ", ".join(CLOSURE_STEPS)
        raise WorkspaceError(f"unknown closure step {step!r}; the closure steps are: {allowed}")
    if not command:
        raise WorkspaceError("record-evidence requires a command to run after '--'")
    # Before the project is even read: an argv holding a bare operator is malformed whatever the
    # project says, and reporting that beats reporting whatever the command made of it.
    _refuse_shell_operators(command)

    state = load_json(project_dir / "project.json")
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        raise WorkspaceError(f"{project_dir / 'project.json'} has no task list to record against")
    if task_id is not None and not any(isinstance(task, dict) and task.get("id") == task_id for task in tasks):
        known = ", ".join(str(task.get("id")) for task in tasks if isinstance(task, dict) and task.get("id"))
        raise WorkspaceError(f"unknown task {task_id!r}; this project has: {known or '(none)'}")

    working_directory = state.get("working_directory")
    if not _non_empty_string(working_directory) or not Path(working_directory).is_dir():
        raise WorkspaceError(f"working_directory is not an existing directory: {working_directory!r}")

    misrooted = _misrooted_command_arguments(command, project_dir, Path(working_directory))
    if misrooted:
        details = "\n".join(
            f"  {argument!r} does not exist in the working directory, but does exist at {resolved}"
            for argument, resolved in misrooted
        )
        raise WorkspaceError(
            "record-evidence runs the command in the project's working directory, not in the "
            f"project directory:\n  working directory: {working_directory}\n{details}\n"
            "Pass an absolute path for anything inside the project directory."
        )

    try:
        completed = subprocess.run(
            list(command),
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as error:
        raise WorkspaceError(f"cannot run {command[0]!r}: {error}") from error
    except OSError as error:
        raise WorkspaceError(f"cannot run {shlex.join(command)}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise WorkspaceError(f"command timed out after {timeout}s: {shlex.join(command)}") from error

    outcome = "passed" if completed.returncode == 0 else "FAILED"
    joined = shlex.join(command)
    heading = _evidence_heading_command(joined)
    lines = [
        "",
        f"## {task_id or step} — {heading}",
        "",
        f"- Recorded: {now_iso()}",
        f"- Working directory: {working_directory}",
        f"- Exit code: {completed.returncode} ({outcome})",
        "",
    ]
    if heading != joined:
        # The heading is now a summary rather than the command, so the command itself has to appear
        # somewhere a reader can copy it from.
        fence = _fence_for(joined)
        lines.extend(["Command:", "", fence, joined, fence, ""])
    lines.extend(_format_output_tail(completed.stdout, "stdout (tail)", tail_lines))
    lines.extend(_format_output_tail(completed.stderr, "stderr (tail)", tail_lines))
    if not completed.stdout.strip() and not completed.stderr.strip():
        lines.extend(["No output.", ""])

    _append_evidence_entry(project_dir, lines, lock_timeout=lock_timeout)
    return completed.returncode


def _append_evidence_entry(project_dir: Path, lines: "list[str]", *, lock_timeout: float) -> None:
    """Add one entry to `evidence.md` under the project lock, replacing the file in one step.

    Read-modify-write, which is what appending an entry is, needs both halves of this. The lock is
    the project lock rather than a lock of its own, because a commit validates the evidence files it
    is about to accept and must not see a half-written one. And the write is atomic because the
    previous form truncated `evidence.md` before writing it: an interruption there — a full disk, a
    killed process — left the record of every earlier task destroyed by the recording of this one.
    """
    evidence_path = project_dir / "evidence.md"
    try:
        with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
            existing = read_text(evidence_path) if evidence_path.exists() else "# Evidence\n"
            # The skeleton's placeholder would otherwise sit above real entries, saying the opposite
            # of what the file now holds.
            existing = existing.replace(EVIDENCE_PLACEHOLDER, "")
            atomic_write_text(evidence_path, existing.rstrip("\n") + "\n" + "\n".join(lines))
    except OSError as error:
        raise WorkspaceError(f"cannot write {evidence_path}: {error}") from error


# The task graph as something presentable, and the console rendering the lifecycle shows before
# execution starts. Both the pre-execution summary and the closing report's graph subsection are
# built from `build_task_graph` rather than from two independent readings of `project.json`: this
# plugin has twice shipped one rule implemented twice — a section list duplicated between a document
# and a tuple, and a slug rule that diverged across 27 headings — and both times the copies drifted
# before anyone noticed.
#
# The span half reads `evidence.md`, immediately above, because that file is the only per-task
# temporal record the schema has. `TASK_FIELDS` holds no start, no end, and no duration, so a task's
# span is derived from the stamps `record_evidence` wrote and is a lower bound on the work: it
# measures verification, and only for tasks whose verification was recorded at all.

# The level given to a task the layering could not place, which happens only when it is inside a
# dependency cycle or behind one. `_check_dependencies` rejects a cycle at commit time, so canonical
# state never holds one — but `show-graph` reads a file that may have been edited by hand, and a
# renderer that raises on the state a user most needs to look at is a renderer that quits when asked
# to do its job.
TASK_GRAPH_UNLEVELLED = -1

# Wide enough for a descriptive task name, narrow enough that the table still fits an 80-column
# terminal alongside every other column.
TASK_NAME_DISPLAY_WIDTH = 52
DEPENDENCY_DISPLAY_WIDTH = 13


@dataclass
class EvidenceSpan:
    """What `evidence.md` records about one task, reduced to what a summary can show."""

    entries: int = 0
    failed: int = 0
    first: "datetime | None" = None
    last: "datetime | None" = None
    # Entries whose `- Recorded:` stamp could not be placed on a timeline. Counted rather than
    # ignored, so a task with three unreadable stamps renders as unmeasured-with-entries instead of
    # looking like a task nobody ever ran.
    unplaceable: int = 0

    @property
    def measured(self) -> bool:
        return self.first is not None and self.last is not None

    @property
    def seconds(self) -> "float | None":
        """The span in seconds, or None when nothing placeable was recorded."""
        if self.first is None or self.last is None:
            return None
        return (self.last - self.first).total_seconds()


@dataclass
class TaskGraphNode:
    """One task, as the two presentations need it: structure, plan facts, and outcome."""

    id: str
    name: str
    status: str
    level: int
    depends_on: "list[str]"
    unknown_depends_on: "list[str]"
    effect_kind: str
    authorization_status: str
    span: EvidenceSpan
    receipts: int

    @property
    def needs_authorization(self) -> bool:
        """Whether this task cannot proceed on the authorization it currently holds."""
        return self.authorization_status in {"pending", "denied", "deferred"}


@dataclass
class TaskGraph:
    """A project's tasks in topological order, each carrying its dependency level."""

    project: str
    status: str
    nodes: "list[TaskGraphNode]"
    # Tasks the layering could not place. Empty for any state that passed `commit`.
    cycle_members: "list[str]"

    @property
    def levels(self) -> int:
        return len({node.level for node in self.nodes if node.level != TASK_GRAPH_UNLEVELLED})

    @property
    def executed(self) -> bool:
        """Whether there is anything to show about execution, rather than only about the plan.

        This is what lets one command serve both moments instead of taking a mode flag. A plan whose
        tasks are all still `TODO` has no outcome to report, and a project part-way through has one
        for the tasks that have moved.
        """
        return any(node.status != "TODO" for node in self.nodes)

    def effect_counts(self) -> "dict[str, int]":
        counts: "dict[str, int]" = {}
        for node in self.nodes:
            counts[node.effect_kind] = counts.get(node.effect_kind, 0) + 1
        return counts

    def measured_bounds(self) -> "tuple[datetime | None, datetime | None]":
        """The first and last placeable instant across every task, or a pair of Nones."""
        instants = [node.span.first for node in self.nodes if node.span.first is not None]
        instants += [node.span.last for node in self.nodes if node.span.last is not None]
        return (min(instants), max(instants)) if instants else (None, None)


# The heading `record_evidence` writes, read back. Both halves live in this module so the format has
# one owner: an entry is `## <task id or closure step> — <command>`, and the em dash is what
# separates an owner from a command that may itself contain spaces.
_EVIDENCE_ENTRY_HEADING = re.compile(r"^##[ \t]+(?P<owner>\S+)[ \t]+—", re.MULTILINE)
_EVIDENCE_RECORDED_STAMP = re.compile(r"^-[ \t]+Recorded:[ \t]*(?P<stamp>.+?)[ \t]*$", re.MULTILINE)
_EVIDENCE_EXIT_CODE = re.compile(r"^-[ \t]+Exit code:[ \t]*(?P<code>-?\d+)\b", re.MULTILINE)


def _placeable_instant(value: str) -> "datetime | None":
    """One recorded stamp as an instant, or None when it cannot be placed on a timeline.

    `record_evidence` writes a timezone-aware ISO-8601 stamp and nothing else, but the workspace this
    ships for contains two entries carrying a date alone — hand-authored, which the skill forbids and
    which no parser gets to assume away. A stamp with no offset is refused rather than assumed to be
    local time, for two reasons: subtracting a naive datetime from an aware one is a `TypeError` that
    surfaces far from its cause, and inventing an offset would place a task at a confidently wrong
    hour, which is worse than declining to place it at all.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_evidence_spans(evidence_markdown: str) -> "dict[str, EvidenceSpan]":
    """Reduce `evidence.md` to one span per owner: first and last instant, entries, failures.

    Keyed by whatever the heading names, which is a task id for a task and a closure step otherwise,
    so a caller that only knows about tasks simply finds no entry for `report`. Nothing here rejects
    an unknown owner: this is a reader of a file that is appended to by hand as well as by tool, and
    its job is to report what is there.
    """
    spans: "dict[str, EvidenceSpan]" = {}
    headings = list(_EVIDENCE_ENTRY_HEADING.finditer(evidence_markdown))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(evidence_markdown)
        body = evidence_markdown[heading.end() : end]
        span = spans.setdefault(heading.group("owner"), EvidenceSpan())
        span.entries += 1
        exit_code = _EVIDENCE_EXIT_CODE.search(body)
        if exit_code is not None and int(exit_code.group("code")) != 0:
            span.failed += 1
        stamp = _EVIDENCE_RECORDED_STAMP.search(body)
        instant = _placeable_instant(stamp.group("stamp")) if stamp is not None else None
        if instant is None:
            span.unplaceable += 1
            continue
        if span.first is None or instant < span.first:
            span.first = instant
        if span.last is None or instant > span.last:
            span.last = instant
    return spans


def _dependency_levels(dependencies: "dict[str, list[str]]") -> "dict[str, int]":
    """A dependency level per task: 0 with no prerequisite in the plan, else one past the deepest.

    Kahn's algorithm, so the level of every task is decided only by the edges and never by the order
    the tasks happen to appear in the file. A task missing from the result is in a cycle or behind
    one, which the caller reports rather than this raising: an unknown dependency is treated as
    already satisfied for layering, because a plan naming a task that does not exist is a plan whose
    shape is still worth showing next to that fact.
    """
    levels: "dict[str, int]" = {}
    remaining = {task_id: list(deps) for task_id, deps in dependencies.items()}
    while remaining:
        ready = sorted(task_id for task_id, deps in remaining.items() if all(dep not in remaining for dep in deps))
        if not ready:
            break
        for task_id in ready:
            depths = [levels[dep] for dep in remaining.pop(task_id) if dep in levels]
            levels[task_id] = max(depths) + 1 if depths else 0
    return levels


def build_task_graph(state: dict[str, Any], evidence_markdown: str = "") -> TaskGraph:
    """Turn canonical state and its evidence file into one graph both presentations render.

    Order is `(level, id)`, which is a topological order because a dependency's level is always
    strictly lower than its dependent's, and which depends on nothing but the task ids — so the same
    plan renders identically however its tasks are arranged in the file. Tasks the layering could not
    place sort last, where the renderer marks them.
    """
    spans = parse_evidence_spans(evidence_markdown)
    tasks = [task for task in state.get("tasks", []) if isinstance(task, dict)]
    dependencies = {
        str(task.get("id")): [str(dep) for dep in task.get("depends_on") or [] if _non_empty_string(dep)]
        for task in tasks
        if _non_empty_string(task.get("id"))
    }
    levels = _dependency_levels(dependencies)

    nodes = []
    for task in tasks:
        task_id = task.get("id")
        if not _non_empty_string(task_id):
            continue
        declared = dependencies[task_id]
        effect = task.get("effect") if isinstance(task.get("effect"), dict) else {}
        authorization = task.get("authorization") if isinstance(task.get("authorization"), dict) else {}
        receipts = task.get("receipts")
        nodes.append(
            TaskGraphNode(
                id=task_id,
                name=str(task.get("name") or ""),
                status=str(task.get("status") or ""),
                level=levels.get(task_id, TASK_GRAPH_UNLEVELLED),
                depends_on=declared,
                unknown_depends_on=[dep for dep in declared if dep not in dependencies],
                effect_kind=str(effect.get("kind") or ""),
                authorization_status=str(authorization.get("status") or ""),
                span=spans.get(task_id, EvidenceSpan()),
                receipts=len(receipts) if isinstance(receipts, list) else 0,
            )
        )

    nodes.sort(key=lambda node: (node.level == TASK_GRAPH_UNLEVELLED, node.level, node.id))
    return TaskGraph(
        project=str(state.get("project") or ""),
        status=str(state.get("status") or ""),
        nodes=nodes,
        cycle_members=sorted(task_id for task_id in dependencies if task_id not in levels),
    )


def _truncate(value: str, width: int) -> str:
    """`value` in at most `width` characters, marking that something was cut."""
    return value if len(value) <= width else value[: width - 3].rstrip() + "..."


def _wrap_dependencies(dependencies: "list[str]", width: int) -> "list[str]":
    """A dependency list as one or more lines no wider than `width`, splitting between ids.

    Returns a single "-" line for a task with no dependencies, so the column is never blank in a way
    a reader could mistake for a continuation line. A single id longer than `width` is left over-wide
    rather than cut: a truncated task id is a wrong task id.
    """
    if not dependencies:
        return ["-"]
    lines = [""]
    for position, dependency in enumerate(dependencies):
        piece = dependency + ("," if position < len(dependencies) - 1 else "")
        if lines[-1] and len(lines[-1]) + len(piece) > width:
            lines.append(piece)
        else:
            lines[-1] += piece
    return lines


def _format_duration(seconds: float) -> str:
    """A span in the coarsest unit that still says something, since these run minutes to days."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _render_table(headers: "list[str]", rows: "list[list[str]]", right_aligned: "set[int]") -> "list[str]":
    """Fixed-width columns, two spaces apart, computed from the content they have to hold."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = []
    for row in [headers, *rows]:
        cells = [
            cell.rjust(widths[index]) if index in right_aligned else cell.ljust(widths[index])
            for index, cell in enumerate(row)
        ]
        lines.append(("  " + "  ".join(cells)).rstrip())
    return lines


def render_task_graph(graph: TaskGraph) -> str:
    """The console form: a summary line, a flat topological table, then a warnings block.

    One rendering serves both moments the skill needs it at. Before execution the table describes a
    plan; once anything has run it grows a `Status` column and the summary grows the measured span,
    which is why this takes no mode flag — the state already says which is meaningful.

    The warnings block exists because a column cannot carry urgency. An authorization still `pending`
    on row 12 of 62 is a fact with consequences, and a reader scanning a wide table will miss it;
    naming those tasks again underneath costs three lines and is the whole reason the pre-execution
    summary is worth printing at all.
    """
    executed = graph.executed
    headers = ["#", "ID", "Task", "Lv", "Deps"]
    if executed:
        headers.append("Status")
    headers.extend(["Effect", "Auth"])
    status_column = headers.index("Status") if executed else None
    dependency_column = headers.index("Deps")

    rows: "list[list[str]]" = []
    for position, node in enumerate(graph.nodes, start=1):
        dependency_lines = _wrap_dependencies(node.depends_on, DEPENDENCY_DISPLAY_WIDTH)
        row = [
            str(position),
            node.id,
            _truncate(node.name, TASK_NAME_DISPLAY_WIDTH) or "-",
            "?" if node.level == TASK_GRAPH_UNLEVELLED else str(node.level),
            dependency_lines[0],
        ]
        if status_column is not None:
            row.append(node.status or "-")
        row.extend(
            [
                node.effect_kind or "-",
                "-" if node.authorization_status == "not_required" else node.authorization_status or "-",
            ]
        )
        rows.append(row)
        for continuation in dependency_lines[1:]:
            wrapped = [""] * len(headers)
            wrapped[dependency_column] = continuation
            rows.append(wrapped)

    effects = ", ".join(f"{kind or 'unset'} {count}" for kind, count in sorted(graph.effect_counts().items()))
    summary = [
        f"{graph.project or 'project'} | {graph.status or 'unknown'} | {len(graph.nodes)} tasks | "
        f"{graph.levels} levels | effects: {effects or 'none declared'}"
    ]
    first, last = graph.measured_bounds()
    if first is not None and last is not None:
        started = [node for node in graph.nodes if node.status in {"RUNNING", "DONE"}]
        unmeasured = [node for node in started if not node.span.measured]
        span = f"verification spans {first:%Y-%m-%d %H:%M} -> {last:%H:%M} ({_format_duration((last - first).total_seconds())})"
        if unmeasured:
            span += f" | {len(unmeasured)} of {len(started)} started tasks unmeasured"
        summary.append(span)

    lines = [*summary, ""]
    if rows:
        lines.extend(_render_table(headers, rows, right_aligned={0, headers.index("Lv")}))
    else:
        # A header row over nothing reads as a rendering fault rather than as a fact about the
        # project. A plan that does not exist yet is worth saying in words.
        lines.append("No tasks planned yet.")
    lines.extend(_task_graph_warnings(graph))
    # No trailing newline: the caller prints this, and a renderer that supplies its own would put a
    # blank line under every summary.
    return "\n".join(lines)


def _task_graph_warnings(graph: TaskGraph) -> "list[str]":
    """The block under the table: what a reader must not miss, named task by task.

    Authorization is reported even when nothing is outstanding. The absence of a pending
    authorization is a fact worth stating rather than a silence to interpret, and a reader who has
    learned that this block always speaks about authorization can trust it when it says nothing is
    waiting.
    """
    lines = []
    outstanding = [node for node in graph.nodes if node.needs_authorization]
    if outstanding:
        lines.extend(["", f"Authorization outstanding for {len(outstanding)} of {len(graph.nodes)} tasks:"])
        lines.extend(
            f"  {node.id}  authorization {node.authorization_status}, {node.effect_kind or 'unset'} effect: {node.name}"
            for node in outstanding
        )
    else:
        lines.extend(["", "Authorization: nothing outstanding."])

    failed = [node for node in graph.nodes if node.span.failed]
    if failed:
        lines.extend(["", "Non-zero exit codes recorded:"])
        lines.extend(
            f"  {node.id}  {node.span.failed} of {node.span.entries} recorded commands failed" for node in failed
        )

    unplaceable = [node for node in graph.nodes if node.span.unplaceable]
    if unplaceable:
        lines.extend(["", "Evidence entries that could not be placed on a timeline:"])
        lines.extend(
            f"  {node.id}  {node.span.unplaceable} of {node.span.entries} entries carry no timezone-aware stamp"
            for node in unplaceable
        )

    unknown = [node for node in graph.nodes if node.unknown_depends_on]
    if unknown:
        lines.extend(["", "Dependencies naming tasks that are not in the plan:"])
        lines.extend(f"  {node.id}  depends on {', '.join(node.unknown_depends_on)}" for node in unknown)

    if graph.cycle_members:
        lines.extend(
            [
                "",
                f"Not levelled, so inside a dependency cycle or behind one: {', '.join(graph.cycle_members)}",
            ]
        )
    return lines


def project_task_graph(project_dir: Path) -> TaskGraph:
    """The task graph of a project on disk. Reads two files and writes nothing.

    A missing `evidence.md` is not an error. The pre-execution summary exists precisely for the
    moment before anything has run, and a plan with no evidence renders every task as unmeasured,
    which is the truth about it.

    Refused for anything before schema v3, because the columns a reader would trust — effect kind,
    authorization status, dependency level — were introduced in v3 and remain present in v4. A v2
    project would render a table of `unset` and look like a finding rather than a schema mismatch.
    """
    directory = project_dir.resolve()
    if not directory.is_dir():
        raise WorkspaceError(f"project directory does not exist: {directory}")
    state = load_json(directory / "project.json")
    version = state.get("schema_version")
    if version not in (3, 4):
        raise WorkspaceError(
            f"{directory} is schema v{version!r}, and the task graph needs v3 or v4 fields; "
            "migrate it first with `research-project migrate`"
        )
    evidence_path = directory / "evidence.md"
    evidence = read_text(evidence_path) if evidence_path.exists() else ""
    return build_task_graph(state, evidence)


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
            # Every derived file is regenerated in full under the one lock. Rendering inside the
            # `with` rather than before it is what makes a concurrent rebuild harmless: the loser
            # reads a filesystem that already holds the winner's work and writes a superset of it.
            # `MEMORY.md` and `POSTMORTEMS.md` ride along here because they derive from the same
            # directory scan, and because a commit already ends by rebuilding, so memory stays
            # current for free.
            atomic_write_text(index_path, render_index(workspace_root))
            if (workspace_root / MEMORY_DIRECTORY).is_dir() or memory_index_path(workspace_root).is_file():
                atomic_write_text(memory_index_path(workspace_root), render_memory_index(workspace_root))
                # Written whenever `MEMORY.md` is, so the pointer in it never names a missing file.
                # An empty render still lands: a root that had post-mortems and lost them should end
                # up with an empty list rather than a stale one.
                atomic_write_text(
                    postmortem_index_path(workspace_root), render_postmortem_index(workspace_root)
                )
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


def _done_task_ids(state: dict[str, Any]) -> set[str]:
    """IDs of tasks already DONE in `state`, tolerating malformed entries."""
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        return set()
    return {
        task["id"]
        for task in tasks
        if isinstance(task, dict) and task.get("status") == "DONE" and _non_empty_string(task.get("id"))
    }


SCHEMA_V3_REQUIRED = "transactional commits require schema v3; migrate the project first"
SCHEMA_UNSUPPORTED = "project uses an unsupported schema version"
READER_ONLY_REFUSAL = (
    "this installation can read v4 projects but has no executor; "
    "mutating commands are refused while execution.attempts is non-empty or coordinator_run is set"
)
IMMUTABLE_PROJECT_FIELDS = ("schema_version", "project", "created")


def _revision_conflicts(current: dict[str, Any], candidate: dict[str, Any], expected_revision: int) -> "list[str]":
    """Every way `expected_revision` disagrees with the project or the candidate."""
    conflicts: list[str] = []
    if current.get("revision") != expected_revision:
        conflicts.append(
            f"revision conflict: expected {expected_revision}, found {current.get('revision')}; reload and reconcile"
        )
    if candidate.get("revision") != expected_revision:
        conflicts.append(
            f"candidate was built from revision {candidate.get('revision')}, expected {expected_revision}; "
            "reload and reconcile"
        )
    return conflicts


def _immutable_field_changes(current: dict[str, Any], candidate: dict[str, Any]) -> "list[str]":
    """Identity fields the candidate would rewrite, in declaration order."""
    return [
        f"candidate cannot change immutable field: {immutable}"
        for immutable in IMMUTABLE_PROJECT_FIELDS
        if candidate.get(immutable) != current.get(immutable)
    ]


def check_candidate(
    project_dir: Path,
    candidate_path: Path,
    *,
    expected_revision: "int | None" = None,
) -> ValidationReport:
    """Report everything a commit of `candidate_path` would object to, without committing it.

    `commit_candidate` raises on the first problem, which is correct for a transaction and
    expensive for a coordinator assembling a candidate by hand: a stale revision hides the
    transition error behind it, which hides the validation errors behind that, so one candidate
    costs three round trips to fix. This runs the same four checks and returns all their findings
    together.

    It deliberately takes no lock and writes nothing, so it is safe to run while another process
    holds the lock, and `revision` is exactly where it was afterwards. The revision it simulates
    is the one a commit would write, so a candidate this reports clean is a candidate that commits.
    """
    project_dir = project_dir.resolve()
    candidate = load_json(candidate_path.resolve())
    current = load_json(project_dir / "project.json")
    report = ValidationReport()
    schema_version = current.get("schema_version")
    if schema_version not in (3, 4):
        report.errors.append(SCHEMA_UNSUPPORTED)
        return report
    if schema_version == 4:
        curr_exec = current.get("execution")
        if not isinstance(curr_exec, dict):
            curr_exec = {}
        if curr_exec.get("attempts") or curr_exec.get("coordinator_run") is not None:
            report.errors.append(READER_ONLY_REFUSAL)
            return report
    if expected_revision is None:
        # Defaulting is for the dry run only: a real commit must state the revision it read, since
        # that claim is what makes the transaction detect a concurrent write.
        current_revision = current.get("revision")
        if not isinstance(current_revision, int) or isinstance(current_revision, bool) or current_revision < 0:
            report.errors.append(f"project.json holds an unusable revision: {current_revision!r}")
            return report
        expected_revision = current_revision
    report.errors.extend(_revision_conflicts(current, candidate, expected_revision))
    report.errors.extend(_immutable_field_changes(current, candidate))
    report.errors.extend(check_state_transition(current, candidate))
    simulated = copy.deepcopy(candidate)
    simulated["revision"] = expected_revision + 1
    simulated["updated"] = now_iso()
    validator = validate_v4_state if schema_version == 4 else validate_v3_state
    report.extend(
        validator(
            simulated,
            project_dir,
            close=simulated.get("status") == "DONE",
            check_files=True,
            already_done=_done_task_ids(current),
        )
    )
    return report


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
        schema_version = current.get("schema_version")
        if schema_version not in (3, 4):
            raise WorkspaceError(SCHEMA_UNSUPPORTED)
        if schema_version == 4:
            curr_exec = current.get("execution")
            if not isinstance(curr_exec, dict):
                curr_exec = {}
            if curr_exec.get("attempts") or curr_exec.get("coordinator_run") is not None:
                raise WorkspaceError(READER_ONLY_REFUSAL)
        conflicts = _revision_conflicts(current, candidate, expected_revision)
        if conflicts:
            raise WorkspaceConflict(conflicts[0])
        immutable_changes = _immutable_field_changes(current, candidate)
        if immutable_changes:
            raise WorkspaceError(immutable_changes[0])
        transition_errors = check_state_transition(current, candidate)
        if transition_errors:
            raise WorkspaceError("; ".join(transition_errors))
        candidate["revision"] = expected_revision + 1
        candidate["updated"] = now_iso()
        validator = validate_v4_state if schema_version == 4 else validate_v3_state
        report = validator(
            candidate,
            project_dir,
            close=candidate.get("status") == "DONE",
            check_files=True,
            already_done=_done_task_ids(current),
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
    briefing: bool = False,
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
            "schema_version": 4,
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
            "execution": {
                "protocol_version": 1,
                "coordinator_run": None,
                "ownership_generation": 0,
                "attempts": {},
            },
        }
        # project.json is the commit point: write the skeleton first so an interrupted init leaves
        # an inert directory that detect_schema does not recognise and render_index skips, rather
        # than a project that reports schema v4 while missing the files v4 requires.
        atomic_write_text(
            project_dir / "spec.md",
            f"# {title.strip()}\n\n## Current specification\n\nAlignment in progress.\n\n"
            "## Decision history\n\n- Project initialized; requirements pending alignment.\n",
        )
        if briefing:
            atomic_write_text(project_dir / "briefing.md", _briefing_skeleton(title.strip()))
        atomic_write_text(project_dir / "evidence.md", f"# Evidence\n\n{EVIDENCE_PLACEHOLDER}")
        atomic_write_text(project_dir / MEMORY_STAGING_FILENAME, MEMORY_STAGING_SKELETON)
        execution_config = _probe_execution_config(project_dir, legacy_writers_quiesced=False)
        atomic_write_json(project_dir / "execution" / "config.json", execution_config)
        # The flat cross-project `reflection.md` is no longer scaffolded. It is what the memory layer
        # replaces: one always-read file that grew to 61 KB while its guardrail, which counted
        # entries, still reported it clean. Existing ones stay valid and warn that they are legacy.
        # Only the directory is created here. `MEMORY.md` is derived, and the index rebuild that
        # ends this function generates it as soon as `memory/` exists — so writing it here too would
        # be a second writer of a generated file, which is how INDEX.md drift used to happen.
        (workspace_root / MEMORY_DIRECTORY).mkdir(exist_ok=True)
        report = validate_v4_state(state, project_dir, close=False, check_files=True)
        if report.errors:
            raise WorkspaceError("fresh v4 state validation failed:\n- " + "\n- ".join(report.errors))
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


def _probe_execution_config(project_dir: Path, *, legacy_writers_quiesced: bool) -> dict[str, Any]:
    """Probe execution prerequisites and return the immutable generation configuration."""
    scratch_dir = project_dir / "execution" / "scratch"
    tmp_dir = project_dir / "execution" / "tmp"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    probe_src = scratch_dir / "probe_src.tmp"
    probe_dst = scratch_dir / "probe_dst.tmp"
    probe_case_upper = scratch_dir / "probe_CASE.tmp"
    probe_case_lower = scratch_dir / "probe_case.tmp"

    atomic_link = False
    same_volume = False
    case_sensitive = True
    try:
        probe_src.write_bytes(b"")
        try:
            os.unlink(probe_dst)
        except OSError:
            pass
        try:
            os.link(probe_src, probe_dst)
            atomic_link = True
        except OSError:
            pass
        same_volume = os.stat(scratch_dir).st_dev == os.stat(tmp_dir).st_dev
        probe_case_upper.write_bytes(b"")
        case_sensitive = not probe_case_lower.exists()
    finally:
        for probe_path in (probe_src, probe_dst, probe_case_upper):
            try:
                os.unlink(probe_path)
            except OSError:
                pass
        try:
            os.rmdir(scratch_dir)
        except OSError:
            pass

    if not atomic_link:
        raise WorkspaceError(
            "R-NO-ATOMIC-LINK: the store's filesystem cannot link a file without clobbering an "
            "existing name; the parallel execution protocol is unavailable on this filesystem"
        )
    if not same_volume:
        raise WorkspaceError(
            "R-CROSS-VOLUME: the store's temporary directory is on a different volume from the "
            "execution store; the parallel execution protocol requires them on the same volume"
        )
    if os.name != "posix":
        raise WorkspaceError("R-NO-RUNNER: automatic execution requires a POSIX subprocess runner")
    import sys

    try:
        runner_probe = subprocess.run(
            [sys.executable, "-c", "pass"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"R-NO-RUNNER: local subprocess probe failed: {error}") from error
    if runner_probe.returncode != 0:
        raise WorkspaceError(f"R-NO-RUNNER: local subprocess probe exited with status {runner_probe.returncode}")

    return {
        "max_concurrent": 2,
        "max_prepare": 2,
        "project_lock_timeout": 5.0,
        "registry_lock_timeout": 5.0,
        "poll_interval": 2.0,
        "heartbeat_interval": 30,
        "stale_after": 900,
        "stale_grace": 900,
        "check_timeout": 1800,
        "max_reruns": 16,
        "max_consecutive_failures": 3,
        "log_cap": 1048576,
        "summary_cap": 4096,
        "tmp_reap": 3600,
        "atomic_link": atomic_link,
        "same_volume": same_volume,
        "case_sensitive": case_sensitive,
        "runner": "posix_subprocess",
        "legacy_writers_quiesced": legacy_writers_quiesced,
    }


def enable_execution(
    project_dir: Path,
    *,
    expected_revision: int,
    legacy_writers_quiesced: bool,
    lock_timeout: float = 5.0,
) -> None:
    project_dir = project_dir.resolve()
    state_path = project_dir / "project.json"
    config_path = project_dir / "execution" / "config.json"

    current = load_json(state_path)
    current_revision = current.get("revision")
    if current_revision != expected_revision:
        raise WorkspaceConflict(
            f"revision conflict: expected {expected_revision}, found {current_revision}; reload and reconcile"
        )

    if current.get("schema_version") == 4:
        if not config_path.exists():
            raise WorkspaceError(
                "project is schema v4 but execution/config.json is missing; "
                "the execution store may be corrupt — inspect manually"
            )
        generation: int = current.get("execution", {}).get("ownership_generation", 0)
        print(f"already enabled at generation {generation}")
        return

    if not legacy_writers_quiesced:
        raise WorkspaceError(
            "R-LEGACY-WRITER: --legacy-writers-quiesced is required; attest that every installation "
            "with write access to this workspace or working directory has been upgraded to schema v4"
        )

    expected_config = _probe_execution_config(project_dir, legacy_writers_quiesced=True)

    if config_path.exists():
        existing_config = load_json(config_path)
        if existing_config != expected_config:
            raise WorkspaceError(
                "config mismatch: execution/config.json exists but does not match what this call "
                "would write; inspect config.json and remove it if it is stale before re-running"
            )
    else:
        atomic_write_json(config_path, expected_config)

    # Commit schema 3 → 4 under the project lock, bypassing _immutable_field_changes.
    # This is the one permitted exception: enable_execution is the only path that may change
    # schema_version, and it takes the lock and writes the migration directly.
    with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
        current = load_json(state_path)
        if current.get("revision") != expected_revision:
            if current.get("schema_version") == 4:
                # A concurrent call committed the activation first (or a retry after signal).
                generation = current.get("execution", {}).get("ownership_generation", 0)
                print(f"already enabled at generation {generation}")
                return
            raise WorkspaceConflict(
                f"revision conflict: expected {expected_revision}, found {current.get('revision')}; "
                "reload and reconcile"
            )
        new_state = copy.deepcopy(current)
        new_state["schema_version"] = 4
        new_state["execution"] = {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        }
        new_state["revision"] = expected_revision + 1
        new_state["updated"] = now_iso()
        report = validate_v4_state(
            new_state,
            project_dir,
            close=new_state.get("status") == "DONE",
            check_files=True,
            already_done=_done_task_ids(current),
        )
        if report.errors:
            raise WorkspaceError("v4 state validation failed:\n- " + "\n- ".join(report.errors))
        atomic_write_json(state_path, new_state)
    _rebuild_index_after_commit(project_dir.parent, "schema v4 enabled at generation 0", lock_timeout)
