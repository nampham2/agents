"""Targeted reads retain requirements/evidence and keep unrelated history out of context."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_lib import WorkspaceError, atomic_write_json, validate_project
from workspace_session import list_projects, project_context, read_project_text

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_report_contract import GOOD_HTML, GOOD_MARKDOWN
from tests.plugins.research.project.test_workspace import WorkspaceFixture


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceFixture:
    fixture = WorkspaceFixture(tmp_path)
    atomic_write_json(fixture.project_dir / "project.json", fixture.state())
    return fixture


def test_task_only_preserves_complete_assignment_without_reading_logs(workspace: WorkspaceFixture) -> None:
    expected = project_context(workspace.project_dir, task_id="T01", worker=True)
    (workspace.project_dir / "spec.md").write_bytes(b"\xff")
    (workspace.project_dir / "evidence.md").write_bytes(b"\xff")
    actual = project_context(workspace.project_dir, task_id="T01", task_only=True)
    assert actual == {**expected, "role": "coordinator"}
    with pytest.raises(WorkspaceError, match="requires --task"):
        project_context(workspace.project_dir, task_only=True)
    with pytest.raises(WorkspaceError, match="choose"):
        project_context(workspace.project_dir, task_id="T01", task_only=True, worker=True)


def test_spec_pages_reassemble_exact_section_and_ignore_fenced_headings(workspace: WorkspaceFixture) -> None:
    selected = "### Constraints\nKeep all requirements.\n````text\n## Decision history\n```\n````\n" + "é" * 8100 + "\n"
    (workspace.project_dir / "spec.md").write_text(
        "# Specification\n## Current specification\n### Goal\nOutput.\n" + selected
        + "### Verification\nRun checks.\n## Decision history\nOld decisions.\n", encoding="utf-8",
    )
    result = read_project_text(workspace.project_dir, "spec", section="Constraints", max_chars=1000)
    assert result["total_chars"] == len(selected)
    pages = [result["text"]]
    while result["next_offset"] is not None:
        result = read_project_text(
            workspace.project_dir, "spec", section="Constraints", offset=result["next_offset"], max_chars=1000,
        )
        pages.append(result["text"])
    assert "".join(pages) == selected
    current = read_project_text(workspace.project_dir, "spec", max_chars=20000)
    assert "Old decisions" not in current["text"]
    assert current["truncated"] is False
    history = read_project_text(workspace.project_dir, "spec", section="Decision history")
    assert history["text"] == "## Decision history\nOld decisions.\n"
    with pytest.raises(WorkspaceError, match="exactly one"):
        read_project_text(workspace.project_dir, "spec", section="Absent")
    (workspace.project_dir / "spec.md").write_text("### Goal\nx\n### Goal\ny\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="exactly one"):
        read_project_text(workspace.project_dir, "spec", section="Goal")


def test_evidence_selection_preserves_failures_and_separates_owners(workspace: WorkspaceFixture) -> None:
    first = "## T01 — check\nExit code: 1 (FAILED)\n~~~\n## T02 — fake heading\n~~~\n"
    second = "## T01\nExit code: 0 (passed)\n"
    report = "## report — validation\nExit code: 0 (passed)\n"
    text = "# Evidence\n" + first + "## T010 — unrelated\nSecret unrelated output\n" + second + report
    (workspace.project_dir / "evidence.md").write_text(text, encoding="utf-8")
    selected = read_project_text(workspace.project_dir, "evidence", task_id="T01")
    assert selected["text"] == first + second
    assert selected["truncated"] is False
    assert read_project_text(workspace.project_dir, "evidence", step="report")["text"] == report
    assert read_project_text(workspace.project_dir, "evidence")["text"] == text
    state = workspace.state()
    state["tasks"].append(workspace.task("T02"))
    atomic_write_json(workspace.project_dir / "project.json", state)
    assert read_project_text(workspace.project_dir, "evidence", task_id="T02")["total_chars"] == 0


@pytest.mark.parametrize("document,options,message", [
    ("other", {}, "document"),
    ("spec", {"offset": -1}, "offset"),
    ("spec", {"max_chars": 20001}, "max-chars"),
    ("spec", {"task_id": "T01"}, "use --section"),
    ("evidence", {"section": "T01"}, "use --section"),
    ("evidence", {"task_id": "T01", "step": "report"}, "use --section"),
    ("evidence", {"task_id": "absent"}, "unknown task"),
    ("evidence", {"step": "absent"}, "unknown closure"),
    ("spec", {"offset": 10000}, "beyond"),
])
def test_bad_read_requests_are_actionable(
    workspace: WorkspaceFixture, document: str, options: dict[str, Any], message: str,
) -> None:
    with pytest.raises(WorkspaceError, match=message):
        read_project_text(workspace.project_dir, document, **options)


def test_reads_fail_on_unreadable_source(workspace: WorkspaceFixture) -> None:
    (workspace.project_dir / "spec.md").write_bytes(b"\xff")
    with pytest.raises(WorkspaceError):
        read_project_text(workspace.project_dir, "spec")


def test_discovery_filters_pages_and_keeps_uncertain_records(workspace: WorkspaceFixture) -> None:
    root = workspace.workspace_root
    for index in range(12):
        path = root / f"2026-09-20-{index:03}"
        path.mkdir()
        atomic_write_json(path / "project.json", {**workspace.state(), "title": f"Parser {index}"})
    invalid = root / "invalid"
    invalid.mkdir()
    (invalid / "project.json").write_text("[]", encoding="utf-8")
    legacy = root / "legacy"
    legacy.mkdir()
    (legacy / "00_meta.yaml").touch()
    (legacy / "02_task_plan.md").touch()
    (root / "empty").mkdir()
    (root / ".private").mkdir()
    result = list_projects(root, query="parser", status="DONE", limit=3)
    assert result["total"] == 14
    assert len(result["projects"]) == 3
    assert result["next_offset"] == 3
    assert {row["status"] for row in result["projects"]} >= {"LEGACY", "INVALID"}
    assert list_projects(root, query="absent", status="EXECUTING")["total"] == 2
    assert list_projects(root, offset=100)["next_offset"] is None
    with pytest.raises(WorkspaceError, match="limit"):
        list_projects(root, limit=0)
    with pytest.raises(WorkspaceError, match="cannot list"):
        list_projects(root / "missing")
    atomic_write_json(invalid / "project.json", {"title": []})
    assert list_projects(root, query="absent")["total"] == 2
    long = {**workspace.state(), "title": "x" * 1000}
    atomic_write_json(workspace.project_dir / "project.json", long)
    matching = list_projects(root, query="x" * 600)["projects"]
    shortened = next(row for row in matching if row["project"] == workspace.project_dir.name)
    assert shortened["title"] == "x" * 500
    assert shortened["truncated_fields"] == ["title"]


def test_bounded_memory_cli_ranks_topics_and_keeps_postmortems_opt_in(
    workspace: WorkspaceFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace.project_dir / "reflection.md").write_text("\n".join("needle " + "x" * 600 for _ in range(12)))
    memory = workspace.workspace_root / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "needle-topic.md").write_text(
        "---\nname: needle-topic\ndescription: A needle lesson\nkind: method\nscope: anywhere\n"
        "sources: \nupdated: 2026-09-09\n---\n\nThe needle sits in the body.\n"
    )
    base = ["research-project", "search-memory", "needle", "--workspace-root", str(workspace.workspace_root)]
    with patch.object(sys, "argv", [*base, "--limit", "2"]):
        assert manage_workspace.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert [hit["topic"] for hit in result["matches"]] == ["needle-topic"]
    assert result["matches"][0]["excerpt"] == "The needle sits in the body."
    assert result["total"] == 1 and result["next_offset"] is None
    assert "postmortems" not in result
    with patch.object(sys, "argv", [*base, "--limit", "2", "--include-postmortems"]):
        assert manage_workspace.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["postmortem_total"] == 12
    assert len(result["postmortems"]) == 2
    assert all(hit["truncated"] and len(hit["excerpt"]) == 160 for hit in result["postmortems"])
    with patch.object(sys, "argv", [*base, "--limit", "0"]):
        assert manage_workspace.main() == 1
    assert "limit" in capsys.readouterr().err


def test_context_surfaces_bounded_memory_candidates_only_when_the_root_has_memory(workspace: WorkspaceFixture) -> None:
    before = project_context(workspace.project_dir)
    assert "memory_candidates" not in before
    memory = workspace.workspace_root / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "titled.md").write_text(
        "---\nname: titled\ndescription: Matches the project title\nkind: method\nscope: anywhere\n"
        f"sources: \nupdated: 2026-09-09\n---\n\n{before['title']}\n"
    )
    after = project_context(workspace.project_dir)
    assert [item["topic"] for item in after["memory_candidates"]] == ["titled"]
    assert len(json.dumps(after["memory_candidates"]).encode()) <= 600
    assert "memory_candidates" not in project_context(workspace.project_dir, task_id="T01", task_only=True)


def test_concise_reports_keep_evidence_sections_and_legacy_graph_checks(workspace: WorkspaceFixture) -> None:
    artifacts = workspace.project_dir / "artifacts"
    (artifacts / "report.md").write_text(GOOD_MARKDOWN.replace("### Task graph\n\nWritten.\n\n", ""))
    (artifacts / "report.html").write_text(GOOD_HTML.replace("<h3>Task graph</h3>\n<p>Written.</p>\n", ""))
    for report_format in ("markdown", "html", "both"):
        assert validate_project(workspace.project_dir, report_format=report_format, report_profile="concise").valid
    strict = validate_project(workspace.project_dir, check_report=True)
    assert any("Task graph" in error for error in strict.errors)
    close = validate_project(workspace.project_dir, close=True)
    assert not any("Task graph" in warning for warning in close.warnings)
    (artifacts / "report.md").write_text("# Incomplete\n")
    assert not validate_project(workspace.project_dir, report_format="markdown", report_profile="concise").valid
    assert not validate_project(workspace.project_dir, report_profile="concise").valid
    assert not validate_project(workspace.project_dir, report_format="both", report_profile="unknown").valid


@pytest.mark.parametrize("surface", ["bin", "skills/project/scripts"])
def test_targeted_commands_through_both_launchers(workspace: WorkspaceFixture, surface: str) -> None:
    launcher = REPO_ROOT / "plugins/research" / surface / "research-project"
    commands = [
        ["context", str(workspace.project_dir), "--task", "T01", "--task-only"],
        ["read", str(workspace.project_dir), "spec", "--section", "Current specification"],
        ["read", str(workspace.project_dir), "evidence", "--task", "T01"],
        ["list-projects", str(workspace.workspace_root), "--query", "Test"],
    ]
    for command in commands:
        result = subprocess.run([str(launcher), *command], text=True, capture_output=True, check=True)
        assert json.loads(result.stdout)
    validator = launcher.with_name("research-validate")
    (workspace.project_dir / "artifacts/report.md").write_text(
        GOOD_MARKDOWN.replace("### Task graph\n\nWritten.\n\n", "")
    )
    command = [str(validator), str(workspace.project_dir), "--report-format", "markdown"]
    concise = subprocess.run([*command, "--report-profile", "concise"], text=True, capture_output=True)
    assert concise.returncode == 0, concise.stderr
    strict = subprocess.run(command, text=True, capture_output=True)
    assert strict.returncode == 1
    assert "Task graph" in strict.stderr


def test_cli_read_and_discovery(workspace: WorkspaceFixture, capsys: pytest.CaptureFixture[str]) -> None:
    for command in (
        ["read", str(workspace.project_dir), "spec"],
        ["list-projects", str(workspace.workspace_root)],
    ):
        with patch.object(sys, "argv", ["research-project", *command]):
            assert manage_workspace.main() == 0
        assert json.loads(capsys.readouterr().out)
