"""Guarded reads and deterministic edits for project-owned Markdown documents."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from workspace_lib import (
    DESIGN_DOCUMENT,
    MEMORY_STAGING_FILENAME,
    MEMORY_STAGING_PLACEHOLDER,
    MEMORY_STAGING_SKELETON,
    SPEC_CANONICAL_SECTIONS,
    DirectoryLock,
    WorkspaceConflict,
    WorkspaceError,
    atomic_write_text,
    document_conflict_message,
    document_sha256,
    is_legacy_planning_layout,
    now_iso,
    planning_path,
    read_text,
    stale_confirmations,
)
from workspace_session import _headings, _load_state

# Documents replaced whole under the content-token guard, as opposed to `spec`, which is edited by
# section, and `notes`, which are append-only.
WHOLE_DOCUMENTS = ("reflection", "planning", "architecture", "handoff")
# What a project that still keeps its agreement in a root `architecture.md` is told when it names the
# design document: the old habit would otherwise write into, or read as missing, the wrong file.
LEGACY_PLANNING_HINT = (
    "this project keeps its agreement in a root architecture.md, which is the planning document; "
    "use the document id planning. The design document is " + DESIGN_DOCUMENT
)
FRICTION_FILENAME = "friction.md"
# What the tool or the skill cost the agent, in a fixed vocabulary so a retro can count it across
# projects. A finding is about the work; a friction entry is about the tool.
FRICTION_KINDS = (
    "unclear-instruction", "missing-information", "gate-refusal", "wasted-command", "reference-not-useful",
    "user-correction", "tool-defect",
)
FRICTION_POINTER_CHARS = 200
FRICTION_HEADER = "# Friction\n"
FRICTION_FIELD_PATTERN = re.compile(r"^(?P<name>Kind|Pointer): (?P<value>.*)$")
ENTRY_HEADING_PATTERN = re.compile(r"^### (?P<at>\S+) — (?P<id>[a-z0-9][a-z0-9-]{0,63})$", re.MULTILINE)

ENTRY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ENTRY_MARKER_PATTERN = re.compile(r"^<!-- research-entry: (?P<id>[a-z0-9][a-z0-9-]{0,63}) -->$", re.MULTILINE)
ENTRY_BODY_PATTERN = re.compile(r"^<!-- research-body-sha256: (?P<digest>[0-9a-f]{64}) -->$", re.MULTILINE)


def _managed_path(project_dir: Path, document: str, task_id: str | None = None) -> Path:
    project_dir = project_dir.resolve()
    if document == "spec":
        path = project_dir / "spec.md"
    elif document == "reflection":
        path = project_dir / "reflection.md"
    elif document == "planning":
        path = planning_path(project_dir)
    elif document == "architecture":
        path = project_dir / DESIGN_DOCUMENT
    elif document == "handoff":
        path = project_dir / "handoff.md"
    elif document == "friction":
        path = project_dir / FRICTION_FILENAME
    elif document == "notes":
        if task_id is None or not ENTRY_ID_PATTERN.fullmatch(task_id.lower()):
            raise WorkspaceError("notes require a safe task ID")
        state = _load_state(project_dir)
        if not any(task["id"] == task_id for task in state["tasks"]):
            raise WorkspaceError(f"unknown task: {task_id}")
        path = project_dir / "tasks" / f"{task_id}.md"
    else:
        raise WorkspaceError("document must be spec, reflection, planning, architecture, handoff, friction, or notes")
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
        missing = {
            "path": str(path), "revision": state["revision"], "exists": False, "document_sha256": "missing",
            "text": "", "total_chars": 0, "offset": 0, "next_offset": None, "truncated": False,
        }
        if document == "architecture" and is_legacy_planning_layout(project_dir):
            missing["hint"] = LEGACY_PLANNING_HINT
        return missing
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
        raise WorkspaceError("editable document must be spec, reflection, planning, architecture, or handoff")
    project_dir = project_dir.resolve()
    if document == "architecture" and is_legacy_planning_layout(project_dir):
        raise WorkspaceError(LEGACY_PLANNING_HINT)
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
                raise WorkspaceConflict(
                    document_conflict_message(document, expected_sha256, actual, project_dir, use="--expected-sha256")
                )
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
    result = {
        "operation": "document.edit", "document": document, "path": str(path), "revision": state["revision"],
        "previous_sha256": actual, "document_sha256": document_sha256(replacement),
    }
    if document in ("spec", "planning"):
        spec_path, planning_file = project_dir / "spec.md", planning_path(project_dir)
        spec_text = replacement if document == "spec" else (read_text(spec_path) if spec_path.is_file() else "")
        planning_text = (
            replacement
            if document == "planning"
            else (read_text(planning_file) if planning_file.is_file() else "")
        )
        stale = stale_confirmations(spec_text, planning_text)
        if stale:
            result["stale_confirmations"] = stale
    return result


def append_record(
    project_dir: Path,
    kind: str,
    body: str,
    *,
    task_id: str | None = None,
    entry_id: str | None = None,
    lock_timeout: float = 5.0,
    friction_kind: str | None = None,
    pointer: str | None = None,
) -> dict[str, Any]:
    """Append an idempotent decision, task finding or friction entry under the project lock."""
    body = body.strip()
    if not body:
        raise WorkspaceError("record body must not be empty")
    if kind == "friction":
        body = _friction_body(body, friction_kind, pointer)
    elif friction_kind is not None or pointer is not None:
        raise WorkspaceError("--kind and --pointer belong to a friction entry")
    if entry_id is None:
        entry_id = f"entry-{uuid.uuid4().hex}"
    if ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
        raise WorkspaceError("entry ID must contain only lowercase letters, digits, and hyphens")
    project_dir = project_dir.resolve()
    document = {"decision": "spec", "finding": "notes", "friction": "friction"}.get(kind, "")
    if not document:
        raise WorkspaceError("record kind must be decision, finding or friction")
    if kind == "friction" and task_id is not None:
        raise WorkspaceError("a friction entry belongs to the project, not to a task")
    path = _managed_path(project_dir, document, task_id)
    marker = f"<!-- research-entry: {entry_id} -->"
    body_digest = document_sha256(body)
    newline = "\n"
    try:
        with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
            state = _load_state(project_dir)
            if state.get("execution", {}).get("coordinator_run") or state.get("execution", {}).get("attempts"):
                raise WorkspaceError("project execution is active; resolve executor ownership before appending records")
            fresh = FRICTION_HEADER if kind == "friction" else f"# Task {task_id} notes\n"
            current = read_text(path, preserve_newlines=True) if path.exists() else fresh
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


def _friction_body(body: str, friction_kind: str | None, pointer: str | None) -> str:
    """The stored body of a friction entry: the two fixed lines, then the agent's sentences."""
    if friction_kind not in FRICTION_KINDS:
        raise WorkspaceError(f"friction kind must be one of: {', '.join(FRICTION_KINDS)}")
    pointer = (pointer or "").strip()
    if not pointer or "\n" in pointer or len(pointer) > FRICTION_POINTER_CHARS:
        raise WorkspaceError(
            f"a friction entry needs --pointer: one line of at most {FRICTION_POINTER_CHARS} characters naming the "
            "reference, command or record"
        )
    return f"Kind: {friction_kind}\nPointer: {pointer}\n\n{body}"


def friction_entries(text: str) -> list[dict[str, str]]:
    """Every friction entry in document order: id, at, kind, pointer, body.

    A hand-written entry without the two lines is kept with empty kind and pointer rather than
    dropped: the retro counts it as friction of unknown kind, which is still friction.
    """
    entries: list[dict[str, str]] = []
    markers = list(ENTRY_MARKER_PATTERN.finditer(text))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        chunk = text[marker.end():end]
        heading = ENTRY_HEADING_PATTERN.search(chunk)
        entry = {"id": marker.group("id"), "at": heading.group("at") if heading else "", "kind": "", "pointer": ""}
        rest = chunk[heading.end():] if heading else chunk
        lines = rest.strip("\n").split("\n")
        while lines:
            field = FRICTION_FIELD_PATTERN.match(lines[0])
            if field is None:
                break
            entry[field.group("name").lower()] = field.group("value").strip()
            lines.pop(0)
        entry["body"] = "\n".join(lines).strip()
        entries.append(entry)
    return entries


# Words a level-two reflection title must carry to be staged at close. Measured against the 20
# closed reflections on 2026-10-16: 20 of 82 headings across 13 projects, longest title 82
# characters, and no matched body containing a heading. `Outcome` and `What worked` carry none of
# them, so a close does not open with sections nobody wrote down as a lesson.
LESSON_TITLE_TOKENS = (
    "did not work",
    "went wrong",
    "got wrong",
    "failure",
    "cost time",
    "lesson",
    "differently",
    "wrong",
)


def reflection_lesson_sections(reflection: str) -> list[tuple[str, str]]:
    """The reflection's level-two sections whose title names a lesson, in document order.

    Candidate c3 asks finalize to hand these to staging so the triage gate decides what leaves the
    project. The rule is a vocabulary match rather than an exact title list: `Lessons` and `The
    failure mode that recurred four times` are lessons without repeating the words c3 quoted.
    """
    headings = _headings(reflection)
    sections: list[tuple[str, str]] = []
    for index, (level, title, offset) in enumerate(headings):
        if level != 2 or not any(token in title.lower() for token in LESSON_TITLE_TOKENS):
            continue
        end = next((position for depth, _, position in headings[index + 1 :] if depth <= level), len(reflection))
        sections.append((title.strip(), reflection[_line_end(reflection, offset) : end].strip()))
    return sections


def append_staged_section(content: str, title: str, body: str) -> str:
    """Return `content` with one staged lesson section appended, in the shape `workflow triage` selects.

    Triage selects an independent level-two section by its exact title, so the title and the body
    are held to that shape here. Empty content is a staging file that does not exist yet, and the
    scaffold is what its first section lands in.
    """
    title = title.strip()
    body = body.strip()
    if not title or "\n" in title or title.startswith("#") or len(title) > 120:
        raise WorkspaceError("a lesson title must be one non-empty line of at most 120 characters, without a leading #")
    if not body:
        raise WorkspaceError("a staged lesson needs a non-empty body")
    if _headings(body):
        raise WorkspaceError("a staged lesson may not contain headings: triage selects a section by its title alone")
    kept = (content or MEMORY_STAGING_SKELETON).replace(MEMORY_STAGING_PLACEHOLDER + "\n", "", 1).rstrip("\r\n")
    return f"{kept}\n\n## {title}\n\n{body}\n"


def stage_lesson(project_dir: Path, title: str, body: str, *, lock_timeout: float = 5.0) -> dict[str, Any]:
    """Add one candidate lesson to `memory-staging.md` as a section `workflow triage` can select.

    Nothing wrote this file but a hand edit, and a hand edit is a step that gets skipped: most
    projects closed with the skeleton untouched and rebuilt their lessons from memory at the end.
    Triage selects items by an exact, unique level-two title and refuses any heading below it, so
    the section is written in exactly that shape and the title and body are held to it here.
    """
    title = title.strip()
    body = body.strip()
    project_dir = project_dir.resolve()
    path = project_dir / MEMORY_STAGING_FILENAME
    try:
        with DirectoryLock(project_dir / ".project.lock", timeout=lock_timeout):
            current = read_text(path, preserve_newlines=True) if path.exists() else MEMORY_STAGING_SKELETON
            matches = [(level, offset) for level, name, offset in _headings(current) if name == title]
            if matches:
                if len(matches) != 1 or matches[0][0] != 2:
                    raise WorkspaceError(f"the title {title!r} collides with another heading in {path.name}")
                _, start, _, end = _section_span(current, title)
                if current[start:end].split("\n", 1)[1].strip() == body:
                    return {
                        "operation": "lesson.stage", "title": title, "path": str(path),
                        "document_sha256": document_sha256(current), "existing": True,
                    }
                raise WorkspaceConflict(
                    f"a staged lesson titled {title!r} already exists with different text; choose another title"
                )
            replacement = append_staged_section(current, title, body)
            atomic_write_text(path, replacement)
    except OSError as error:
        raise WorkspaceError(f"cannot write {path}: {error}") from error
    return {
        "operation": "lesson.stage", "title": title, "path": str(path),
        "document_sha256": document_sha256(replacement), "existing": False,
    }
