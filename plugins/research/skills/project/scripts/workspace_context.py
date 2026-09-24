"""Bounded source selection, checkpoint rendering, fingerprints and resume packets."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from memory_search import memory_health
from workspace_documents import ENTRY_MARKER_PATTERN, _section_span
from workspace_evidence import evidence_entries
from workspace_journal import contained, document_path, list_input, object_input, snapshot, string_input
from workspace_lib import DirectoryLock, WorkspaceError, document_sha256, now_iso
from workspace_session import _load_state, project_context, validated_project_context

CHECKPOINT = re.compile(r"\A<!-- research-checkpoint-v1\n(.*?)\n-->\n", re.DOTALL)
CONTINUATION_FIELDS = {"next", "questions", "pointers", "partial", "ownership", "effects", "lessons", "session"}


def selected_read(project: Path, selection: dict[str, Any]) -> dict[str, Any]:
    """Read a selected section/entry with whole-source hashes and explicit continuation offsets."""
    selection = object_input(
        selection, {"document", "section", "entry", "offset", "max_chars", "if_sha256"}, {"document"}
    )
    name = string_input(selection["document"])
    path = contained(project, "evidence.md") if name == "evidence" else document_path(project, name)
    text, token = snapshot(path)
    result: dict[str, Any] = {"document": name, "sha256": token, "exists": token != "missing"}
    if selection.get("if_sha256") == token:
        return {**result, "unchanged": True}
    if name == "handoff":
        text = CHECKPOINT.sub("", text, count=1)
    if "section" in selection and "entry" in selection:
        raise WorkspaceError("select a section or entry, not both")
    if "entry" in selection:
        entry = string_input(selection["entry"])
        if name == "evidence":
            text = evidence_entries(project, record_id=entry, include_text=True)["entries"][0]["text"]
        else:
            markers = list(ENTRY_MARKER_PATTERN.finditer(text))
            matches = [i for i, marker in enumerate(markers) if marker["id"] == entry]
            if len(matches) != 1:
                raise WorkspaceError("entry must match exactly one saved decision/finding")
            index = matches[0]
            text = text[markers[index].start() : markers[index + 1].start() if index + 1 < len(markers) else len(text)]
    elif "section" in selection:
        _, start, _, end = _section_span(text, string_input(selection["section"]))
        text = text[start:end]
    offset, limit = selection.get("offset", 0), selection.get("max_chars", 2000)
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= len(text) or not 1 <= limit <= 20000:
        raise WorkspaceError("invalid offset/max_chars; reload changed sources")
    end = min(len(text), offset + limit)
    return {
        **result,
        "text": text[offset:end],
        "total_chars": len(text),
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "truncated": offset > 0 or end < len(text),
    }


def git_observation(target: Path) -> dict[str, Any]:
    """Observe Git metadata only; never attribute dirty paths to an actor."""
    result: dict[str, Any] = {}
    try:
        for key, arguments in (
            ("commit", ["rev-parse", "HEAD"]),
            ("branch", ["branch", "--show-current"]),
            ("dirty", ["status", "--porcelain=v1", "--untracked-files=normal"]),
        ):
            completed = subprocess.run(
                ["git", "-C", str(target), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                timeout=5,
            )
            if completed.returncode:
                return {"available": False}
            output = completed.stdout.strip()
            result[key] = output[:4000]
            result[key + "_sha256"] = document_sha256(output)
            result[key + "_truncated"] = len(output) > 4000
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    return {"available": True, **result}


def checkpoint_data(project: Path) -> dict[str, Any] | None:
    """Recognize only the versioned metadata block; legacy notes remain untouched."""
    text, _ = snapshot(document_path(project, "handoff"))
    match = CHECKPOINT.match(text)
    if not match:
        return None
    try:
        value = json.loads(match[1])
    except ValueError as error:
        raise WorkspaceError("checkpoint metadata is unreadable") from error
    object_input(
        value,
        {"version", "revision", "phase", "sources", "git", "continuation", "recorded", "tasks", "review"},
        {"version", "revision", "sources", "continuation", "git"},
    )
    if (
        value["version"] != 1
        or type(value["revision"]) is not int
        or not isinstance(value["sources"], dict)
        or not isinstance(value["continuation"], dict)
        or not isinstance(value["git"], dict)
    ):
        raise WorkspaceError("unsupported checkpoint metadata")
    object_input(value["sources"], {"spec", "architecture"})
    object_input(value["continuation"], CONTINUATION_FIELDS)
    if not all(isinstance(item, str) for item in [*value["sources"].values(), *value["continuation"].values()]):
        raise WorkspaceError("invalid checkpoint fields")
    return value


def render_checkpoint(
    project: Path,
    state: dict[str, Any],
    changes: dict[str, Any],
    documents: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Merge explicitly supplied continuation fields, deriving mechanical metadata."""
    object_input(changes, CONTINUATION_FIELDS | {"import_legacy"})
    previous = checkpoint_data(project)
    existing, _ = snapshot(document_path(project, "handoff"))
    if existing and previous is None and changes.get("import_legacy") is not True:
        raise WorkspaceError("legacy handoff requires import_legacy: true; its full text will be preserved")
    continuation = dict(previous["continuation"]) if previous else {}
    for key, value in changes.items():
        if key != "import_legacy":
            if not isinstance(value, str):
                raise WorkspaceError(
                    "continuation fields must be strings; use empty text to explicitly resolve a field"
                )
            continuation[key] = value
    string_input(continuation.get("next"))
    if continuation.get("session", "working") not in ("working", "waiting", "blocked", "ready for handoff"):
        raise WorkspaceError("invalid session label")
    sources = {}
    for name in ("spec", "architecture"):
        sources[name] = (
            document_sha256(documents[name])
            if documents and name in documents
            else snapshot(document_path(project, name))[1]
        )
    metadata = {
        "version": 1,
        "revision": state["revision"],
        "phase": state["status"],
        "sources": sources,
        "git": git_observation(Path(state["working_directory"])),
        "continuation": continuation,
        "tasks": [
            {"id": task["id"], "evidence": task["evidence"]}
            for task in state["tasks"]
            if task["id"] in state["current_tasks"]
        ],
        "review": state["review"],
    }
    if previous and all(previous.get(key) == value for key, value in metadata.items()):
        return existing, previous
    metadata["recorded"] = now_iso()
    text = "<!-- research-checkpoint-v1\n" + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + "\n-->\n"
    text += f"# Continuation\n\nSession state: {continuation.get('session', 'working')}\n"
    text += f"Phase: {state['status']}; revision: {state['revision']}\n"
    text += "".join(
        f"\n## {key.title()}\n\n{value}\n" for key, value in continuation.items() if key != "session" and value
    )
    legacy = existing if previous is None else ""
    if previous and "\n## Preserved legacy note\n" in existing:
        legacy = existing.split("\n## Preserved legacy note\n", 1)[1].strip()
    if legacy:
        text += "\n## Preserved legacy note\n\n" + legacy.strip() + "\n"
    return text, metadata


def checkpoint_freshness(project: Path) -> dict[str, Any]:
    """Compare machine-recorded hints without certifying ownership or acceptance."""
    previous = checkpoint_data(project)
    if previous is None:
        return {"status": "unknown", "reason": "missing or legacy checkpoint metadata"}
    state = _load_state(project)
    sources = {}
    for name, old in previous["sources"].items():
        actual = snapshot(document_path(project, name))[1]
        sources[name] = "missing" if actual == "missing" else "unchanged" if actual == old else "changed"
    git = git_observation(Path(state["working_directory"]))
    return {
        "revision_changed": state["revision"] != previous["revision"],
        "sources": sources,
        "target": "unknown" if not git.get("available") else "unchanged" if git == previous["git"] else "changed",
        "ownership": "requires host observations; matching hashes do not establish stopped processes",
    }


def resume_bundle(project: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Read one coherent cooperative-writer snapshot, with bounded explicit selections."""
    object_input(request, {"task", "selections", "max_chars", "worker", "scope", "lessons"})
    maximum = request.get("max_chars", 12000)
    if type(maximum) is not int or not 1 <= maximum <= 40000:
        raise WorkspaceError("bundle max_chars must be between 1 and 40000")
    selections = list_input(
        request.get("selections", [{"document": "handoff"}, {"document": "spec", "section": "Current specification"}]),
        12,
    )
    with DirectoryLock(project / ".project.lock"):
        context = validated_project_context(project)
        context.pop("spec")
        context.pop("spec_truncated")
        selected = []
        omitted = []
        for selection in selections:
            object_input(selection, {"document", "section", "entry", "offset", "max_chars", "if_sha256"}, {"document"})
            limit = selection.get("max_chars", 2000)
            if type(limit) is not int or not 1 <= limit <= 20000:
                raise WorkspaceError("selection max_chars must be between 1 and 20000")
            if maximum <= 0:
                omitted.append(selection)
                continue
            item = selected_read(project, {**selection, "max_chars": min(maximum, limit)})
            maximum -= len(item.get("text", ""))
            selected.append(item)
        context.update(sources=selected, omitted=omitted, freshness=checkpoint_freshness(project))
        context["memory_health"] = memory_health(project.parent, context["title"])
        if "task" in request:
            context["assignment"] = project_context(project, task_id=string_input(request["task"]), task_only=True)
        context["worker_packet"] = {key: request[key] for key in ("worker", "scope", "lessons") if key in request}
        return context


def fingerprint(project: Path, references: list[Any]) -> list[dict[str, Any]]:
    """Hash only explicitly selected local files with a bounded total read budget."""
    state = _load_state(project)
    roots = {"workspace": project, "target": Path(state["working_directory"]), "workspace_root": project.parent}
    result = []
    remaining = 16 * 1024 * 1024
    for ref in list_input(references):
        object_input(ref, {"root", "path", "sha256"}, {"root", "path"})
        if not isinstance(ref["root"], str) or ref["root"] not in roots:
            raise WorkspaceError("fingerprints require a local root")
        path = contained(roots[ref["root"]], ref["path"])
        actual = "missing"
        if path.exists():
            size = path.stat().st_size
            if not path.is_file() or size > remaining:
                raise WorkspaceError("fingerprint file budget exceeded or path is not a file")
            with path.open("rb") as stream:
                data = stream.read(remaining + 1)
            if len(data) > remaining:
                raise WorkspaceError("fingerprint file grew beyond budget")
            remaining -= len(data)
            actual = hashlib.sha256(data).hexdigest()
        result.append(
            {
                "root": ref["root"],
                "path": ref["path"],
                "sha256": actual,
                "changed": ref.get("sha256") != actual if "sha256" in ref else None,
            }
        )
    return result
