"""Tests for the planning review gate's tooling.

The project skill requires an agreed `planning.md` before task planning (a root `architecture.md`
in a project begun before the split), and nothing in the toolchain could see the file at first:
resume context listed only spec, evidence and reflection, validation never mentioned it, and the
`read`/`edit` selectors refused it, leaving the one canonical record that gates planning as the only
one written without a content-token guard.

The validator check warns and never errors, for the reason the briefing check does: the file
postdates existing projects, and an error would invalidate history and block reopening a closed
project. Unlike the briefing, absence is reported, because a project in `PLANNING`, `EXECUTING` or
`REVIEW` has by definition passed the gate and a gate whose record can be silently missing is not
a gate. `BLOCKED` is reachable straight from `ALIGNING`, where a draft is the expected state, so it
is not checked.

`artifacts/architecture.md` is the living design, written after agreement and never gated.
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
import workspace_lib
from workspace_context import checkpoint_data, checkpoint_freshness
from workspace_documents import document_snapshot, edit_document
from workspace_lib import (
    DESIGN_DOCUMENT,
    PLANNING_GATE_STATUSES,
    PROJECT_STATUSES,
    WorkspaceError,
    allocate_project,
    document_sha256,
    planning_status,
    planning_warnings,
    validate_v3_state,
    validate_v4_state,
)
from workspace_session import validated_project_context

AGREED = "# Planning A1\n\n**Status:** agreed (confirmed 2026-09-22 by the user)\n\n## Modules\n\nOne module.\n"
DRAFT = "# Planning A1\n\nStatus: draft\n\n## Modules\n\nOne module.\n"
UNMARKED = "# Planning A1\n\n## Modules\n\nOne module.\n"


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


def planning_findings(project: Path, status: str, **kwargs: Any) -> list[str]:
    report = validate_v4_state(state(project, status), project, **kwargs)
    assert report.valid, report.errors
    return [warning for warning in report.warnings if "planning.md" in warning]


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Status: agreed", "agreed"),
        ("**Status:** Draft", "draft"),
        ("- Review A1 — status: agreed on 2026-09-22", "agreed"),
        ("# Planning\n\nNo status line at all.\n", None),
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
    assert planning_status(markdown) == expected


def test_only_the_gate_statuses_are_checked(project: Path) -> None:
    # No file exists yet, so every checked status must warn and every other status must stay quiet.
    for status in sorted(PROJECT_STATUSES):
        warned = bool(planning_warnings(project, status))
        assert warned == (status in PLANNING_GATE_STATUSES), status
    assert PLANNING_GATE_STATUSES == {"PLANNING", "EXECUTING", "REVIEW"}
    # Validation sees arbitrary JSON; an unhashable status must yield no finding rather than a crash.
    assert planning_warnings(project, ["PLANNING"]) == []
    assert planning_warnings(project, None) == []


def test_each_failure_is_named_distinctly(project: Path) -> None:
    assert planning_warnings(project, "PLANNING") == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]
    (project / "planning.md").write_text(UNMARKED, encoding="utf-8")
    assert planning_warnings(project, "PLANNING") == [
        "planning.md has no recognisable 'Status: draft' or 'Status: agreed' line"
    ]
    (project / "planning.md").write_text(DRAFT, encoding="utf-8")
    assert planning_warnings(project, "EXECUTING") == [
        "planning.md is still a draft; task planning requires an agreed revision"
    ]
    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    assert planning_warnings(project, "REVIEW") == []


def test_a_directory_named_planning_md_reads_as_missing(project: Path) -> None:
    # `exists()` is true for a directory, and reading one raises; the gate must warn, not crash.
    (project / "planning.md").mkdir()
    assert planning_warnings(project, "PLANNING") == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]
    assert planning_findings(project, "PLANNING") == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]


def test_validation_reports_the_gate_as_a_warning_not_an_error(project: Path) -> None:
    assert planning_findings(project, "ALIGNING") == []
    assert planning_findings(project, "PLANNING") == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]
    assert planning_findings(project, "PLANNING", check_files=False) == []
    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    assert planning_findings(project, "PLANNING") == []


def test_resume_context_carries_the_planning_token(project: Path) -> None:
    before = validated_project_context(project)["documents"]["planning"]
    assert before == {"path": str(project / "planning.md"), "exists": False, "sha256": "missing"}
    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    after = validated_project_context(project)["documents"]["planning"]
    assert after["exists"] is True
    assert after["sha256"] == document_sha256(AGREED)


def test_the_document_is_edited_whole_under_the_token_guard(project: Path) -> None:
    assert document_snapshot(project, "planning")["exists"] is False
    created = edit_document(project, "planning", expected_sha256="missing", body=DRAFT)
    assert created["document"] == "planning"
    assert (project / "planning.md").read_text(encoding="utf-8") == DRAFT
    snapshot = document_snapshot(project, "planning")
    assert snapshot["document_sha256"] == created["document_sha256"]
    assert snapshot["text"] == DRAFT
    # A stale token is refused, exactly as for the reflection.
    with pytest.raises(WorkspaceError, match="document conflict"):
        edit_document(project, "planning", expected_sha256="missing", body=AGREED)
    agreed = edit_document(project, "planning", expected_sha256=snapshot["document_sha256"], body=AGREED)
    assert planning_status((project / "planning.md").read_text(encoding="utf-8")) == "agreed"
    with pytest.raises(WorkspaceError, match="planning edits require a body"):
        edit_document(project, "planning", expected_sha256="x", sections={"Modules": "x"})
    with pytest.raises(WorkspaceError, match="planning body must not be empty"):
        edit_document(project, "planning", expected_sha256=agreed["document_sha256"], body="  \n")


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
    assert [warning for warning in report.warnings if "planning.md" in warning] == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]
    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    report = validate_v3_state(v3_state, project)
    assert [warning for warning in report.warnings if "planning.md" in warning] == []


def test_the_cli_reads_and_edits_the_document(project: Path) -> None:
    code, out, _ = invoke(["edit", str(project), "planning", "--body-file", "-", "--expected-sha256", "missing"],
                          stdin=AGREED)
    assert code == 0, out
    token = json.loads(out)["document_sha256"]

    code, out, _ = invoke(["read", str(project), "planning"])
    assert code == 0
    read = json.loads(out)
    assert read["document_sha256"] == token
    assert read["text"] == AGREED

    code, _, err = invoke(["read", str(project), "planning", "--section", "Modules"])
    assert code != 0
    assert "do not accept --section" in err

    code, _, err = invoke(["edit", str(project), "planning", "--section", "Modules", "--body", "x",
                           "--expected-sha256", token])
    assert code != 0
    assert "planning edit does not accept --section" in err

    code, _, err = invoke(["edit", str(project), "planning", "--body", "x", "--expected-sha256", "stale"])
    assert code != 0
    assert "document conflict" in err


def test_a_legacy_root_architecture_md_is_the_planning_document(project: Path) -> None:
    (project / "architecture.md").write_text(DRAFT, encoding="utf-8")
    assert planning_warnings(project, "PLANNING") == [
        "architecture.md is still a draft; task planning requires an agreed revision"
    ]
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    assert planning_warnings(project, "PLANNING") == []
    snapshot = document_snapshot(project, "planning")
    assert snapshot["path"] == str(project / "architecture.md")
    assert snapshot["text"] == AGREED
    context = validated_project_context(project)["documents"]
    assert context["planning"]["path"] == str(project / "architecture.md")
    assert context["planning"]["exists"] is True
    # Editing the planning document in such a project writes the file it already has, not a second one.
    edit_document(project, "planning", expected_sha256=snapshot["document_sha256"], body=DRAFT)
    assert (project / "architecture.md").read_text(encoding="utf-8") == DRAFT
    assert not (project / "planning.md").exists()


def test_planning_md_wins_over_a_root_architecture_md_with_a_warning(project: Path) -> None:
    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    (project / "architecture.md").write_text(DRAFT, encoding="utf-8")
    found = planning_warnings(project, "PLANNING")
    assert len(found) == 1
    assert "both exist" in found[0]
    assert "planning.md is used" in found[0]
    assert document_snapshot(project, "planning")["text"] == AGREED
    # Closed projects stay quiet, so history is never re-judged.
    assert planning_warnings(project, "DONE") == []


def test_a_project_created_after_the_cutoff_that_keeps_only_the_root_file_is_told_to_rename(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    loaded = state(project, "PLANNING")
    monkeypatch.setattr(workspace_lib, "PLANNING_SPLIT_FROM", "2000-01-01T00:00:00+00:00")
    found = planning_warnings(project, "PLANNING", loaded)
    assert len(found) == 1
    assert found[0].startswith("rename the root architecture.md to planning.md")
    assert DESIGN_DOCUMENT in found[0]
    # A project begun before the cutoff, and a caller with no state to judge by, are left alone.
    monkeypatch.setattr(workspace_lib, "PLANNING_SPLIT_FROM", "2999-01-01T00:00:00+00:00")
    assert planning_warnings(project, "PLANNING", loaded) == []
    assert planning_warnings(project, "PLANNING") == []
    # The rename itself clears the warning.
    monkeypatch.setattr(workspace_lib, "PLANNING_SPLIT_FROM", "2000-01-01T00:00:00+00:00")
    (project / "architecture.md").rename(project / "planning.md")
    assert planning_warnings(project, "PLANNING", loaded) == []


def test_the_design_document_is_a_separate_unguarded_file_under_artifacts(project: Path) -> None:
    missing = document_snapshot(project, "architecture")
    assert missing["exists"] is False
    assert missing["path"] == str(project / DESIGN_DOCUMENT)
    assert "hint" not in missing
    created = edit_document(project, "architecture", expected_sha256="missing", body="# Design\n\nOne module.\n")
    assert (project / DESIGN_DOCUMENT).read_text(encoding="utf-8") == "# Design\n\nOne module.\n"
    # It is not the agreement: no stale-confirmation report, and the planning gate still sees nothing.
    assert "stale_confirmations" not in created
    assert not (project / "planning.md").exists()
    assert planning_warnings(project, "PLANNING") == [
        "planning.md is missing; a PLANNING project needs an agreed planning document"
    ]
    after = validated_project_context(project)["documents"]
    assert after["architecture"]["path"] == str(project / DESIGN_DOCUMENT)
    assert after["architecture"]["exists"] is True
    assert after["planning"]["exists"] is False
    with pytest.raises(WorkspaceError, match="document conflict"):
        edit_document(project, "architecture", expected_sha256="missing", body="# Other\n")


def test_a_legacy_project_is_refused_the_design_id_with_a_hint_to_use_planning(project: Path) -> None:
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    read = document_snapshot(project, "architecture")
    assert read["exists"] is False
    assert "use the document id planning" in read["hint"]
    with pytest.raises(WorkspaceError, match="use the document id planning"):
        edit_document(project, "architecture", expected_sha256="missing", body=DRAFT)
    assert (project / "architecture.md").read_text(encoding="utf-8") == AGREED
    assert not (project / "artifacts").exists()


def old_checkpoint(project: Path, sources: dict[str, str]) -> None:
    """Write a handoff the way a plugin before the split did: the agreement is the `architecture` source."""
    metadata = {
        "version": 1, "revision": 0, "sources": sources, "git": {"available": False},
        "continuation": {"next": "Plan"},
    }
    (project / "handoff.md").write_text(
        "<!-- research-checkpoint-v1\n" + json.dumps(metadata) + "\n-->\n# Continuation\n", encoding="utf-8"
    )


def test_an_old_checkpoint_architecture_source_is_the_planning_document(project: Path) -> None:
    (project / "architecture.md").write_text(AGREED, encoding="utf-8")
    spec = document_sha256((project / "spec.md").read_text(encoding="utf-8"))
    old_checkpoint(project, {"spec": spec, "architecture": document_sha256(AGREED)})
    assert checkpoint_data(project) is not None
    assert checkpoint_freshness(project)["sources"] == {"spec": "unchanged", "architecture": "unchanged"}
    (project / "architecture.md").write_text(DRAFT, encoding="utf-8")
    assert checkpoint_freshness(project)["sources"]["architecture"] == "changed"
    # The same key in a project with a design document and no agreement does not read the design.
    (project / "architecture.md").unlink()
    edit_document(project, "architecture", expected_sha256="missing", body="# Design\n")
    assert checkpoint_freshness(project)["sources"]["architecture"] == "missing"


def test_a_new_checkpoint_records_the_planning_source(project: Path) -> None:
    from workspace_workflows import workflow

    (project / "planning.md").write_text(AGREED, encoding="utf-8")
    result = workflow(
        project, "checkpoint",
        {"id": "cp", "expected_revision": 0, "tokens": {}, "continuation": {"next": "Plan", "session": "working"}},
    )
    assert result
    recorded = checkpoint_data(project)
    assert recorded is not None
    sources = recorded["sources"]
    assert set(sources) == {"spec", "planning"}
    assert sources["planning"] == document_sha256(AGREED)
