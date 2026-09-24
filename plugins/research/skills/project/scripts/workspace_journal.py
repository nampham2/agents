"""Recoverable, named metadata operations; never replays commands or external effects."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workspace_lib import (
    DirectoryLock,
    WorkspaceConflict,
    WorkspaceError,
    _commit_state_locked,
    _rebuild_index_after_commit,
    atomic_write_json,
    atomic_write_text,
    check_state_candidate,
    document_sha256,
    load_json,
    read_text,
)
from workspace_session import _load_state, prepare_update

if TYPE_CHECKING:
    from collections.abc import Callable

DOCUMENT = re.compile(
    r"^(spec|architecture|reflection|handoff|memory-staging|tasks/[A-Za-z0-9_-]+|reviews/review_[0-9]+|artifacts/report)$"
)
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class OperationError(WorkspaceError):
    """Expose partial outcomes without claiming failed persistence succeeded."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def object_input(value: Any, allowed: set[str], required: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Validate closed object fields before traversal."""
    if not isinstance(value, dict) or value.keys() - allowed or required - value.keys():
        raise WorkspaceError(f"expected object fields {sorted(allowed)}; required {sorted(required)}")
    return value


def string_input(value: Any) -> str:
    """Require meaningful caller-authored text."""
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("expected non-empty text")
    return value


def list_input(value: Any, maximum: int = 50) -> list[Any]:
    """Bound request fanout."""
    if not isinstance(value, list) or len(value) > maximum:
        raise WorkspaceError(f"expected an array of at most {maximum} items")
    return value


def contained(root: Path, relative: str) -> Path:
    """Resolve a relative path without allowing symlink or traversal escapes."""
    relative = string_input(relative)
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceError("path must be relative without '..'")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise WorkspaceError("path escapes its root")
    return resolved


def document_path(project: Path, name: str) -> Path:
    """Resolve only managed prose documents, never state or evidence logs."""
    if not isinstance(name, str) or not DOCUMENT.fullmatch(name):
        raise WorkspaceError(f"unsupported managed document: {name}")
    return contained(project, name + ".md")


def snapshot(path: Path) -> tuple[str, str]:
    """Return contents and a conflict token, preserving original newline bytes."""
    if not path.exists():
        return "", "missing"
    text = read_text(path, preserve_newlines=True)
    return text, document_sha256(text)


def journal_path(project: Path, operation_id: str) -> Path:
    """Resolve a caller-selected retry identity."""
    if not isinstance(operation_id, str) or not IDENTIFIER.fullmatch(operation_id):
        raise WorkspaceError("operation ID must be lowercase letters, digits and hyphens, at most 64 characters")
    return contained(project, f".operations/{operation_id}.json")


def ensure_writable(state: dict[str, Any]) -> None:
    """Retain the legacy executor ownership refusal for all new writes."""
    execution = state.get("execution", {})
    if execution.get("coordinator_run") is not None or execution.get("attempts"):
        raise WorkspaceError("project execution is active; resolve legacy ownership first")


def _payload(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key not in ("revision", "updated")}


def _landed(project: Path, journal: dict[str, Any]) -> bool:
    candidate = journal.get("candidate")
    current = _load_state(project)
    return (
        isinstance(candidate, dict)
        and type(journal.get("expected_revision")) is int
        and current["revision"] == journal["expected_revision"] + 1
        and _payload(current) == _payload(candidate)
    )


def _validate_journal(journal: dict[str, Any]) -> None:
    """Refuse damaged journals rather than treating missing data as consent to replay."""
    required = {
        "kind",
        "id",
        "signature",
        "expected_revision",
        "documents",
        "candidate",
        "saved",
        "state_committed",
        "complete",
        "result",
        "after_commit",
    }
    object_input(journal, required, required)
    journal_path(Path("."), journal["id"])
    if (
        journal["kind"] != "metadata"
        or type(journal["expected_revision"]) is not int
        or not isinstance(journal["documents"], dict)
        or not isinstance(journal["result"], dict)
        or not isinstance(journal["saved"], list)
        or not isinstance(journal["after_commit"], list)
        or type(journal["state_committed"]) is not bool
        or type(journal["complete"]) is not bool
        or (journal["candidate"] is not None and not isinstance(journal["candidate"], dict))
    ):
        raise WorkspaceError("invalid metadata journal")
    for name, item in journal["documents"].items():
        document_path(Path("."), name)
        object_input(item, {"before", "text"}, {"before", "text"})
        string_input(item["before"])
        if not isinstance(item["text"], str):
            raise WorkspaceError("invalid journal document")
    if any(
        not isinstance(name, str) or name not in journal["documents"]
        for name in [*journal["saved"], *journal["after_commit"]]
    ):
        raise WorkspaceError("invalid journal document selection")


def _advance(project: Path, path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    _validate_journal(journal)
    current = _load_state(project)
    ensure_writable(current)
    expected = journal["expected_revision"]
    candidate = journal["candidate"]
    landed = candidate is not None and current["revision"] == expected + 1 and _payload(current) == _payload(candidate)
    if journal["complete"]:
        return journal["result"]
    if current["revision"] != expected and not landed and not journal["state_committed"]:
        raise WorkspaceConflict("state changed during operation; inspect saved operation before recovery")
    if not landed and not journal["state_committed"]:
        # Preflight every token before writing any member of the batch.
        for name, item in journal["documents"].items():
            actual = snapshot(document_path(project, name))[1]
            if actual not in (item["before"], document_sha256(item["text"])):
                raise WorkspaceConflict(f"document changed during operation: {name}")
        for name, item in journal["documents"].items():
            if name in journal["after_commit"]:
                continue
            destination = document_path(project, name)
            if snapshot(destination)[1] != document_sha256(item["text"]):
                atomic_write_text(destination, item["text"])
            journal["saved"] = list(dict.fromkeys([*journal["saved"], name]))
            atomic_write_json(path, journal)
        if candidate is not None:
            _commit_state_locked(project, copy.deepcopy(candidate), expected)
            landed = True
    journal["state_committed"] = landed or journal["state_committed"]
    atomic_write_json(path, journal)
    for name in journal["after_commit"]:
        item = journal["documents"][name]
        destination = document_path(project, name)
        if snapshot(destination)[1] == document_sha256(item["text"]):
            journal["saved"] = list(dict.fromkeys([*journal["saved"], name]))
            continue
        if current["revision"] != expected + 1 and journal["state_committed"] and not landed:
            raise WorkspaceConflict("state advanced before final note recovery; reconcile continuation explicitly")
        if snapshot(destination)[1] not in (item["before"], document_sha256(item["text"])):
            raise WorkspaceConflict(f"post-commit document changed: {name}")
        atomic_write_text(destination, item["text"])
        journal["saved"] = list(dict.fromkeys([*journal["saved"], name]))
        atomic_write_json(path, journal)
    journal["result"].update(
        operation_id=journal["id"],
        revision=_load_state(project)["revision"],
        committed=journal["state_committed"],
        saved=journal["saved"],
        tokens={key: document_sha256(item["text"]) for key, item in journal["documents"].items()},
    )
    atomic_write_json(path, journal)
    return journal["result"]


def run_operation(
    project: Path,
    action: str,
    request: dict[str, Any],
    builder: Callable[[dict[str, Any]], tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]],
) -> dict[str, Any]:
    """Preflight and apply a named document/state operation with a durable retry journal."""
    path = journal_path(project, string_input(request.get("id")))
    ensure_writable(_load_state(project))
    path.parent.mkdir(exist_ok=True)
    with DirectoryLock(path.with_suffix(".lock")):
        return _run_operation(project, action, request, builder)


def _run_operation(
    project: Path,
    action: str,
    request: dict[str, Any],
    builder: Callable[[dict[str, Any]], tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]],
) -> dict[str, Any]:
    project = project.resolve()
    operation_id = string_input(request.get("id"))
    path = journal_path(project, operation_id)
    signature = document_sha256(json.dumps({"action": action, "request": request}, sort_keys=True))
    journal: dict[str, Any] = {}
    try:
        with DirectoryLock(project / ".project.lock"):
            if path.exists():
                journal = load_json(path)
                if journal.get("signature") != signature or journal.get("kind") != "metadata":
                    raise WorkspaceConflict("operation ID already used with different input")
            else:
                state = _load_state(project)
                ensure_writable(state)
                expected = request.get("expected_revision")
                if type(expected) is not int or expected != state["revision"]:
                    raise WorkspaceConflict("expected_revision must match current state")
                documents, patch, result = builder(state)
                tokens = request.get("tokens", {})
                if not isinstance(tokens, dict):
                    raise WorkspaceError("tokens must be a document-to-hash object")
                prepared = {}
                for name, body in documents.items():
                    before = snapshot(document_path(project, name))[1]
                    if tokens.get(name, "missing") != before:
                        raise WorkspaceConflict(f"document conflict: {name}; supply its current token")
                    prepared[name] = {"before": before, "text": body}
                candidate = prepare_update(state, patch) if patch is not None else None
                if candidate is not None:
                    report = check_state_candidate(project, candidate, expected_revision=expected, check_files=False)
                    if report.errors:
                        raise WorkspaceError("; ".join(report.errors))
                journal = {
                    "kind": "metadata",
                    "id": operation_id,
                    "signature": signature,
                    "expected_revision": expected,
                    "documents": prepared,
                    "candidate": candidate,
                    "saved": [],
                    "state_committed": False,
                    "complete": False,
                    "after_commit": result.pop("_after_commit", []),
                    "result": {"operation": action, **result},
                }
                atomic_write_json(path, journal)
            result = _advance(project, path, journal)
        if journal["state_committed"] and not journal["complete"]:
            _rebuild_index_after_commit(project.parent, "operation state is committed", 5.0)
        journal["complete"] = True
        result["complete"] = True
        atomic_write_json(path, journal)
        return result
    except (OSError, WorkspaceError) as error:
        landed = journal.get("state_committed", False)
        if journal.get("candidate") is not None:
            landed = landed or _landed(project, journal)
        saved = list(journal.get("saved", [])) if isinstance(journal.get("saved"), list) else []
        documents = journal.get("documents")
        if isinstance(documents, dict):
            for name, item in documents.items():
                if isinstance(item, dict) and isinstance(item.get("text"), str) and DOCUMENT.fullmatch(name):
                    if snapshot(document_path(project, name))[1] == document_sha256(item["text"]) and name not in saved:
                        saved.append(name)
        raise OperationError(
            str(error),
            {
                "operation": action,
                "operation_id": operation_id,
                "complete": False,
                "committed": landed,
                "saved": saved,
                "revision": _load_state(project)["revision"],
                "tokens": {name: snapshot(document_path(project, name))[1] for name in saved},
                "recovery": "inspect operation; retry the same ID/input or use recover; never replay commands",
                "error": str(error),
            },
        ) from error


def recover_operation(project: Path, operation_id: str, *, apply: bool = False) -> dict[str, Any]:
    """Inspect any saved operation; repair only metadata/index writes when explicitly requested."""
    project = project.resolve()
    path = journal_path(project, operation_id)
    with DirectoryLock(path.with_suffix(".lock")):
        return _recover_operation(project, path, apply=apply)


def _recover_operation(project: Path, path: Path, *, apply: bool) -> dict[str, Any]:
    journal = load_json(path)
    if not apply:
        return {key: journal.get(key) for key in ("kind", "id", "complete", "saved", "state_committed", "result")}
    if journal.get("kind") != "metadata":
        raise WorkspaceError("command/memory attempts require their own same-ID recovery; never generic replay")
    with DirectoryLock(project / ".project.lock"):
        result = _advance(project, path, journal)
    if journal["state_committed"] and not journal["complete"]:
        _rebuild_index_after_commit(project.parent, "operation state is committed", 5.0)
    journal["complete"] = True
    result["complete"] = True
    atomic_write_json(path, journal)
    return result
