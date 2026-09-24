"""Guarded reads and deterministic edits for project-owned Markdown documents."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from workspace_lib import (
    SPEC_CANONICAL_SECTIONS,
    DirectoryLock,
    WorkspaceConflict,
    WorkspaceError,
    atomic_write_text,
    document_sha256,
    now_iso,
    read_text,
)
from workspace_session import _headings, _load_state

# Documents replaced whole under the content-token guard, as opposed to `spec`, which is edited by
# section, and `notes`, which are append-only.
WHOLE_DOCUMENTS = ("reflection", "architecture", "handoff")

ENTRY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ENTRY_MARKER_PATTERN = re.compile(r"^<!-- research-entry: (?P<id>[a-z0-9][a-z0-9-]{0,63}) -->$", re.MULTILINE)
ENTRY_BODY_PATTERN = re.compile(r"^<!-- research-body-sha256: (?P<digest>[0-9a-f]{64}) -->$", re.MULTILINE)


def _managed_path(project_dir: Path, document: str, task_id: str | None = None) -> Path:
    project_dir = project_dir.resolve()
    if document == "spec":
        path = project_dir / "spec.md"
    elif document == "reflection":
        path = project_dir / "reflection.md"
    elif document == "architecture":
        path = project_dir / "architecture.md"
    elif document == "handoff":
        path = project_dir / "handoff.md"
    elif document == "notes":
        if task_id is None or not ENTRY_ID_PATTERN.fullmatch(task_id.lower()):
            raise WorkspaceError("notes require a safe task ID")
        state = _load_state(project_dir)
        if not any(task["id"] == task_id for task in state["tasks"]):
            raise WorkspaceError(f"unknown task: {task_id}")
        path = project_dir / "tasks" / f"{task_id}.md"
    else:
        raise WorkspaceError("document must be spec, reflection, architecture, handoff, or notes")
    try:
        path.resolve(strict=False).relative_to(project_dir)
    except (OSError, ValueError) as error:
        raise WorkspaceError(f"managed document escapes the project: {path}") from error
    return path


def document_snapshot(
    project_dir: Path,
    document: str,
    *,
    task_id: str | None = None,
    outline: bool = False,
    offset: int = 0,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Read a bounded managed document and return its whole-document content token."""
    if offset < 0 or not 1 <= max_chars <= 20000:
        raise WorkspaceError("offset must be nonnegative; max-chars must be between 1 and 20000")
    project_dir = project_dir.resolve()
    state = _load_state(project_dir)
    path = _managed_path(project_dir, document, task_id)
    if not path.exists():
        return {
            "path": str(path), "revision": state["revision"], "exists": False, "document_sha256": "missing",
            "text": "", "total_chars": 0, "offset": 0, "next_offset": None, "truncated": False,
        }
    text = read_text(path, preserve_newlines=True)
    if outline:
        rows = [{"level": level, "title": title} for level, title, _ in _headings(text)]
        return {
            "path": str(path), "revision": state["revision"], "exists": True,
            "document_sha256": document_sha256(text), "headings": rows,
        }
    if offset > len(text):
        raise WorkspaceError("offset is beyond the document; reload after source changes")
    end = min(len(text), offset + max_chars)
    return {
        "path": str(path), "revision": state["revision"], "exists": True,
        "document_sha256": document_sha256(text), "text": text[offset:end], "total_chars": len(text),
        "offset": offset, "next_offset": end if end < len(text) else None,
        "truncated": offset > 0 or end < len(text),
    }


def _line_end(text: str, offset: int) -> int:
    newline = text.find("\n", offset)
    return len(text) if newline < 0 else newline + 1


def _section_span(text: str, heading: str) -> tuple[int, int, int, int]:
    headings = _headings(text)
    matches = [(index, level, offset) for index, (level, title, offset) in enumerate(headings) if title == heading]
    if len(matches) != 1:
        available = ", ".join(title for _, title, _ in headings) or "(none)"
        raise WorkspaceError(f"section must match exactly one heading: {heading}; available: {available}")
    index, level, start = matches[0]
    body_start = _line_end(text, start)
    end = next((pos for depth, _, pos in headings[index + 1 :] if depth <= level), len(text))
    return level, start, body_start, end


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normal_body(body: str, newline: str) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return body.replace("\n", newline) + newline + newline


def _check_body_headings(body: str, selected_level: int) -> None:
    invalid = [title for level, title, _ in _headings(body) if level <= selected_level]
    if invalid:
        raise WorkspaceError("replacement body contains a heading at or above the selected level: " + invalid[0])


def _replace_sections(text: str, sections: dict[str, str]) -> str:
    if not sections:
        raise WorkspaceError("sections mapping must not be empty")
    newline = _newline(text)
    canonical = [name for name, _ in SPEC_CANONICAL_SECTIONS]
    headings = {title for _, title, _ in _headings(text)}
    if set(sections) == set(canonical) and not set(canonical) <= headings:
        level, _, body_start, end = _section_span(text, "Current specification")
        if level != 2:
            raise WorkspaceError("Current specification must be a level-two heading")
        rendered = "".join(
            f"### {heading}{newline}{newline}{_normal_body(sections[heading], newline)}" for heading in canonical
        )
        return text[:body_start] + newline + rendered + text[end:]
    spans: list[tuple[int, int, str]] = []
    for heading, body in sections.items():
        level, _, body_start, end = _section_span(text, heading)
        _check_body_headings(body, level)
        spans.append((body_start, end, _normal_body(body, newline)))
    spans.sort()
    for index in range(len(spans) - 1):
        _, left_end, _ = spans[index]
        right_start, _, _ = spans[index + 1]
        if right_start < left_end:
            raise WorkspaceError("section replacements overlap; select only parent or child headings")
    result = text
    for start, end, replacement in reversed(spans):
        result = result[:start] + replacement + result[end:]
    return result


def edit_document(
    project_dir: Path,
    document: str,
    *,
    expected_sha256: str,
    body: str | None = None,
    sections: dict[str, str] | None = None,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    """Replace a whole managed document, or selected specification bodies.

    Every write is guarded by the whole-document content token, so a stale draft cannot overwrite
    an edit made by another session.
    """
    if (body is None) == (sections is None):
        raise WorkspaceError("provide exactly one of body or sections")
    if document == "spec" and sections is None:
        raise WorkspaceError("spec edits require a sections mapping")
    if document in WHOLE_DOCUMENTS and body is None:
        raise WorkspaceError(f"{document} edits require a body")
    if document not in ("spec", *WHOLE_DOCUMENTS):
        raise WorkspaceError("editable document must be spec, reflection, architecture, or handoff")
    project_dir = project_dir.resolve()
    path = _managed_path(project_dir, document)
    try:
        with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
            state = _load_state(project_dir)
            if state.get("execution", {}).get("coordinator_run") or state.get("execution", {}).get("attempts"):
                raise WorkspaceError("project execution is active; resolve executor ownership before editing documents")
            exists = path.exists()
            current = read_text(path, preserve_newlines=True) if exists else ""
            actual = document_sha256(current) if exists else "missing"
            if actual != expected_sha256:
                raise WorkspaceConflict(f"document conflict: expected {expected_sha256}, found {actual}")
            if document in WHOLE_DOCUMENTS:
                replacement = (body or "").strip()
                if not replacement:
                    raise WorkspaceError(f"{document} body must not be empty")
                replacement += "\n"
            else:
                replacement = _replace_sections(current, sections or {})
            atomic_write_text(path, replacement)
    except OSError as error:
        raise WorkspaceError(f"cannot write {path}: {error}") from error
    return {
        "operation": "document.edit", "document": document, "path": str(path), "revision": state["revision"],
        "previous_sha256": actual, "document_sha256": document_sha256(replacement),
    }


def append_record(
    project_dir: Path,
    kind: str,
    body: str,
    *,
    task_id: str | None = None,
    entry_id: str | None = None,
    lock_timeout: float = 5.0,
) -> dict[str, Any]:
    """Append an idempotent decision or task finding under the project lock."""
    body = body.strip()
    if not body:
        raise WorkspaceError("record body must not be empty")
    if entry_id is None:
        entry_id = f"entry-{uuid.uuid4().hex}"
    if ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
        raise WorkspaceError("entry ID must contain only lowercase letters, digits, and hyphens")
    project_dir = project_dir.resolve()
    document = "spec" if kind == "decision" else "notes" if kind == "finding" else ""
    if not document:
        raise WorkspaceError("record kind must be decision or finding")
    path = _managed_path(project_dir, document, task_id)
    marker = f"<!-- research-entry: {entry_id} -->"
    body_digest = document_sha256(body)
    newline = "\n"
    try:
        with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
            state = _load_state(project_dir)
            if state.get("execution", {}).get("coordinator_run") or state.get("execution", {}).get("attempts"):
                raise WorkspaceError("project execution is active; resolve executor ownership before appending records")
            current = read_text(path, preserve_newlines=True) if path.exists() else f"# Task {task_id} notes\n"
            existing_ids = [match.group("id") for match in ENTRY_MARKER_PATTERN.finditer(current)]
            if entry_id in existing_ids:
                start = current.index(marker)
                end = current.find("\n<!-- research-entry: ", start + len(marker))
                existing = current[start:end if end >= 0 else len(current)]
                existing_digests = ENTRY_BODY_PATTERN.findall(existing)
                if existing_digests == [body_digest]:
                    return {
                        "operation": f"{kind}.append", "entry_id": entry_id, "path": str(path),
                        "anchor": entry_id, "document_sha256": document_sha256(current), "existing": True,
                    }
                raise WorkspaceConflict(f"entry ID already exists with different content: {entry_id}")
            stamp = now_iso()
            entry = (
                f"{marker}{newline}<!-- research-body-sha256: {body_digest} -->{newline}"
                f"<a id=\"{entry_id}\"></a>{newline}"
                f"### {stamp} — {entry_id}{newline}{newline}{body}{newline}"
            )
            if kind == "decision":
                _, _, _, end = _section_span(current, "Decision history")
                replacement = current[:end].rstrip("\r\n") + newline + newline + entry + current[end:]
            else:
                replacement = current.rstrip("\r\n") + newline + newline + entry
            atomic_write_text(path, replacement)
    except OSError as error:
        raise WorkspaceError(f"cannot write {path}: {error}") from error
    return {
        "operation": f"{kind}.append", "entry_id": entry_id, "path": str(path), "anchor": entry_id,
        "document_sha256": document_sha256(replacement), "existing": False,
    }
