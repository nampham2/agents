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
from workspace_session import _headings, _load_state, project_context, validated_project_context

CHECKPOINT = re.compile(r"\A<!-- research-checkpoint-v1\n(.*?)\n-->\n", re.DOTALL)
CONTINUATION_FIELDS = {
    "next", "questions", "pointers", "partial", "ownership", "effects", "lessons", "do_not", "session",
}
# `key.title()` would print "Do_Not"; the one field whose heading is not its name.
CONTINUATION_HEADINGS = {"do_not": "Do not"}


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


def _git(target: Path, *arguments: str) -> "str | None":
    """Run one read-only Git query; None means Git refused (not a repository, bad ref)."""
    completed = subprocess.run(
        ["git", "-C", str(target), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=5,
    )
    if completed.returncode:
        return None
    # Only trailing newlines go: a porcelain status line starts with its status column, and a
    # leading space there is the difference between "modified" and "staged".
    return completed.stdout.rstrip("\n")


def git_observation(target: Path) -> dict[str, Any]:
    """Observe Git metadata only; never attribute dirty paths to an actor."""
    result: dict[str, Any] = {}
    try:
        for key, arguments in (
            ("commit", ["rev-parse", "HEAD"]),
            ("branch", ["branch", "--show-current"]),
            ("dirty", ["status", "--porcelain=v1", "--untracked-files=normal"]),
        ):
            output = _git(target, *arguments)
            if output is None:
                return {"available": False}
            output = output.strip()
            result[key] = output[:4000]
            result[key + "_sha256"] = document_sha256(output)
            result[key + "_truncated"] = len(output) > 4000
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    return {"available": True, **result}


def worktree_observation(path: Path) -> dict[str, Any]:
    """Describe a directory as Git sees it: repository, linked worktree, branch, head, dirty paths.

    Read-only and bounded. `available` is False only when Git itself could not be consulted; a
    directory that is not a repository is a normal observation, not a failure. `repository` is the
    main checkout that owns the worktree (the parent of the common Git directory), and `toplevel`
    lets a caller insist that `path` is the worktree root rather than a subdirectory of one.
    Dirty paths include untracked files: an uncommitted new file is exactly what closure must not
    lose. Nothing here attributes a dirty path to an actor.
    """
    result: dict[str, Any] = {
        "available": True,
        "exists": path.is_dir(),
        "is_repository": False,
        "is_worktree": False,
        "repository": None,
        "toplevel": None,
        "branch": None,
        "head": None,
        "dirty": [],
    }
    if not result["exists"]:
        return result
    try:
        inside = _git(path, "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            return result
        toplevel = _git(path, "rev-parse", "--show-toplevel")
        git_dir = _git(path, "rev-parse", "--absolute-git-dir")
        common = _git(path, "rev-parse", "--git-common-dir")
        head = _git(path, "rev-parse", "HEAD")
        branch = _git(path, "branch", "--show-current")
        status = _git(path, "status", "--porcelain=v1", "--untracked-files=normal")
        if None in (toplevel, git_dir, common, branch, status):
            return {**result, "available": False}
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "available": False}
    assert toplevel is not None and git_dir is not None and common is not None
    assert branch is not None and status is not None
    common_path = Path(common) if Path(common).is_absolute() else Path(toplevel) / common
    common_path = common_path.resolve()
    result.update(
        is_repository=True,
        is_worktree=Path(git_dir).resolve() != common_path,
        repository=str(common_path.parent),
        toplevel=str(Path(toplevel).resolve()),
        # A fresh repository has no commit yet; that is an observation, not an error.
        head=head,
        branch=branch,
        dirty=status.splitlines()[:200],
    )
    return result


def worktree_registered(repository: Path, path: Path) -> "bool | None":
    """Report whether `repository` still lists `path` as a worktree; None when Git cannot say."""
    try:
        listing = _git(repository, "worktree", "list", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing is None:
        return None
    wanted = path.resolve()
    for line in listing.splitlines():
        if line.startswith("worktree ") and Path(line[len("worktree "):]).resolve() == wanted:
            return True
    return False


WORKTREE_GUIDANCE = (
    "read references/worktrees.md, confirm a worktree path and branch with the user, create it, and record it "
    "with `workflow <project-dir> worktree` before the first write"
)


def worktree_status(state: dict[str, Any]) -> dict[str, Any]:
    """Say whether the target root is a repository and whether a recorded worktree covers it.

    This is the observation task start and resume hand to the agent. It grants nothing: the warning
    is the cue to run the worktree procedure, and a legacy project without the field gets the same
    cue as a new one because its repository is just as unprotected.
    """
    target = state["working_directory"]
    recorded = next(
        (item for item in state.get("worktrees", []) if item["role"] == "target" and item["status"] != "removed"),
        None,
    )
    seen = worktree_observation(Path(target))
    warnings: list[str] = []
    if not seen["available"]:
        warnings.append(f"Git could not be consulted for {target}; worktree state is unknown")
    elif seen["is_repository"] and recorded is None:
        kind = "linked worktree" if seen["is_worktree"] else "repository"
        warnings.append(f"target {target} is a Git {kind} with no recorded worktree; {WORKTREE_GUIDANCE}")
    return {
        "target": target,
        "available": seen["available"],
        "is_repository": seen["is_repository"] if seen["available"] else None,
        "is_worktree": seen["is_worktree"] if seen["available"] else None,
        "recorded": recorded,
        "additional": [
            item for item in state.get("worktrees", []) if item["role"] == "additional" and item["status"] != "removed"
        ],
        "warnings": warnings,
    }


def worktree_closure_findings(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Errors that must stop closure and warnings that must reach the handoff.

    Every repository worktree needs the user's keep/remove decision before the project closes, and
    a worktree kept as clean must still be clean now: work committed between the decision and
    finalize is fine, new uncommitted work is not. Untouched observation failures are warnings so
    that a machine without Git can still close a project whose decisions are recorded.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for item in state.get("worktrees", []):
        if item["kind"] == "none" or item["status"] == "removed":
            continue
        path = item["path"]
        if item["status"] == "active":
            errors.append(
                f"worktree {path} has no closure decision; report `git status`, ask the user whether to commit "
                "and whether to keep or remove it, then record `workflow <project-dir> worktree` close"
            )
            continue
        seen = worktree_observation(Path(path))
        if not seen["available"]:
            warnings.append(f"worktree {path} could not be observed at closure; mark it UNVERIFIED in the handoff")
        elif not seen["exists"]:
            warnings.append(
                f"worktree {path} was kept but is missing on disk; record its removal with "
                "`workflow <project-dir> worktree` close, decision remove"
            )
        elif seen["dirty"] and item["closure"]["decision"] == "keep":
            errors.append(
                f"worktree {path} was kept as clean but now has uncommitted changes: "
                + ", ".join(seen["dirty"][:10])
                + "; commit them (only when the user asks) or record accept_dirty"
            )
    return errors, warnings


def worktree_summaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact per-worktree observations for checkpoint metadata."""
    summaries = []
    for item in state.get("worktrees", []):
        summary: dict[str, Any] = {"path": item["path"], "branch": item["branch"], "status": item["status"]}
        if item["kind"] != "none" and item["status"] != "removed":
            seen = worktree_observation(Path(item["path"]))
            summary["dirty"] = len(seen["dirty"]) if seen["available"] and seen["exists"] else None
        summaries.append(summary)
    return summaries


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
        {"version", "revision", "phase", "sources", "git", "worktrees", "continuation", "recorded", "tasks", "review"},
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
            # Only what is supplied now: a value saved before this rule that holds a heading must
            # still re-render, or one old handoff would be unrecoverable. A heading inside a field
            # renders as a section of its own, or, glued to a line, as no heading at all.
            for level, title, _ in _headings(value):
                if level <= 2:
                    raise WorkspaceError(
                        f"continuation field {key!r} contains the heading {title!r}; a field is body text under "
                        "its own heading. Put the Do not list in the do_not field."
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
        "worktrees": worktree_summaries(state),
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
    # The Do not list leads: it is the part of a handoff a resuming session must not miss.
    order = ["do_not", *(key for key in continuation if key not in ("session", "do_not"))]
    text += "".join(
        f"\n## {CONTINUATION_HEADINGS.get(key, key.title())}\n\n{continuation[key]}\n"
        for key in order
        if continuation.get(key)
    )
    legacy = existing if previous is None else ""
    if previous and "\n## Preserved legacy note\n" in existing:
        legacy = existing.split("\n## Preserved legacy note\n", 1)[1].strip()
    if legacy:
        text += "\n## Preserved legacy note\n\n" + legacy.strip() + "\n"
    return text, metadata


def handoff_banner(project: Path, state: dict[str, Any]) -> str:
    """Say so when the handoff was written at a different revision or phase than the state now has.

    Nothing refreshes a handoff when a task starts or finishes, so a session that ends mid-work
    leaves one whose Phase and Next describe an earlier moment. Empty when it is current, missing,
    legacy or unreadable: this is a hint for a reader, never a reason to fail a read.
    """
    try:
        previous = checkpoint_data(project)
    except WorkspaceError:
        return ""
    if previous is None or (previous["revision"] == state["revision"] and previous.get("phase") == state["status"]):
        return ""
    return (
        f"handoff.md was written at revision {previous['revision']} (phase {previous.get('phase', 'unknown')}); the "
        f"project is now at revision {state['revision']} (phase {state['status']}). Its Phase and Next may be out "
        "of date: trust project.json, and refresh the handoff with workflow checkpoint."
    )


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
    freshness: dict[str, Any] = {
        "revision_changed": state["revision"] != previous["revision"],
        "sources": sources,
        "target": "unknown" if not git.get("available") else "unchanged" if git == previous["git"] else "changed",
        "ownership": "requires host observations; matching hashes do not establish stopped processes",
    }
    # Only when they say something: this is in every resume, and a bundle is worth having only while
    # it stays smaller than the separate reads it replaces.
    if freshness["revision_changed"] or previous.get("phase") != state["status"]:
        freshness.update(
            handoff_revision=previous["revision"],
            handoff_phase=previous.get("phase"),
            phase_changed=previous.get("phase") != state["status"],
        )
    return freshness


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
        checkpoint = checkpoint_data(project)
        context.update(sources=selected, omitted=omitted, freshness=checkpoint_freshness(project))
        # Structured, so it is not subject to the 2000-character slice of the handoff text; absent
        # when there is nothing to say, to keep the common payload as small as it was.
        do_not = checkpoint["continuation"].get("do_not", "") if checkpoint else ""
        if do_not:
            context["do_not"] = do_not
        context["worktree"] = worktree_status(_load_state(project))
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
