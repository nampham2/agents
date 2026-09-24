"""Mechanical Markdown reporting; conclusions and acceptance remain caller-authored."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from workspace_journal import object_input, string_input
from workspace_lib import (
    REPORT_CANONICAL_SECTIONS,
    REPORT_GRAPH_PARENT,
    ValidationReport,
    WorkspaceError,
    _resolve_local_reference,
    _validate_evidence_reference,
    project_task_graph,
    read_text,
    render_task_graph,
)
from workspace_session import _headings


def _citation(project: Path, state: dict[str, Any], reference: dict[str, Any]) -> tuple[str, list[str]]:
    report = ValidationReport()
    _validate_evidence_reference(reference, "citation", project, Path(state["working_directory"]), True, report)
    if report.errors:
        return "unresolved citation", report.errors
    path = reference["path"]
    anchor = reference.get("anchor")
    if reference["root"] != "external":
        resolved, _ = _resolve_local_reference(project, Path(state["working_directory"]), reference["root"], path)
        assert resolved is not None
        path = quote(os.path.relpath(resolved, project / "artifacts"), safe="/")
        if anchor:
            text = read_text(resolved)
            headings = {re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-") for _, title, _ in _headings(text)}
            if anchor not in headings and not re.search(r'id=["\']' + re.escape(anchor) + r'["\']', text):
                report.errors.append(f"citation anchor missing: {reference['root']}:{reference['path']}#{anchor}")
    if anchor:
        path += "#" + quote(anchor, safe="")
    return f"[{reference['root']}:{reference['path']}]({path})", report.errors


def render_report(project: Path, state: dict[str, Any], request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Render requested prose, authoritative task accounting, citations and an optional graph."""
    sections = object_input(request.get("sections", {}), {name for name, _ in REPORT_CANONICAL_SECTIONS})
    if type(request.get("graph", False)) is not bool:
        raise WorkspaceError("graph must be boolean")
    text = "# " + state["title"].replace("\n", " ") + "\n"
    for name, _ in REPORT_CANONICAL_SECTIONS:
        body = string_input(sections[name]) if name in sections else "TODO: caller-authored findings."
        text += f"\n## {name}\n\n{body}\n"
        if request.get("graph") and name == REPORT_GRAPH_PARENT:
            text += "\n### Task graph\n\n```text\n" + render_task_graph(project_task_graph(project)) + "\n```\n"
    text += "\n## Task accounting\n\n| Task | Status | Evidence |\n| --- | --- | --- |\n"
    errors = []
    for task in state["tasks"]:
        citations = []
        for reference in task["evidence"]:
            citation, findings = _citation(project, state, reference)
            citations.append(citation.replace("|", "&#124;"))
            errors.extend(findings)
        text += f"| {task['id']} | {task['status']} | {'; '.join(citations) or 'None recorded'} |\n"
    return text, {
        "path": str(project / "artifacts/report.md"),
        "link_errors": errors,
        "unwritten_sections": [name for name, _ in REPORT_CANONICAL_SECTIONS if name not in sections],
        "acceptance": "scaffold and link integrity do not establish supported conclusions",
    }
