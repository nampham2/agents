"""Bounded source selection, checkpoint rendering, fingerprints and resume packets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from memory_search import memory_health
from workspace_documents import ENTRY_MARKER_PATTERN, _section_span
from workspace_durability import context_view, durability_view, durable_move
from workspace_evidence import evidence_entries
from workspace_fields import CONTINUATION_FIELDS
from workspace_journal import (
    contained,
    document_path,
    list_input,
    object_input,
    schema_input,
    snapshot,
    string_input,
)
from workspace_lib import (
    DirectoryLock,
    WorkspaceError,
    _resolve_local_reference,
    delivery_rules,
    document_sha256,
    now_iso,
    read_text,
    resolve_workspace_root,
    task_packets_enabled,
)
from workspace_session import (
    STOP_REASONS,
    _headings,
    _load_state,
    project_context,
    validated_project_context,
)
from workspace_similar import find_similar

CHECKPOINT = re.compile(r"\A<!-- research-checkpoint-v1\n(.*?)\n-->\n", re.DOTALL)
# `key.title()` would print "Do_Not"; the one field whose heading is not its name.
CONTINUATION_HEADINGS = {"do_not": "Do not", "stop_reason": "Stop reason"}
SESSION_FIELDS = {"host", "id", "cwd"}
# A resume request carrying any of these is a worker packet: it never counts toward a crash loop.
WORKER_KEYS = ("worker", "scope", "lessons")


def selected_read(project: Path, selection: dict[str, Any]) -> dict[str, Any]:
    """Read a selected section/entry with whole-source hashes and explicit continuation offsets."""
    selection = schema_input(selection, "read")
    name = string_input(selection["document"], "document")
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
        entry = string_input(selection["entry"], "entry")
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
        _, start, _, end = _section_span(text, string_input(selection["section"], "section"))
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
    elif seen["is_repository"] and recorded is None and delivery_rules(state).warn_unrecorded_worktree:
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


def _status_path(line: str) -> str:
    """The path part of a `git status --short` line: two status columns and a space precede it."""
    return line[3:]


def worktree_closure_findings(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Errors that must stop closure and warnings that must reach the handoff.

    Every repository worktree needs the user's keep/remove decision before the project closes, and
    a worktree kept as clean must still be clean now: work committed between the decision and
    finalize is fine, new uncommitted work is not. Untouched observation failures are warnings so
    that a machine without Git can still close a project whose decisions are recorded.
    """
    from workspace_delivery import closure_findings

    errors: list[str] = closure_findings(state)
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
        elif item["closure"]["decision"] == "keep":
            if seen["dirty"]:
                errors.append(
                    f"worktree {path} was kept as clean but now has uncommitted changes: "
                    + ", ".join(seen["dirty"][:10])
                    + "; commit them (only when the user asks) or record accept_dirty"
                )
        else:
            # accept_dirty accepted the paths that were dirty then, not whatever becomes dirty later.
            # Matching is by path: staging an accepted file or committing it is no new work, but a
            # path the user never saw is. A change to the content of an accepted path is not visible
            # to `git status`, so that is outside what this can detect.
            accepted = {_status_path(line) for line in item["closure"]["dirty"]}
            beyond = [line for line in seen["dirty"] if _status_path(line) not in accepted]
            if beyond:
                errors.append(
                    f"worktree {path} has uncommitted changes beyond the accepted dirty snapshot: "
                    + ", ".join(beyond[:10])
                    + "; commit them (only when the user asks) or record accept_dirty again with the user's "
                    "explicit acceptance"
                )
    return errors, warnings


def worktree_summaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact per-worktree observations for checkpoint metadata."""
    summaries = []
    for item in state.get("worktrees", []):
        summary: dict[str, Any] = {"path": item["path"], "branch": item["branch"], "status": item["status"]}
        summary["exists"] = None
        if item["kind"] != "none" and item["status"] != "removed":
            seen = worktree_observation(Path(item["path"]))
            summary["dirty"] = len(seen["dirty"]) if seen["available"] and seen["exists"] else None
            summary["exists"] = seen["exists"] if seen["available"] else None
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
        {
            "version",
            "revision",
            "phase",
            "sources",
            "git",
            "worktrees",
            "continuation",
            "recorded",
            "tasks",
            "review",
            "session",
        },
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
    # `architecture` is the key checkpoints held before the planning document was split from the design.
    object_input(value["sources"], {"spec", "planning", "architecture"})
    object_input(value["continuation"], CONTINUATION_FIELDS)
    if not all(isinstance(item, str) for item in [*value["sources"].values(), *value["continuation"].values()]):
        raise WorkspaceError("invalid checkpoint fields")
    # Checkpoints written before the session block have no key; a recorded one is a closed object.
    session = value.get("session")
    if session is not None:
        object_input(session, SESSION_FIELDS, SESSION_FIELDS, where="checkpoint session")
        if not all(isinstance(session[field], str) and session[field] for field in SESSION_FIELDS):
            raise WorkspaceError("invalid checkpoint session")
        if session["host"] not in ("claude", "pi"):
            raise WorkspaceError("invalid checkpoint session host")
    return value


def session_observation() -> dict[str, str] | None:
    """Name the agent session that writes this checkpoint, when the host says which one it is.

    Pi sets `PI_SESSION_ID` in every shell tool call. Claude Code sets `CLAUDE_CODE_SESSION_ID`.
    Pi wins when both are set, because a Pi session can inherit a Claude variable from its parent
    shell. No time: the checkpoint's own `recorded` dates the save, and a per-call stamp would make
    every checkpoint look changed.
    """
    for variable, host in (("PI_SESSION_ID", "pi"), ("CLAUDE_CODE_SESSION_ID", "claude")):
        identity = os.environ.get(variable, "").strip()
        if identity:
            return {"host": host, "id": identity, "cwd": os.getcwd()}
    return None


STOPPING_LABELS = ("waiting", "blocked", "ready for handoff")


def infer_stop_reason(project: Path, state: dict[str, Any], continuation: Any) -> str | None:
    """The stop_reason a stopping checkpoint omitted, when `next_move` names one; otherwise None.

    The label is read the way `render_checkpoint` reads it, from the supplied continuation and else from the
    saved handoff. An empty stop_reason counts as omitted; a non-empty one is never replaced.
    """
    if not isinstance(continuation, dict) or continuation.get("stop_reason"):
        return None
    label = continuation.get("session")
    if label is None:
        previous = checkpoint_data(project)
        label = previous["continuation"].get("session", "working") if previous else "working"
    return durable_move(project, state)["stop_reason"] if label in STOPPING_LABELS else None


def next_hint(project: Path, state: dict[str, Any]) -> str | None:
    """What the move says to do instead of stopping, for the refusal that names a missing stop_reason.

    The move is `durable_move` without a pending resume: a checkpoint is not a resume.
    """
    move = durable_move(project, state)
    if move["continue"] is None:
        return None
    return f"next_move says continue {move['continue']}: {move['move_detail']}"


def render_checkpoint(
    project: Path,
    state: dict[str, Any],
    changes: dict[str, Any],
    documents: dict[str, str] | None = None,
    next_hint: str | None = None,
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
    session_label = continuation.get("session", "working")
    if session_label not in ("working", "waiting", "blocked", "ready for handoff"):
        raise WorkspaceError("invalid session label")
    if session_label == "working":
        if changes.get("stop_reason"):
            raise WorkspaceError("a working session carries no stop_reason; stop_reason names why work stops")
        continuation.pop("stop_reason", None)
    elif continuation.get("stop_reason") not in STOP_REASONS:
        raise WorkspaceError(
            f"session {session_label!r} needs a stop_reason, one of: {', '.join(STOP_REASONS)}. Continue the "
            "next ready task instead of stopping when none of them applies" + (f" ({next_hint})" if next_hint else "")
        )
    sources = {}
    for name in ("spec", "planning"):
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
        "session": session_observation(),
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
        # A checkpoint's `architecture` source was the agreement, which is now the planning document.
        actual = snapshot(document_path(project, "planning" if name == "architecture" else name))[1]
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


def resume_bundle(project: Path, request: dict[str, Any], *, action: str = "resume") -> dict[str, Any]:
    """Read one coherent cooperative-writer snapshot, with bounded explicit selections.

    Only a project resume counts itself toward a crash loop (`pending_resume`): it is not logged until
    it finishes. A packet, or a resume carrying worker keys, is logged as a worker packet and never adds one.
    """
    schema_input(request, "resume")
    maximum = request.get("max_chars", 12000)
    if type(maximum) is not int or not 1 <= maximum <= 40000:
        raise WorkspaceError("bundle max_chars must be between 1 and 40000")
    selections = list_input(
        request.get("selections", [{"document": "handoff"}, {"document": "spec", "section": "Current specification"}]),
        12,
        "selections",
    )
    with DirectoryLock(project / ".project.lock"):
        context = validated_project_context(project)
        context.pop("spec")
        context.pop("spec_truncated")
        selected = []
        omitted = []
        for selection in selections:
            schema_input(selection, "read")
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
        state = _load_state(project)
        context["worktree"] = worktree_status(state)
        if action == "resume" and task_packets_enabled(state):
            cited = [
                "ev-" + item["evidence"][-1]["anchor"].removeprefix("evidence-")
                for item in state["tasks"] if item["status"] == "RUNNING" and item["evidence"]
            ]
            context["evidence_applicability"] = evidence_applicability(project, state, cited)
        pending = action == "resume" and not any(key in request for key in WORKER_KEYS)
        context.update(durable_move(project, state, pending_resume=pending))
        context.update(durability_view(project, state, pending_resume=pending))
        context["memory_health"] = memory_health(project.parent, context["title"])
        if "task" in request:
            context["assignment"] = project_context(
                project, task_id=string_input(request["task"], "task"), task_only=True
            )
        context["worker_packet"] = {key: request[key] for key in WORKER_KEYS if key in request}
        return context


FINGERPRINT_LIMIT = 16 * 1024 * 1024
NOT_A_FILE = "fingerprint file budget exceeded or path is not a file"


class Budget:
    """One read budget shared by every path hashed in a call; once a read is refused it stays spent."""

    def __init__(self, limit: "int | None" = None) -> None:
        self.remaining = FINGERPRINT_LIMIT if limit is None else limit
        self.exhausted = False

    def take(self, size: int) -> bool:
        if size > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= size
        return True


def _file_digest(path: Path, budget: Budget) -> str:
    """SHA-256 of one regular file, charged to `budget`; raises `WorkspaceError` when it does not fit."""
    if path.stat().st_size > budget.remaining:
        budget.exhausted = True
        raise WorkspaceError(NOT_A_FILE)
    with path.open("rb") as stream:
        data = stream.read(budget.remaining + 1)
    if len(data) > budget.remaining:
        budget.exhausted = True
        raise WorkspaceError("fingerprint file grew beyond budget")
    budget.take(len(data))
    return hashlib.sha256(data).hexdigest()


def _raise(error: OSError) -> None:
    raise error


def _directory_digest(path: Path, budget: Budget) -> str:
    """SHA-256 over `<relative POSIX path>\\0<file hash>\\n` lines, sorted by path.

    Symlinks and other non-regular files are skipped, `.git` is skipped and an empty directory adds nothing.
    """
    entries: list[tuple[str, str]] = []
    for current, directories, files in os.walk(path, followlinks=False, onerror=_raise):
        directories[:] = [name for name in directories if name != ".git"]
        for name in files:
            full = Path(current) / name
            if full.is_symlink() or not full.is_file():
                continue
            entries.append((full.relative_to(path).as_posix(), _file_digest(full, budget)))
    entries.sort()
    return hashlib.sha256("".join(f"{name}\0{digest}\n" for name, digest in entries).encode("utf-8")).hexdigest()


def hash_path(path: Path, budget: Budget) -> tuple[str, "str | None"]:
    """`(kind, sha256)` of a path now: `missing`, `file` or `dir`.

    Raises what `fingerprint()` raises (`WorkspaceError`) for a path that is neither a file nor a directory
    or that does not fit the budget, and lets an `OSError` through.
    """
    if not path.exists():
        return "missing", None
    if path.is_file():
        return "file", _file_digest(path, budget)
    if path.is_dir():
        return "dir", _directory_digest(path, budget)
    raise WorkspaceError(NOT_A_FILE)


def _hash_declared(
    project: Path, target: Path, root: str, relative: str, budget: Budget
) -> tuple[str, "str | None"]:
    """The kind and hash of one declared path, never raising: `over-budget` and `unreadable` are kinds."""
    if budget.exhausted:
        return "over-budget", None
    path, error = _resolve_local_reference(project, target, root, relative)
    if path is None or error:
        return "unreadable", None
    try:
        if path.exists() and not (path.is_file() or path.is_dir()):
            return "unreadable", None  # a FIFO or a device is never opened
        return hash_path(path, budget)
    except WorkspaceError:
        return "over-budget", None
    except OSError:
        return "unreadable", None


def capture_fingerprint(project: Path, state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """The evidence lines of one task-owned entry: the assignment counter, then one line per declared path.

    The task and state are the ones read before the check ran, so an assignment edited while the command
    ran is recorded with the old counter and reads as invalidated. `reads` resolve against the target and
    `outputs` against their root; an `external` output is skipped.
    """
    target = Path(state["working_directory"])
    declared = [("target", item) for item in task.get("reads", [])]
    declared += [(out["root"], out["path"]) for out in task.get("outputs", []) if out["root"] != "external"]
    budget = Budget()
    lines = [f"- Assignment counter: {task['assignment_counter']}"]
    for root, relative in dict.fromkeys(declared):
        if not relative.isprintable():
            lines.append(f"- Fingerprint: unreadable {root} - {relative!r}")
            continue
        kind, digest = _hash_declared(project, target, root, relative, budget)
        lines.append(f"- Fingerprint: {kind} {root} {digest or '-'} {relative}")
    return lines


def _dirty_paths(target: Path, paths: list[str]) -> list[str]:
    """Paths among `paths` that Git reports changed, or `[]` when Git cannot say (no repository, a timeout)."""
    try:
        output = _git(
            target, "--no-optional-locks", "--literal-pathspecs", "status", "--porcelain=v1",
            "--untracked-files=normal", "--", *paths,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [line[3:] for line in output.splitlines()] if output else []


def classify_evidence(
    project: Path, state: dict[str, Any], row: dict[str, Any], budget: Budget,
) -> "dict[str, Any] | None":
    """Whether a recorded check still applies: `applicable`, `invalidated` or `needs-judgement`.

    None for an entry no task owns (a closure step). The checks run in the order of the design: counter,
    missing lines, then each recorded path against its current kind and hash. Never raises.
    """
    task = next((item for item in state["tasks"] if item["id"] == row["owner"]), None)
    if task is None or row["kind"] == "observation":
        return None
    result: dict[str, Any] = {"record_id": row["record_id"], "task": task["id"], "dirty": []}
    counter = task.get("assignment_counter")
    if row["counter"] is not None and type(counter) is int and row["counter"] < counter:
        return {**result, "state": "invalidated",
                "reasons": [f"assignment counter moved from {row['counter']} to {counter}"]}
    if row["counter"] is None or not row["fingerprints"]:
        return {**result, "state": "needs-judgement", "reasons": ["no fingerprint was recorded with this entry"]}
    target = Path(state["working_directory"])
    changed: list[str] = []
    unsure: list[str] = []
    for item in row["fingerprints"]:
        now_kind, now_digest = _hash_declared(project, target, item["root"], item["path"], budget)
        label = f"{item['root']}:{item['path']}"
        if item["kind"] in ("over-budget", "unreadable"):
            unsure.append(f"{label}: recorded as {item['kind']}")
        elif now_kind in ("over-budget", "unreadable"):
            unsure.append(f"{label}: {now_kind} now")
        elif (now_kind, now_digest) != (item["kind"], item["sha256"]):
            changed.append(f"{label}: {item['kind']} then, {now_kind} now")
    if changed:
        return {**result, "state": "invalidated", "reasons": changed}
    if unsure:
        return {**result, "state": "needs-judgement", "reasons": unsure}
    local = [item["path"] for item in row["fingerprints"] if item["root"] == "target"]
    return {**result, "state": "applicable", "reasons": [], "dirty": _dirty_paths(target, local) if local else []}


def evidence_applicability(project: Path, state: dict[str, Any], record_ids: list[str]) -> list[dict[str, Any]]:
    """Classify the named records of task-owned entries with one shared budget; it never blocks."""
    from workspace_evidence import _entry_rows

    path = project / "evidence.md"
    if not record_ids or not path.exists():
        return []
    rows = {row["record_id"]: row for row in _entry_rows(read_text(path)) if row["record_id"]}
    budget = Budget()
    report = []
    for record_id in record_ids:
        item = classify_evidence(project, state, rows[record_id], budget) if record_id in rows else None
        if item is not None:
            report.append(item)
    return report


def fingerprint(project: Path, references: list[Any]) -> list[dict[str, Any]]:
    """Hash only explicitly selected local files with a bounded total read budget."""
    state = _load_state(project)
    roots = {"workspace": project, "target": Path(state["working_directory"]), "workspace_root": project.parent}
    result = []
    budget = Budget()
    for ref in list_input(references):
        object_input(ref, {"root", "path", "sha256"}, {"root", "path"})
        if not isinstance(ref["root"], str) or ref["root"] not in roots:
            raise WorkspaceError("fingerprints require a local root")
        path = contained(roots[ref["root"]], ref["path"])
        actual = "missing"
        if path.exists():
            if not path.is_file():
                raise WorkspaceError(NOT_A_FILE)
            actual = _file_digest(path, budget)
        result.append(
            {
                "root": ref["root"],
                "path": ref["path"],
                "sha256": actual,
                "changed": ref.get("sha256") != actual if "sha256" in ref else None,
            }
        )
    return result


COMPACT_NOTE = "tokens are valid until the next write; validation findings are omitted, run context --validate for them"


def compact_context(payload: dict[str, Any], state: dict[str, Any], project: Path) -> dict[str, Any]:
    """The project-level summary of `context` in one object: what an agent needs to resume and write."""
    move = durable_move(project, state)
    result: dict[str, Any] = {
        "project_dir": payload["roots"]["workspace"],
        "revision": payload["revision"],
        "roots": payload["roots"],
        "tokens": payload["tokens"],
        "current_tasks": state.get("current_tasks", []),
        "next_move": {key: move[key] for key in ("continue", "stop_reason", "move_detail")},
        "valid": payload["validation"]["valid"],
        "compact": True,
        "note": COMPACT_NOTE,
    }
    result.update(context_view(project, state))
    return result


def start_project(
    workspace_root: Path | None,
    *,
    project_dir: Path | None = None,
    title: str | None = None,
    working_directory: Path | None = None,
    create: bool = False,
    create_root: bool = False,
    briefing: bool = False,
    lock_timeout: float = 5.0,
    limit: int = 5,
    tags: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Find, create or resume a project and return its compact context with the exit code.

    A title resumes a project only when exactly one live look-alike is on the same repository; several
    matches, or one elsewhere, come back as candidates and nothing is chosen. `create` runs init when there
    is no look-alike at all.
    """
    if project_dir is not None and (title is not None or working_directory is not None):
        raise WorkspaceError(
            "give --project-dir to resume, or --title and --working-directory to find or create"
        )
    matched: dict[str, Any] | None = None
    warnings: list[str] = []
    if project_dir is None:
        if title is None or working_directory is None:
            raise WorkspaceError("give --project-dir to resume, or --title together with --working-directory")
        root = resolve_workspace_root(workspace_root)
        blocking = find_similar(root, title, working_directory)["blocking"] if root.is_dir() else []
        if len(blocking) == 1 and blocking[0]["same_repository"]:
            matched = blocking[0]
            project_dir = root / matched["project"]
        elif blocking:
            return {"candidates": blocking}, 1
        elif not create:
            return {"candidates": []}, 0
        else:
            from workspace_operations import init_project, require_new_tags

            # Only the branch that creates needs tags: a resumed project, or a list of candidates, ignores them.
            new_tags = require_new_tags(tags or [])
            project_dir, warnings = init_project(
                root, title, working_directory, lock_timeout=lock_timeout, create_root=create_root,
                briefing=briefing, tags=new_tags,
            )
    project_dir = project_dir.resolve()
    payload = validated_project_context(project_dir, limit=limit)
    state = _load_state(project_dir)
    result = compact_context(payload, state, project_dir)
    move = result["next_move"]["continue"]
    task_ids = {task["id"] for task in state["tasks"]}
    result["assignment"] = (
        project_context(project_dir, limit=limit, task_id=move, task_only=True) if move in task_ids else None
    )
    if matched is not None:
        result["matched"] = {key: matched[key] for key in ("project", "title", "status", "score", "same_repository")}
    if warnings:
        result["warnings"] = warnings
    return result, 0
