"""Retry-safe selected memory triage, preserving unresolved staging and promotion receipts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workspace_documents import _headings, _section_span
from workspace_journal import (
    OperationError,
    document_path,
    ensure_writable,
    journal_path,
    list_input,
    object_input,
    run_operation,
    snapshot,
    string_input,
)
from workspace_lib import (
    DirectoryLock,
    WorkspaceError,
    amend_memory_topic,
    atomic_write_json,
    document_sha256,
    load_json,
    rebuild_index,
)
from workspace_session import _load_state


def triage(project: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Promote selected items before marking them resolved; retries deduplicate by durable identity."""
    object_input(
        request, {"id", "expected_revision", "tokens", "items"}, {"id", "expected_revision", "tokens", "items"}
    )
    path = journal_path(project, request["id"])
    if len(request["id"]) > 45:
        raise WorkspaceError("triage ID must be at most 45 characters")
    items = list_input(request["items"], 20)
    if not items:
        raise WorkspaceError("triage needs selected items")
    signature = document_sha256(json.dumps(request, sort_keys=True))
    journal: dict[str, Any] = {}
    try:
        ensure_writable(_load_state(project))
        path.parent.mkdir(exist_ok=True)
        with DirectoryLock(path.with_suffix(".lock")):
            if path.exists():
                journal = load_json(path)
                if journal.get("kind") != "memory" or journal.get("signature") != signature:
                    raise WorkspaceError("operation ID already used with different input")
                object_input(
                    journal,
                    {
                        "kind",
                        "id",
                        "signature",
                        "complete",
                        "documents",
                        "promotions",
                        "receipts",
                        "items",
                        "project",
                        "result",
                    },
                    {"documents", "promotions", "receipts", "project"},
                )
                if not isinstance(journal["documents"], dict) or not isinstance(journal["project"], str):
                    raise WorkspaceError("damaged memory journal")
                for name, body in journal["documents"].items():
                    if name not in ("memory-staging", "reflection") or not isinstance(body, str):
                        raise WorkspaceError("damaged memory document plan")
                promotions = list_input(journal["promotions"], 20)
                list_input(journal["receipts"], len(promotions))
                for promotion in promotions:
                    object_input(
                        promotion,
                        {"topic", "body", "description", "kind", "scope", "expected_sha256", "entry_id"},
                        {"topic", "body", "expected_sha256", "entry_id"},
                    )
                    for value in promotion.values():
                        string_input(value)
            else:
                with DirectoryLock(project / ".project.lock"):
                    state = _load_state(project)
                    ensure_writable(state)
                    if (
                        type(request["expected_revision"]) is not int
                        or request["expected_revision"] != state["revision"]
                    ):
                        raise WorkspaceError("revision conflict")
                    tokens = object_input(request["tokens"], {"memory-staging", "reflection"})
                    staging, token = snapshot(document_path(project, "memory-staging"))
                    reflection, reflection_token = snapshot(document_path(project, "reflection"))
                    if tokens.get("memory-staging") != token:
                        raise WorkspaceError("staging token conflict")
                    spans = []
                    promotions = []
                    seen = set()
                    for index, item in enumerate(items):
                        object_input(
                            item,
                            {"section", "disposition", "reason", "promotion"},
                            {"section", "disposition", "reason"},
                        )
                        section = string_input(item["section"])
                        reason = string_input(item["reason"])
                        if section in seen:
                            raise WorkspaceError("duplicate staged selection")
                        seen.add(section)
                        level, start, _, end = _section_span(staging, section)
                        if level != 2 or any(depth > 2 for depth, _, _ in _headings(staging[start:end])):
                            raise WorkspaceError("staged items must be independent level-two sections")
                        disposition = item["disposition"]
                        if disposition not in ("promote", "reflection", "discard"):
                            raise WorkspaceError("triage disposition must be promote, reflection or discard")
                        if disposition == "promote":
                            promotion = object_input(
                                item.get("promotion"),
                                {"topic", "body", "description", "kind", "scope", "expected_sha256"},
                                {"topic", "body", "expected_sha256"},
                            )
                            for value in promotion.values():
                                string_input(value)
                            promotions.append({**promotion, "entry_id": f"{state['project']}-{request['id']}-{index}"})
                        elif "promotion" in item:
                            raise WorkspaceError("promotion details require promote disposition")
                        if disposition == "reflection":
                            reflection += f"\n## Lesson: {section}\n\n{reason}\n\n{staging[start:end]}\n"
                        spans.append((start, end, ""))
                    for start, end, replacement in sorted(spans, reverse=True):
                        staging = staging[:start] + replacement + staging[end:]
                    staging += "\n<!-- Resolved lesson records: .operations/" + request["id"] + ".json -->\n"
                    documents = {"memory-staging": staging}
                    if any(item["disposition"] == "reflection" for item in items):
                        if tokens.get("reflection", "missing") != reflection_token:
                            raise WorkspaceError("reflection token conflict")
                        documents["reflection"] = reflection
                    journal = {
                        "kind": "memory",
                        "id": request["id"],
                        "signature": signature,
                        "complete": False,
                        "documents": documents,
                        "promotions": promotions,
                        "receipts": [],
                        "items": items,
                        "project": state["project"],
                    }
                    atomic_write_json(path, journal)
            for promotion in journal["promotions"][len(journal["receipts"]) :]:
                fields: dict[str, Any] = {key: value for key, value in promotion.items() if key != "topic"}
                destination = amend_memory_topic(
                    project.parent, promotion["topic"], sources=[journal["project"]], **fields
                )
                journal["receipts"].append(
                    {"path": str(destination), "sha256": snapshot(destination)[1], "entry_id": promotion["entry_id"]}
                )
                atomic_write_json(path, journal)
            local = {
                "id": request["id"] + "-local",
                "expected_revision": request["expected_revision"],
                "tokens": request["tokens"],
            }
            result = run_operation(
                project,
                "triage-record",
                local,
                lambda state: (journal["documents"], None, {"receipts": journal["receipts"]}),
            )
            rebuild_index(project.parent)
            journal["complete"] = True
            journal["result"] = result
            atomic_write_json(path, journal)
            return {**result, "operation_id": request["id"], "complete": True}
    except (OSError, WorkspaceError) as error:
        raise OperationError(
            str(error),
            {
                "operation_id": request["id"],
                "complete": False,
                "receipts": journal.get("receipts", []),
                "error": str(error),
                "recovery": "retry identical triage ID/input; promotions deduplicate; unresolved staging is retained",
            },
        ) from error
