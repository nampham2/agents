"""Tests for the architecture review gate's tooling.

The project skill requires an agreed `architecture.md` before task planning, but until this change
nothing in the toolchain could see the file: resume context listed only spec, evidence and
reflection, validation never mentioned it, and the `read`/`edit` selectors refused it, leaving the
one canonical record that gates planning as the only one written without a content-token guard.

The validator check warns and never errors, for the reason the briefing check does: the file
postdates existing projects, and an error would invalidate history and block reopening a closed
project. Unlike the briefing, absence is reported, because a project in `PLANNING`, `EXECUTING` or
`REVIEW` has by definition passed the gate and a gate whose record can be silently missing is not
a gate. `BLOCKED` is reachable straight from `ALIGNING`, where a draft is the expected state, so it
is not checked.
"""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_documents import document_snapshot, edit_document
from workspace_lib import (
    ARCHITECTURE_GATE_STATUSES,
    PROJECT_STATUSES,
    WorkspaceError,
    allocate_project,
    architecture_status,
    architecture_warnings,
    document_sha256,
    validate_v3_state,
    validate_v4_state,
)
from workspace_session import validated_project_context

AGREED = "# Architecture A1\n\n**Status:** agreed (confirmed 2026-09-22 by the user)\n\n## Modules\n\nOne module.\n"
DRAFT = "# Architecture A1\n\nStatus: draft\n\n## Modules\n\nOne module.\n"
UNMARKED = "# Architecture A1\n\n## Modules\n\nOne module.\n"


def invoke(arguments: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with (
        patch.object(sys, "argv", ["research-project", *arguments]),
        patch.object(sys, "stdin", StringIO(stdin)),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        code = manage_workspace.main()
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    return allocate_project(workspace, title="Gate", working_directory=target)


def state(project: Path, status: str) -> dict[str, Any]:
    loaded = json.loads((project / "project.json").read_text(encoding="utf-8"))
    loaded["status"] = status
    return loaded


def architecture_findings(project: Path, status: str, **kwargs: Any) -> list[str]:
    report = validate_v4_state(state(project, status), project, **kwargs)
    assert report.valid, report.errors
    return [warning for warning in report.warnings if "architecture.md" in warning]


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Status: agreed", "agreed"),
        ("**Status:** Draft", "draft"),
        ("- Review A1 — status: agreed on 2026-09-22", "agreed"),
        ("# Architecture\n\nNo status line at all.\n", None),
        # `\bstatus\b` must not match inside a longer word, or prose about statuses would count.
        ("The statuses were agreed informally.", None),
        # The first occurrence wins, so a history section further down cannot override the header.
        ("Status: agreed\n\n## History\n\nA0 status: draft, superseded.\n", "agreed"),
        # The word and its value must share a line; a marker split across lines is not a marker.
        ("Status:\nagreed\n", None),
        ("Status:\r\n\r\nagreed\n", None),
        ("Status: **agreed**", "agreed"),
    ],
)
def test_the_status_line_is_read_in_any_markdown_dress(markdown: str, expected: str | None) -> None:
    assert architecture_status(markdown) == expected


def test_only_the_gate_statuses_are_checked(project: Path) -> None:
    # No file exists yet, so every checked status must warn and every other status must stay quiet.
    for status in sorted(PROJECT_STATUSES):
        warned = bool(architecture_warnings(project, status))
        assert warned == (status in ARCHITECTURE_GATE_STATUSES), status
    assert ARCHITECTURE_GATE_STATUSES == {"PLANNING", "EXECUTING", "REVIEW"}
    # Validation sees arbitrary JSON; an unhashable status must yield no finding rather than a crash.
    assert architecture_warnings(project, ["PLANNING"]) == []
    assert architecture_warnings(project, None) == []


def test_each_failure_is_named_distinctly(project: Path) -> None:
    assert architecture_warnings(project, "PLANNING") == [
        "architecture.md is missing; a PLANNING project needs an agreed architecture document"
    ]
    (project / "architecture.md").write_text(UNMARKED, encoding="utf-8")
    assert architecture_warnings(project, "PLANNING") == [
        "architecture.md has no recognisable 'Status: draft' or 'Status: agreed' line"
    ]
    (project / "architecture.md").write_text(DRAFT, encoding="utf-8")
    assert architecture_warnings(project, "EXECUTING") == [
        "architecture.md is still a draft; task planning requires an agreed revision"
    ]
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    assert architecture_warnings(project, "REVIEW") == []


def test_a_directory_named_architecture_md_reads_as_missing(project: Path) -> None:
    # `exists()` is true for a directory, and reading one raises; the gate must warn, not crash.
    (project / "architecture.md").mkdir()
    assert architecture_warnings(project, "PLANNING") == [
        "architecture.md is missing; a PLANNING project needs an agreed architecture document"
    ]
    assert architecture_findings(project, "PLANNING") == [
        "architecture.md is missing; a PLANNING project needs an agreed architecture document"
    ]


def test_validation_reports_the_gate_as_a_warning_not_an_error(project: Path) -> None:
    assert architecture_findings(project, "ALIGNING") == []
    assert architecture_findings(project, "PLANNING") == [
        "architecture.md is missing; a PLANNING project needs an agreed architecture document"
    ]
    assert architecture_findings(project, "PLANNING", check_files=False) == []
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    assert architecture_findings(project, "PLANNING") == []


def test_resume_context_carries_the_architecture_token(project: Path) -> None:
    before = validated_project_context(project)["documents"]["architecture"]
    assert before == {"path": str(project / "architecture.md"), "exists": False, "sha256": "missing"}
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    after = validated_project_context(project)["documents"]["architecture"]
    assert after["exists"] is True
    assert after["sha256"] == document_sha256(AGREED)


def test_the_document_is_edited_whole_under_the_token_guard(project: Path) -> None:
    assert document_snapshot(project, "architecture")["exists"] is False
    created = edit_document(project, "architecture", expected_sha256="missing", body=DRAFT)
    assert created["document"] == "architecture"
    assert (project / "architecture.md").read_text(encoding="utf-8") == DRAFT
    snapshot = document_snapshot(project, "architecture")
    assert snapshot["document_sha256"] == created["document_sha256"]
    assert snapshot["text"] == DRAFT
    # A stale token is refused, exactly as for the reflection.
    with pytest.raises(WorkspaceError, match="document conflict"):
        edit_document(project, "architecture", expected_sha256="missing", body=AGREED)
    agreed = edit_document(project, "architecture", expected_sha256=snapshot["document_sha256"], body=AGREED)
    assert architecture_status((project / "architecture.md").read_text(encoding="utf-8")) == "agreed"
    with pytest.raises(WorkspaceError, match="architecture edits require a body"):
        edit_document(project, "architecture", expected_sha256="x", sections={"Modules": "x"})
    with pytest.raises(WorkspaceError, match="architecture body must not be empty"):
        edit_document(project, "architecture", expected_sha256=agreed["document_sha256"], body="  \n")


def test_the_v3_validator_applies_the_same_gate(project: Path, tmp_path: Path) -> None:
    # Both schema validators carry the file checks; a v3 project past alignment is held to the gate too.
    v3_state = {
        "schema_version": 3,
        "project": project.name,
        "title": "Gate",
        "status": "PLANNING",
        "created": "2026-09-22T10:00:00+02:00",
        "updated": "2026-09-22T10:00:00+02:00",
        "working_directory": str(tmp_path / "target"),
        "revision": 0,
        "current_tasks": [],
        "review": {"cycle": 0, "required": False, "status": "not_required", "evidence": []},
        "cancellation_reason": None,
        "tasks": [],
    }
    report = validate_v3_state(v3_state, project)
    assert [warning for warning in report.warnings if "architecture.md" in warning] == [
        "architecture.md is missing; a PLANNING project needs an agreed architecture document"
    ]
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    report = validate_v3_state(v3_state, project)
    assert [warning for warning in report.warnings if "architecture.md" in warning] == []


def test_the_cli_reads_and_edits_the_document(project: Path) -> None:
    code, out, _ = invoke(["edit", str(project), "architecture", "--body-file", "-", "--expected-sha256", "missing"],
                          stdin=AGREED)
    assert code == 0, out
    token = json.loads(out)["document_sha256"]

    code, out, _ = invoke(["read", str(project), "architecture"])
    assert code == 0
    read = json.loads(out)
    assert read["document_sha256"] == token
    assert read["text"] == AGREED

    code, _, err = invoke(["read", str(project), "architecture", "--section", "Modules"])
    assert code != 0
    assert "do not accept --section" in err

    code, _, err = invoke(["edit", str(project), "architecture", "--section", "Modules", "--body", "x",
                           "--expected-sha256", token])
    assert code != 0
    assert "architecture edit does not accept --section" in err

    code, _, err = invoke(["edit", str(project), "architecture", "--body", "x", "--expected-sha256", "stale"])
    assert code != 0
    assert "document conflict" in err
