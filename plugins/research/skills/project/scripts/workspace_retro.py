"""Retro: the closed projects since the last retro, their records folded into signals, and the ledger.

A retro of the project skill asks what the skill and the tool cost the agent across projects. The
records that answer it are spread over a project directory: the invocation log, the friction entries,
the `## Retro` section of the reflection, the failed evidence. `retro_batch` folds them into one
bounded JSON document per project so the agent judges from counts and pointers, and opens a record
with `read` only where a pointer leads. The ledger under `<root>/retro/` is what makes the next run
start where this one stopped: it carries the cursor, the projects covered, and the candidates the user
rejected or deferred. This module reads project files; it writes only under the workspace root.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from workspace_documents import ENTRY_MARKER_PATTERN, FRICTION_FILENAME, friction_entries
from workspace_evidence import _entry_rows
from workspace_invocations import INVOCATIONS_FILENAME, summarize_invocations
from workspace_lib import (
    RETRO_SECTION_HEADING,
    WorkspaceError,
    _section_content,
    is_canonical_project_id,
    load_json,
    read_text,
    staged_lesson_lines,
)

RETRO_DIRECTORY = "retro"
RETRO_INDEX_FILENAME = "RETRO.md"
RETRO_BLOCK = re.compile(r"<!-- research-retro-v1 (?P<json>\{.*?\}) -->", re.DOTALL)
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TERMINAL = frozenset({"DONE", "CANCELLED"})
CATEGORIES = ("instructions", "gates", "tool-economy", "information-access", "workflow", "tool-defect")
SEVERITIES = ("high", "medium", "low")
DISPOSITIONS = ("accepted", "rejected", "deferred")
# Bounds on one project's row. A retro reads many projects at once; anything past these is reached
# through `read` with the pointer the row gives.
FRICTION_LIMIT = 40
FAILED_HEADS_LIMIT = 10
RETRO_SECTION_CHARS = 2000
PROJECT_LIMIT_MAX = 100
TRANSCRIPT_LIMIT = 3
HEAD_CHARS = 80


def parse_instant(text: str) -> datetime:
    """An ISO instant or date; a value without a zone is local time, a bare date is local midnight."""
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkspaceError(f"not an ISO date or instant: {text!r}") from error
    return moment.astimezone() if moment.tzinfo is None else moment


def _instant_or_none(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_instant(value)
    except WorkspaceError:
        return None


def previous_runs(root: Path) -> dict[str, Any]:
    """What earlier runs settled: the cursor, the projects covered, rejected and deferred candidates."""
    summary: dict[str, Any] = {
        "runs": 0, "run": None, "through": None, "covered": [], "rejected": [], "deferred": [], "unreadable": [],
    }
    directory = root / RETRO_DIRECTORY
    if not directory.is_dir():
        return summary
    covered: set[str] = set()
    through: "datetime | None" = None
    for path in sorted(directory.glob("*.md")):
        try:
            block = RETRO_BLOCK.search(read_text(path))
            run = json.loads(block.group("json")) if block else None
            if not isinstance(run, dict) or not isinstance(run.get("id"), str):
                raise ValueError("no retro block")
        except (WorkspaceError, ValueError):
            summary["unreadable"].append(path.name)
            continue
        summary["runs"] += 1
        summary["run"] = run["id"]
        covered.update(item for item in run.get("batch", []) if isinstance(item, str))
        moment = _instant_or_none(run.get("through"))
        if moment is not None and (through is None or moment > through):
            through, summary["through"] = moment, run["through"]
        for candidate in run.get("candidates", []):
            if not isinstance(candidate, dict) or candidate.get("disposition") not in ("rejected", "deferred"):
                continue
            summary[str(candidate["disposition"])].append(
                {"title": candidate.get("title"), "category": candidate.get("category"), "run": run["id"]}
            )
    summary["covered"] = sorted(covered)
    return summary


def next_run_id(root: Path, today: "str | None" = None) -> str:
    """The next `YYYY-MM-DD-NNN` id under `retro/`, counted per day like project ids."""
    day = today or datetime.now().astimezone().date().isoformat()
    directory = root / RETRO_DIRECTORY
    taken = {path.stem for path in directory.glob(f"{day}-*.md")} if directory.is_dir() else set()
    number = 1
    while f"{day}-{number:03d}" in taken:
        number += 1
    return f"{day}-{number:03d}"


def _load_project(directory: Path) -> "tuple[dict[str, Any] | None, str]":
    try:
        state = load_json(directory / "project.json")
    except WorkspaceError as error:
        return None, str(error)
    return state, ""


def select_batch(
    root: Path, *, since: "str | None" = None, projects: "list[str] | None" = None
) -> dict[str, Any]:
    """Which closed projects this run covers, and which active ones are in range but not ready."""
    previous = previous_runs(root)
    if projects:
        for name in projects:
            if not is_canonical_project_id(name):
                raise WorkspaceError(f"not a project id: {name!r}")
            if not (root / name).is_dir():
                raise WorkspaceError(f"no such project under the root: {name}")
    cursor = parse_instant(since) if since else _instant_or_none(previous["through"])
    selection = {
        "since": since, "projects": list(projects) if projects else None, "closed_only": True,
        "cursor": cursor.isoformat() if cursor else None,
    }
    rows: list[dict[str, Any]] = []
    pending: list[str] = []
    names = list(projects) if projects else sorted(name for name in _project_names(root))
    for name in names:
        state, error = _load_project(root / name)
        if state is None:
            # A project whose record cannot be read is itself a finding for the retro: always listed.
            rows.append({"project": name, "unreadable": True, "error": error})
            continue
        status = state.get("status")
        updated = _instant_or_none(state.get("updated"))
        if not projects:
            if name in previous["covered"] and not since:
                continue
            if cursor is not None and (updated is None or updated <= cursor):
                continue
        if status in TERMINAL:
            rows.append({"project": name, "state": state})
        else:
            pending.append(name)
    # Oldest first, so the batch reads in the order the work happened; unreadable rows last.
    rows.sort(
        key=lambda row: (bool(row.get("unreadable")), str(row.get("state", {}).get("updated", "")), row["project"])
    )
    return {"previous": previous, "selection": selection, "rows": rows, "pending": pending}


def _project_names(root: Path) -> "list[str]":
    try:
        return [entry.name for entry in root.iterdir() if entry.is_dir() and is_canonical_project_id(entry.name)]
    except OSError as error:
        raise WorkspaceError(f"cannot list {root}: {error}") from error


def _text_or_empty(path: Path) -> str:
    try:
        return read_text(path) if path.is_file() else ""
    except WorkspaceError:
        return ""


def project_signals(root: Path, state: dict[str, Any], project_id: str) -> dict[str, Any]:
    """One project's row: identity, the three layers, evidence failures and counts, bounded."""
    directory = root / project_id
    reflection = _text_or_empty(directory / "reflection.md")
    section = _section_content(reflection, RETRO_SECTION_HEADING) if reflection else None
    friction = friction_entries(_text_or_empty(directory / FRICTION_FILENAME))
    evidence_rows = _entry_rows(_text_or_empty(directory / "evidence.md"))
    failed = [row for row in evidence_rows if row.get("exit_code") not in (0, None)]
    tasks: dict[str, int] = {}
    for task in state.get("tasks", []) if isinstance(state.get("tasks"), list) else []:
        status = str(task.get("status", "")) if isinstance(task, dict) else ""
        tasks[status] = tasks.get(status, 0) + 1
    layers = {
        "invocations": (directory / INVOCATIONS_FILENAME).is_file(),
        "friction": (directory / FRICTION_FILENAME).is_file(),
        "retro_section": section is not None,
    }
    review = state.get("review")
    row: dict[str, Any] = {
        "project": project_id,
        "title": state.get("title"),
        "status": state.get("status"),
        "created": state.get("created"),
        "updated": state.get("updated"),
        "revision": state.get("revision"),
        "working_directory": state.get("working_directory"),
        "tasks": tasks,
        "layers": layers,
        "legacy": not any(layers.values()),
        "invocations": summarize_invocations(directory),
        "friction": friction[:FRICTION_LIMIT],
        "friction_total": len(friction),
        "retro_section": None if section is None else section[:RETRO_SECTION_CHARS],
        "retro_section_truncated": section is not None and len(section) > RETRO_SECTION_CHARS,
        "evidence": {
            "records": len(evidence_rows),
            "failed": len(failed),
            "failed_heads": [str(row.get("command", ""))[:HEAD_CHARS] for row in failed[:FAILED_HEADS_LIMIT]],
        },
        "decisions": len(ENTRY_MARKER_PATTERN.findall(_text_or_empty(directory / "spec.md"))),
        "worktrees": len(state.get("worktrees", [])) if isinstance(state.get("worktrees"), list) else 0,
        "review": review.get("status") if isinstance(review, dict) else None,
        "staged_lessons": _staged_lesson_count(directory),
    }
    return row


def _staged_lesson_count(directory: Path) -> int:
    try:
        return len(staged_lesson_lines(directory))
    except (WorkspaceError, OSError):
        return 0


def transcript_signals(project_id: str, claude_dir: Path) -> "list[dict[str, Any]]":
    """Tool-call tallies of the Claude Code transcripts that worked on the project; nothing on failure."""
    try:
        from workspace_dashboard import SessionIndex

        sessions = SessionIndex(claude_dir).find().get(project_id, [])
    except Exception:  # the transcript layout is not an interface; any failure means no transcripts
        return []
    result = []
    for session in sessions[:TRANSCRIPT_LIMIT]:
        for path in (claude_dir / "projects").glob(f"*/{session['id']}.jsonl"):
            tally = _tally_transcript(path)
            if tally is not None:
                result.append({"id": session["id"], "path": str(path), "last": session.get("last"), **tally})
    return result


def _tally_transcript(path: Path) -> "dict[str, Any] | None":
    calls: dict[str, int] = {}
    errors = lines = 0
    try:
        with path.open("rb") as handle:
            for raw in handle:
                lines += 1
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                message = value.get("message") if isinstance(value, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = str(block.get("name", ""))
                        calls[name] = calls.get(name, 0) + 1
                    elif block.get("type") == "tool_result" and block.get("is_error"):
                        errors += 1
        size = path.stat().st_size
    except OSError:
        return None
    return {"tool_calls": calls, "errors": errors, "lines": lines, "bytes": size}


def retro_batch(
    root: Path,
    *,
    since: "str | None" = None,
    projects: "list[str] | None" = None,
    limit: int = 20,
    offset: int = 0,
    transcripts: bool = True,
    claude_dir: "Path | None" = None,
) -> dict[str, Any]:
    """The batch for one retro run, paged, with what earlier runs settled."""
    if offset < 0 or not 1 <= limit <= PROJECT_LIMIT_MAX:
        raise WorkspaceError(f"offset must be nonnegative; limit must be between 1 and {PROJECT_LIMIT_MAX}")
    root = root.resolve()
    selected = select_batch(root, since=since, projects=projects)
    page = selected["rows"][offset : offset + limit]
    rows = []
    for item in page:
        if item.get("unreadable"):
            rows.append(item)
            continue
        row = project_signals(root, item["state"], item["project"])
        if transcripts and claude_dir is not None:
            row["transcripts"] = transcript_signals(item["project"], claude_dir)
        rows.append(row)
    previous = selected["previous"]
    return {
        "previous": {key: previous[key] for key in ("runs", "run", "through", "rejected", "deferred", "unreadable")},
        "covered": len(previous["covered"]),
        "next_id": next_run_id(root),
        "selection": selected["selection"],
        "projects": rows,
        "pending": selected["pending"],
        "total": len(selected["rows"]),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(selected["rows"]) else None,
    }


# The record of one run. `id` is the agent's (the batch output recommends `next_id`); the rest is the
# user's dispositions in their own words and the candidates they judged.
RECORD_REQUIRED = ("id", "source", "batch", "candidates")
RECORD_ALLOWED = (*RECORD_REQUIRED, "notes")
CANDIDATE_REQUIRED = ("id", "title", "category", "severity", "proposal", "evidence", "disposition", "response")
EVIDENCE_REQUIRED = ("project", "record")
EVIDENCE_ALLOWED = (*EVIDENCE_REQUIRED, "anchor")
TITLE_CHARS = 120


def record_schema() -> dict[str, Any]:
    return {
        "allowed": list(RECORD_ALLOWED),
        "required": list(RECORD_REQUIRED),
        "candidate": {
            "required": list(CANDIDATE_REQUIRED), "category": list(CATEGORIES), "severity": list(SEVERITIES),
            "disposition": list(DISPOSITIONS),
            "evidence": {"required": list(EVIDENCE_REQUIRED), "optional": ["anchor"]},
        },
        "note": "through is computed from the batch; a candidate needs at least one evidence pointer",
    }


def _string(value: Any, label: str, *, limit: int = 0) -> str:
    if not isinstance(value, str) or not value.strip() or (label.endswith("title") and "\n" in value.strip()):
        raise WorkspaceError(f"{label} must be a non-empty one-line string")
    if limit and len(value.strip()) > limit:
        raise WorkspaceError(f"{label} must be at most {limit} characters")
    return value.strip()


def _validate_candidate(candidate: Any, index: int, known: "set[str]", seen: "set[str]") -> dict[str, Any]:
    label = f"candidates[{index}]"
    if not isinstance(candidate, dict):
        raise WorkspaceError(f"{label} must be an object")
    extras = sorted(set(candidate) - set(CANDIDATE_REQUIRED))
    missing = [key for key in CANDIDATE_REQUIRED if key not in candidate]
    if extras or missing:
        raise WorkspaceError(
            f"{label}: missing {missing}, unexpected {extras}; fields: {', '.join(CANDIDATE_REQUIRED)}"
        )
    identifier = _string(candidate["id"], f"{label}.id")
    if not RUN_ID_PATTERN.match(identifier) or identifier in seen:
        raise WorkspaceError(f"{label}.id must be a unique lowercase slug")
    seen.add(identifier)
    for field, allowed in (("category", CATEGORIES), ("severity", SEVERITIES), ("disposition", DISPOSITIONS)):
        if candidate[field] not in allowed:
            raise WorkspaceError(f"{label}.{field} must be one of: {', '.join(allowed)}")
    evidence = candidate["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise WorkspaceError(f"{label}.evidence must be a non-empty list of pointers")
    pointers = []
    for position, pointer in enumerate(evidence):
        where = f"{label}.evidence[{position}]"
        if not isinstance(pointer, dict) or set(pointer) - set(EVIDENCE_ALLOWED) or any(
            key not in pointer for key in EVIDENCE_REQUIRED
        ):
            raise WorkspaceError(f"{where} needs project and record, optionally anchor")
        project = _string(pointer["project"], f"{where}.project")
        if project not in known:
            raise WorkspaceError(f"{where}.project names no project under the root: {project}")
        entry = {"project": project, "record": _string(pointer["record"], f"{where}.record")}
        if "anchor" in pointer:
            entry["anchor"] = _string(pointer["anchor"], f"{where}.anchor")
        pointers.append(entry)
    return {
        "id": identifier,
        "title": _string(candidate["title"], f"{label}.title", limit=TITLE_CHARS),
        "category": candidate["category"],
        "severity": candidate["severity"],
        "proposal": _string(candidate["proposal"], f"{label}.proposal"),
        "evidence": pointers,
        "disposition": candidate["disposition"],
        "response": _string(candidate["response"], f"{label}.response"),
    }


def record_run(root: Path, request: dict[str, Any], *, lock_timeout: float = 5.0) -> dict[str, Any]:
    """Write one run to `retro/<id>.md` and refresh `RETRO.md`; the same input again is a no-op."""
    from workspace_lib import DirectoryLock, WorkspaceConflict, atomic_write_text, document_sha256, now_iso

    root = root.resolve()
    extras = sorted(set(request) - set(RECORD_ALLOWED))
    missing = [key for key in RECORD_REQUIRED if key not in request]
    if extras or missing:
        raise WorkspaceError(
            f"retro record: missing {missing}, unexpected {extras}; allowed: {', '.join(RECORD_ALLOWED)}"
        )
    run_id = _string(request["id"], "id")
    if not RUN_ID_PATTERN.match(run_id):
        raise WorkspaceError("id must be a lowercase slug of letters, digits and hyphens, at most 64 characters")
    source = _string(request["source"], "source")
    notes = _string(request["notes"], "notes") if request.get("notes") not in (None, "") else ""
    batch = request["batch"]
    if not isinstance(batch, list) or not batch or any(not isinstance(item, str) for item in batch):
        raise WorkspaceError("batch must be a non-empty list of project ids")
    known = set(_project_names(root))
    titles: dict[str, str] = {}
    through = ""
    for name in batch:
        if name not in known:
            raise WorkspaceError(f"batch names no project under the root: {name}")
        state, error = _load_project(root / name)
        if state is None:
            raise WorkspaceError(f"batch project {name} cannot be read: {error}")
        if state.get("status") not in TERMINAL:
            raise WorkspaceError(f"batch project {name} is not closed: {state.get('status')}")
        titles[name] = f"{state.get('title', '')} ({state.get('status')})"
        updated = state.get("updated") if isinstance(state.get("updated"), str) else ""
        moment, latest = _instant_or_none(updated), _instant_or_none(through)
        if moment is not None and (latest is None or moment > latest):
            through = updated
    if not isinstance(request["candidates"], list):
        raise WorkspaceError("candidates must be a list, empty for a run with no finding")
    seen: set[str] = set()
    candidates = [_validate_candidate(item, index, known, seen) for index, item in enumerate(request["candidates"])]
    previous = previous_runs(root)
    rejected = {(str(item["title"]).lower(), item["category"]) for item in previous["rejected"]}
    warnings = [
        f"candidate {item['id']} repeats a rejected one: {item['title']!r} ({item['category']})"
        for item in candidates
        if (item["title"].lower(), item["category"]) in rejected
    ]
    signature = document_sha256(json.dumps(
        {"id": run_id, "source": source, "batch": batch, "candidates": candidates, "notes": notes}, sort_keys=True
    ))
    directory = root / RETRO_DIRECTORY
    path = directory / f"{run_id}.md"
    counts = {key: sum(1 for item in candidates if item["disposition"] == key) for key in DISPOSITIONS}
    result: dict[str, Any] = {
        "operation": "retro.record", "id": run_id, "path": str(path), "through": through, "batch": len(batch),
        "counts": counts, "warnings": warnings, "index": str(root / RETRO_INDEX_FILENAME),
    }
    try:
        directory.mkdir(exist_ok=True)
        with DirectoryLock(root / ".retro.lock", timeout=lock_timeout):
            if path.exists():
                block = RETRO_BLOCK.search(read_text(path))
                existing = json.loads(block.group("json")) if block else {}
                if existing.get("signature") != signature:
                    raise WorkspaceConflict(f"retro run {run_id} already exists with different content")
                return {**result, "existing": True}
            run = {
                "version": 1, "id": run_id, "recorded_at": now_iso(), "source": source, "through": through,
                "batch": list(batch), "candidates": candidates, "notes": notes, "signature": signature,
            }
            atomic_write_text(path, render_retro_record(run, titles))
            atomic_write_text(root / RETRO_INDEX_FILENAME, render_retro_index(root))
    except OSError as error:
        raise WorkspaceError(f"cannot write {path}: {error}") from error
    return {**result, "existing": False}


def render_retro_record(run: dict[str, Any], titles: dict[str, str]) -> str:
    """The record: a JSON block the tool reads, then the same facts for a reader."""
    lines = [
        f"# Retro {run['id']}",
        "",
        f"<!-- research-retro-v1 {json.dumps(run, sort_keys=True)} -->",
        "",
        f"Recorded {run['recorded_at']}. Source: {run['source']}. Covers projects closed through {run['through']}.",
        "",
        "## Batch",
        "",
        *[f"- {name} — {titles.get(name, '')}" for name in run["batch"]],
        "",
        "## Candidates",
        "",
    ]
    if not run["candidates"]:
        lines.append("No candidate: the batch gave the retro nothing to propose.")
    for severity in SEVERITIES:
        group = [item for item in run["candidates"] if item["severity"] == severity]
        if not group:
            continue
        lines.extend([f"### {severity.title()}", ""])
        for item in group:
            pointers = ", ".join(
                f"{pointer['project']}/{pointer['record']}" + (f"#{pointer['anchor']}" if pointer.get("anchor") else "")
                for pointer in item["evidence"]
            )
            lines.extend([
                f"- **{item['title']}** ({item['category']}, {item['id']}): {item['disposition']}.",
                f"  Proposal: {item['proposal']}",
                f"  Evidence: {pointers}",
                f"  Response: {item['response']}",
            ])
        lines.append("")
    lines.extend(["## Next cursor", "", f"The next run covers projects closed after {run['through']}.", ""])
    if run.get("notes"):
        lines.extend(["## Notes", "", run["notes"], ""])
    return "\n".join(lines)


def render_retro_index(root: Path) -> str:
    """`RETRO.md`: one line per run, from the records on disk, so two writers converge."""
    rows = []
    directory = root / RETRO_DIRECTORY
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        try:
            block = RETRO_BLOCK.search(read_text(path))
            run = json.loads(block.group("json")) if block else None
        except (WorkspaceError, ValueError):
            run = None
        if not isinstance(run, dict) or not isinstance(run.get("id"), str):
            rows.append(f"- {path.name} — unreadable record")
            continue
        candidates = run.get("candidates", [])
        counts = {key: sum(1 for item in candidates if item.get("disposition") == key) for key in DISPOSITIONS}
        rows.append(
            f"- [{run['id']}]({RETRO_DIRECTORY}/{path.name}) — recorded {str(run.get('recorded_at', ''))[:10]}, "
            f"{len(run.get('batch', []))} project(s) through {run.get('through', '')}: "
            + ", ".join(f"{counts[key]} {key}" for key in DISPOSITIONS)
        )
    return (
        "# Retro ledger\n\n<!-- Generated by manage_workspace.py; do not edit manually. -->\n\n"
        "One line per retro run of the research plugin. `research-project retro batch <root>` selects what\n"
        "the next run covers; `research-project retro record <root> -` adds a run.\n\n" + "\n".join(rows) + "\n"
    )
