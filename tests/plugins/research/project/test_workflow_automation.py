"""High-level workflow helpers remove coordinator bookkeeping without weakening guards."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_documents
import workspace_lib
import workspace_operations
import workspace_session
from workspace_documents import append_record, document_snapshot, edit_document
from workspace_evidence import evidence_entries, selected_evidence_references
from workspace_lib import (
    SPEC_CANONICAL_SECTIONS,
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    record_evidence_result,
)
from workspace_operations import CloseOperationError, close_project, task_operation
from workspace_session import update_project_data, validated_project_context

from tests.conftest import MANAGER


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


def task(task_id: str, *, depends_on: list[str] | None = None) -> dict[str, object]:
    return {
        "id": task_id,
        "name": f"Complete {task_id}",
        "success_criteria": f"{task_id} is verified",
        "verification": "Run a real command",
        "depends_on": depends_on or [],
        "effect": {"kind": "none"},
        "outputs": [],
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    return allocate_project(root, title="Automated lifecycle", working_directory=target)


def fill_spec(project: Path) -> dict[str, Any]:
    snapshot = document_snapshot(project, "spec")
    sections = {heading: f"Content for {heading}." for heading, _ in SPEC_CANONICAL_SECTIONS}
    return edit_document(project, "spec", expected_sha256=snapshot["document_sha256"], sections=sections)


def plan(project: Path) -> dict[str, object]:
    fill_spec(project)
    return update_project_data(
        project,
        {"status": "PLANNING", "tasks": [task("T01"), task("T02", depends_on=["T01"])]},
        expected_revision=0,
    )


def test_complete_lifecycle_uses_structured_evidence_and_one_finish_transition(project: Path) -> None:
    plan(project)
    assert task_operation(project, "start", "T01", expected_revision=1)["revision"] == 2
    first = record_evidence_result(project, "T01", [sys.executable, "-c", "print('verified')"])
    finished = task_operation(
        project, "finish", "T01", expected_revision=2,
        evidence_record_ids=[first.record_id], start_next="T02",
    )
    assert finished["changed_tasks"] == [{"id": "T01", "status": "DONE"}, {"id": "T02", "status": "RUNNING"}]
    second = record_evidence_result(project, "T02", [sys.executable, "-c", "pass"])
    task_operation(project, "finish", "T02", expected_revision=3, evidence_record_ids=[second.record_id])
    closed = close_project(
        project, expected_revision=4, reflection="# Reflection\n\nOutcome complete. No known limitations.\n",
        expected_reflection_sha256="missing",
    )
    assert closed["status"] == "DONE"
    assert closed["validation"]["valid"] is True


def test_failed_or_wrong_task_evidence_cannot_finish(project: Path) -> None:
    plan(project)
    task_operation(project, "start", "T01", expected_revision=1)
    failed = record_evidence_result(project, "T01", [sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(WorkspaceError, match="did not pass"):
        task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[failed.record_id])
    other = record_evidence_result(project, "T02", [sys.executable, "-c", "pass"])
    with pytest.raises(WorkspaceError, match="belongs to T02"):
        task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[other.record_id])


def test_evidence_listing_preserves_failure_and_pass(project: Path) -> None:
    plan(project)
    record_evidence_result(project, "T01", [sys.executable, "-c", "raise SystemExit(1)"])
    passed = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    result = evidence_entries(project, task_id="T01")
    assert [entry["passed"] for entry in result["entries"]] == [False, True]
    exact = evidence_entries(project, record_id=passed.record_id, include_text=True)
    assert exact["entries"][0]["record_id"] == passed.record_id
    assert "Exit code: 0" in exact["entries"][0]["text"]


def test_batch_spec_edit_preserves_history_and_rejects_stale_token(project: Path) -> None:
    before = (project / "spec.md").read_text(encoding="utf-8")
    result = fill_spec(project)
    after = (project / "spec.md").read_text(encoding="utf-8")
    assert "Project initialized" in before and "Project initialized" in after
    assert all(f"### {heading}" in after for heading, _ in SPEC_CANONICAL_SECTIONS)
    with pytest.raises(WorkspaceConflict, match="document conflict"):
        edit_document(
            project, "spec", expected_sha256=result["previous_sha256"],
            sections={"Objective and audience": "Changed"},
        )


def test_section_edit_preserves_crlf_and_rejects_parent_heading(project: Path) -> None:
    fill_spec(project)
    path = project / "spec.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    snapshot = document_snapshot(project, "spec")
    edit_document(
        project, "spec", expected_sha256=snapshot["document_sha256"],
        sections={"Objective and audience": "Audience is maintainers."},
    )
    assert b"\r\n" in path.read_bytes()
    snapshot = document_snapshot(project, "spec")
    with pytest.raises(WorkspaceError, match="at or above"):
        edit_document(
            project, "spec", expected_sha256=snapshot["document_sha256"],
            sections={"Objective and audience": "## Escapes the section"},
        )


def test_decisions_and_findings_are_idempotent_and_preserve_notes(project: Path) -> None:
    plan(project)
    decision = append_record(project, "decision", "Use the guarded writer.", entry_id="decision-one")
    repeated = append_record(project, "decision", "Use the guarded writer.", entry_id="decision-one")
    assert decision["existing"] is False and repeated["existing"] is True
    with pytest.raises(WorkspaceConflict, match="different content"):
        append_record(project, "decision", "Different", entry_id="decision-one")
    notes = project / "tasks" / "T01.md"
    notes.write_text("# Existing worker note\n\nHandle: worker-1\n", encoding="utf-8")
    append_record(project, "finding", "Parser behavior confirmed.", task_id="T01", entry_id="finding-one")
    assert "worker-1" in notes.read_text(encoding="utf-8")


def test_validated_context_combines_bounded_context_and_findings(project: Path) -> None:
    plan(project)
    result = validated_project_context(project)
    assert result["revision"] == 1
    assert result["validation"]["valid"] is True
    assert result["documents"]["spec"]["sha256"]
    assert result["roots"]["workspace"] == str(project)


def test_task_block_skip_and_revision_guards(project: Path) -> None:
    plan(project)
    blocked = task_operation(project, "block", "T01", expected_revision=1, reason="Need input")
    assert blocked["changed_tasks"][0]["status"] == "BLOCKED"
    with pytest.raises(WorkspaceError, match="revision conflict"):
        task_operation(project, "start", "T01", expected_revision=1)
    assert task_operation(project, "start", "T01", expected_revision=2)["status"] == "EXECUTING"
    skipped = task_operation(project, "skip", "T01", expected_revision=3, reason="Withdrawn")
    assert skipped["changed_tasks"][0]["status"] == "SKIPPED"


def test_cli_accepts_stdin_updates_and_validated_context(project: Path) -> None:
    fill_spec(project)
    patch = json.dumps({"status": "PLANNING", "tasks": [task("T01")]})
    updated = subprocess.run(
        [sys.executable, str(MANAGER), "update", str(project), "-", "--expected-revision", "0", "--json"],
        input=patch, text=True, capture_output=True, check=True,
    )
    assert json.loads(updated.stdout)["revision"] == 1
    context = subprocess.run(
        [sys.executable, str(MANAGER), "context", str(project), "--validate"],
        text=True, capture_output=True, check=True,
    )
    assert json.loads(context.stdout)["validation"]["valid"] is True


def test_cli_rejects_non_object_stdin_and_incompatible_context(project: Path) -> None:
    invalid = subprocess.run(
        [sys.executable, str(MANAGER), "update", str(project), "-", "--expected-revision", "0"],
        input="[]", text=True, capture_output=True,
    )
    assert invalid.returncode == 1
    assert "must be an object" in invalid.stderr
    invalid_context = subprocess.run(
        [sys.executable, str(MANAGER), "context", str(project), "--validate", "--task", "T01"],
        text=True, capture_output=True,
    )
    assert invalid_context.returncode == 1
    assert "cannot be combined" in invalid_context.stderr


def test_in_process_cli_covers_the_automated_lifecycle(project: Path) -> None:
    snapshot = document_snapshot(project, "spec")
    sections = {heading: heading for heading, _ in SPEC_CANONICAL_SECTIONS}
    code, output, _ = invoke([
        "edit", str(project), "spec", "--sections-json", "-", "--expected-sha256",
        snapshot["document_sha256"],
    ], stdin=json.dumps(sections))
    assert code == 0 and json.loads(output)["document"] == "spec"
    code, output, _ = invoke(
        ["update", str(project), "-", "--expected-revision", "0", "--json"],
        stdin=json.dumps({"status": "PLANNING", "tasks": [task("T01")]}),
    )
    assert code == 0 and json.loads(output)["revision"] == 1
    assert invoke(["context", str(project), "--validate"])[0] == 0
    assert invoke(["read", str(project), "spec", "--outline"])[0] == 0
    assert invoke(["append", str(project), "decision", "--body", "Decision", "--entry-id", "cli-decision"])[0] == 0
    assert invoke([
        "append", str(project), "finding", "--task", "T01", "--body", "Finding", "--entry-id", "cli-finding",
    ])[0] == 0
    assert invoke(["read", str(project), "notes", "--task", "T01"])[0] == 0
    assert invoke(["task", str(project), "start", "T01", "--expected-revision", "1"])[0] == 0
    code, output, _ = invoke([
        "record-evidence", str(project), "--task", "T01", "--json", "--",
        sys.executable, "-c", "pass",
    ])
    assert code == 0
    record_id = json.loads(output)["record_id"]
    assert invoke(["read", str(project), "evidence", "--entries", "--task", "T01"])[0] == 0
    assert invoke([
        "task", str(project), "finish", "T01", "--evidence", record_id, "--expected-revision", "2",
    ])[0] == 0
    code, output, _ = invoke([
        "close", str(project), "--expected-revision", "3", "--reflection", "# Reflection\n\nDone.",
        "--expected-reflection-sha256", "missing",
    ])
    assert code == 0 and json.loads(output)["status"] == "DONE"


def test_cli_validation_errors_are_actionable(project: Path) -> None:
    assert invoke(["update", str(project), "-", "--expected-revision", "0"], stdin="{")[0] == 1
    assert invoke(["update", str(project), "-", "--expected-revision", "0"], stdin="[]")[0] == 1
    assert invoke(["context", str(project), "--validate", "--task", "T01"])[0] == 1
    snapshot = document_snapshot(project, "spec")
    assert invoke([
        "edit", str(project), "spec", "--sections-json", "-", "--section", "x",
        "--expected-sha256", snapshot["document_sha256"],
    ], stdin="{}")[0] == 1
    assert invoke(["edit", str(project), "spec", "--expected-sha256", snapshot["document_sha256"]])[0] == 1
    assert invoke([
        "edit", str(project), "reflection", "--section", "x", "--body", "x",
        "--expected-sha256", "missing",
    ])[0] == 1
    assert invoke(["append", str(project), "finding", "--body", "x"])[0] == 1
    assert invoke(["append", str(project), "decision", "--task", "T01", "--body", "x"])[0] == 1


def test_cli_document_body_files_and_remaining_read_guards(project: Path, tmp_path: Path) -> None:
    assert invoke(["read", str(project), "reflection", "--section", "x"])[0] == 1
    snapshot = document_snapshot(project, "spec")
    assert invoke([
        "edit", str(project), "spec", "--sections-json", "-", "--expected-sha256",
        snapshot["document_sha256"],
    ], stdin=json.dumps({"Objective and audience": 1}))[0] == 1
    assert invoke([
        "edit", str(project), "reflection", "--expected-sha256", "missing",
    ])[0] == 1
    body_file = tmp_path / "body.md"
    body_file.write_text("Updated objective.", encoding="utf-8")
    code, output, _ = invoke([
        "edit", str(project), "spec", "--section", "Current specification", "--body-file", str(body_file),
        "--expected-sha256", snapshot["document_sha256"],
    ])
    assert code == 0 and json.loads(output)["document"] == "spec"
    reflection_file = tmp_path / "reflection.md"
    reflection_file.write_text("# Reflection\n\nChecked.", encoding="utf-8")
    close_result = {"validation": {"valid": True}, "status": "DONE"}
    with patch.object(manage_workspace, "close_project", return_value=close_result) as mocked_close:
        code, output, _ = invoke([
            "close", str(project), "--expected-revision", "0", "--reflection-file", str(reflection_file),
            "--expected-reflection-sha256", "missing",
        ])
    assert code == 0 and json.loads(output)["status"] == "DONE"
    assert mocked_close.call_args.kwargs["reflection"] == "# Reflection\n\nChecked."


def test_document_and_evidence_error_contracts(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="safe task"):
        document_snapshot(project, "notes", task_id="../bad")
    with pytest.raises(WorkspaceError, match="unknown task"):
        document_snapshot(project, "notes", task_id="T99")
    with pytest.raises(WorkspaceError, match="document must"):
        document_snapshot(project, "other")
    with pytest.raises(WorkspaceError, match="offset"):
        document_snapshot(project, "spec", offset=-1)
    assert document_snapshot(project, "reflection")["exists"] is False
    assert document_snapshot(project, "spec", outline=True)["headings"]
    with pytest.raises(WorkspaceError, match="beyond"):
        document_snapshot(project, "spec", offset=100000)
    with pytest.raises(WorkspaceError, match="exactly one"):
        edit_document(
            project, "spec", expected_sha256=document_snapshot(project, "spec")["document_sha256"],
            sections={"Missing": "body"},
        )
    with pytest.raises(WorkspaceError, match="must not be empty"):
        edit_document(
            project, "spec", expected_sha256=document_snapshot(project, "spec")["document_sha256"], sections={},
        )
    with pytest.raises(WorkspaceError, match="exactly one"):
        edit_document(project, "spec", expected_sha256="x", body="x", sections={"x": "x"})
    with pytest.raises(WorkspaceError, match="sections mapping"):
        edit_document(project, "spec", expected_sha256="x", body="x")
    with pytest.raises(WorkspaceError, match="reflection edits"):
        edit_document(project, "reflection", expected_sha256="missing", sections={"x": "x"})
    with pytest.raises(WorkspaceError, match="editable document"):
        edit_document(project, "other", expected_sha256="x", body="x")
    with pytest.raises(WorkspaceError, match="must not be empty"):
        edit_document(project, "reflection", expected_sha256="missing", body="")
    with pytest.raises(WorkspaceError, match="must not be empty"):
        append_record(project, "decision", "")
    assert append_record(project, "decision", "generated id")["entry_id"].startswith("entry-")
    with pytest.raises(WorkspaceError, match="entry ID"):
        append_record(project, "decision", "x", entry_id="Bad ID")
    with pytest.raises(WorkspaceError, match="record kind"):
        append_record(project, "other", "x")
    with pytest.raises(WorkspaceError, match="choose task"):
        evidence_entries(project, task_id="T01", step="report")
    with pytest.raises(WorkspaceError, match="limit"):
        evidence_entries(project, limit=0)
    with pytest.raises(WorkspaceError, match="unknown task"):
        evidence_entries(project, task_id="T99")
    with pytest.raises(WorkspaceError, match="unknown closure"):
        evidence_entries(project, step="bad")
    with pytest.raises(WorkspaceError, match="exactly one"):
        evidence_entries(project, record_id="ev-00000000000000000000000000000000")
    with pytest.raises(WorkspaceError, match="at least one"):
        selected_evidence_references(project, "T01", [])
    update_project_data(project, {"status": "PLANNING", "tasks": [task("T01")]}, expected_revision=0)
    with pytest.raises(WorkspaceError, match="unknown task"):
        task_operation(project, "start", "T99", expected_revision=1)
    with pytest.raises(WorkspaceError, match="non-empty reason"):
        task_operation(project, "block", "T01", expected_revision=1, reason="")
    with pytest.raises(WorkspaceError, match="task action"):
        task_operation(project, "other", "T01", expected_revision=1)
    with pytest.raises(WorkspaceError, match="requires --expected"):
        close_project(project, expected_revision=0, reflection="x")


def test_document_filesystem_and_active_execution_errors(project: Path) -> None:
    spec = project / "spec.md"
    spec.write_bytes(b"\xff")
    with pytest.raises(WorkspaceError, match="UTF-8"):
        document_snapshot(project, "spec")
    spec.write_text("# x\n\n## Current specification\n\nx\n\n## Decision history\n\nx\n", encoding="utf-8")
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    state["execution"]["coordinator_run"] = "active"
    (project / "project.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="execution is active"):
        edit_document(
            project, "spec", expected_sha256=document_snapshot(project, "spec")["document_sha256"],
            sections={"Current specification": "x"},
        )
    with pytest.raises(WorkspaceError, match="execution is active"):
        append_record(project, "decision", "x")


def test_document_os_errors_are_normalized(project: Path) -> None:
    fill_spec(project)
    snapshot = document_snapshot(project, "spec")
    with patch.object(workspace_documents, "atomic_write_text", side_effect=OSError("disk full")):
        with pytest.raises(WorkspaceError, match="cannot write"):
            edit_document(
                project, "spec", expected_sha256=snapshot["document_sha256"],
                sections={"Objective and audience": "x"},
            )
        with pytest.raises(WorkspaceError, match="cannot write"):
            append_record(project, "decision", "x")


def test_overlapping_sections_bad_structure_and_symlink_escape(project: Path, tmp_path: Path) -> None:
    fill_spec(project)
    snapshot = document_snapshot(project, "spec")
    with pytest.raises(WorkspaceError, match="overlap"):
        edit_document(
            project,
            "spec",
            expected_sha256=snapshot["document_sha256"],
            sections={"Current specification": "parent", "Objective and audience": "child"},
        )
    (project / "spec.md").write_text(
        "# Current specification\n\nbody\n\n## Decision history\n\nhistory\n", encoding="utf-8"
    )
    snapshot = document_snapshot(project, "spec")
    sections = {heading: heading for heading, _ in SPEC_CANONICAL_SECTIONS}
    with pytest.raises(WorkspaceError, match="level-two"):
        edit_document(project, "spec", expected_sha256=snapshot["document_sha256"], sections=sections)
    external = tmp_path / "outside.md"
    external.write_text("outside", encoding="utf-8")
    (project / "spec.md").unlink()
    (project / "spec.md").symlink_to(external)
    with pytest.raises(WorkspaceError, match="escapes"):
        document_snapshot(project, "spec")


def test_append_retry_finds_a_nonfinal_entry(project: Path) -> None:
    plan(project)
    append_record(project, "decision", "first", entry_id="first-entry")
    append_record(project, "decision", "second", entry_id="second-entry")
    assert append_record(project, "decision", "first", entry_id="first-entry")["existing"] is True
    with pytest.raises(WorkspaceConflict, match="different content"):
        append_record(project, "decision", "fir", entry_id="first-entry")


def test_evidence_metadata_ambiguity_duplicates_and_pagination(project: Path) -> None:
    plan(project)
    first = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    second = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    page = evidence_entries(project, task_id="T01", limit=1)
    assert page["next_offset"] == 1
    assert evidence_entries(project, task_id="T01", offset=1, limit=1)["entries"][0]["record_id"] == second.record_id
    with pytest.raises(WorkspaceError, match="duplicate"):
        selected_evidence_references(project, "T01", [first.record_id, first.record_id])
    path = project / "evidence.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(first.reference["anchor"], "evidence-00000000000000000000000000000000", 1))
    with pytest.raises(WorkspaceError, match="malformed"):
        selected_evidence_references(project, "T01", [first.record_id])
    duplicate = path.read_text(encoding="utf-8")
    path.write_text(duplicate + duplicate[duplicate.index("## T01"):], encoding="utf-8")
    with pytest.raises(WorkspaceError, match="exactly one"):
        selected_evidence_references(project, "T01", [second.record_id])


def test_evidence_label_and_start_next_guards(project: Path) -> None:
    plan(project)
    task_operation(project, "start", "T01", expected_revision=1)
    result = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    path = project / "evidence.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Exit code: 0 (passed)", "Exit code: 0 (FAILED)"))
    with pytest.raises(WorkspaceError, match="malformed"):
        selected_evidence_references(project, "T01", [result.record_id])
    path.write_text(path.read_text(encoding="utf-8").replace("Exit code: 0 (FAILED)", "Exit code: 0 (passed)"))
    with pytest.raises(WorkspaceError, match="different task"):
        task_operation(
            project,
            "finish",
            "T01",
            expected_revision=2,
            evidence_record_ids=[result.record_id],
            start_next="T01",
        )


def test_evidence_change_guard_and_context_revision_conflict(project: Path) -> None:
    plan(project)
    task_operation(project, "start", "T01", expected_revision=1)
    result = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    first = [{"root": "workspace", "path": "evidence.md", "anchor": result.reference["anchor"]}]
    second = [{"root": "workspace", "path": "evidence.md", "anchor": "changed"}]
    with patch.object(workspace_operations, "selected_evidence_references", side_effect=[first, second]):
        with pytest.raises(WorkspaceError, match="changed before"):
            task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[result.record_id])
    with patch.object(workspace_session, "project_context", return_value={"revision": -1}):
        with pytest.raises(WorkspaceError, match="changed while"):
            validated_project_context(project)


def test_remaining_mapping_and_close_branches(project: Path) -> None:
    bad_patch: Any = []
    with pytest.raises(WorkspaceError, match="JSON object"):
        update_project_data(project, bad_patch, expected_revision=0)
    with pytest.raises(WorkspaceError):
        close_project(project, expected_revision=0)


def test_close_reports_saved_reflection_when_state_commit_fails(project: Path) -> None:
    plan(project)
    with pytest.raises(WorkspaceConflict, match="revision conflict"):
        close_project(
            project,
            expected_revision=0,
            reflection="# Stale reflection",
            expected_reflection_sha256="missing",
        )
    assert not (project / "reflection.md").exists()

    with patch.object(workspace_operations, "commit_state", side_effect=WorkspaceConflict("revision race")):
        with pytest.raises(CloseOperationError) as caught:
            close_project(
                project,
                expected_revision=1,
                reflection="# Saved reflection",
                expected_reflection_sha256="missing",
            )
    assert caught.value.result["reflection_saved"] is True
    assert caught.value.result["committed"] is False
    assert caught.value.result["state"] == {"revision": 1, "status": "PLANNING"}
    assert (project / "reflection.md").read_text(encoding="utf-8") == "# Saved reflection\n"

    with patch.object(manage_workspace, "close_project", side_effect=caught.value):
        code, output, error = invoke(["close", str(project), "--expected-revision", "1"])
    assert code == 1
    assert json.loads(output)["reflection_saved"] is True
    assert "state was not committed" in error


def test_close_reports_a_commit_that_landed_before_index_failure(project: Path) -> None:
    plan(project)
    task_operation(project, "start", "T01", expected_revision=1)
    first = record_evidence_result(project, "T01", [sys.executable, "-c", "pass"])
    task_operation(
        project,
        "finish",
        "T01",
        expected_revision=2,
        evidence_record_ids=[first.record_id],
        start_next="T02",
    )
    second = record_evidence_result(project, "T02", [sys.executable, "-c", "pass"])
    task_operation(project, "finish", "T02", expected_revision=3, evidence_record_ids=[second.record_id])

    with patch.object(workspace_lib, "_rebuild_index_after_commit", side_effect=WorkspaceError("index unavailable")):
        with pytest.raises(CloseOperationError) as caught:
            close_project(
                project,
                expected_revision=4,
                reflection="# Reflection\n\nVerified.",
                expected_reflection_sha256="missing",
            )
    assert caught.value.result["committed"] is True
    assert caught.value.result["revision"] == 5
    assert caught.value.result["recovery"].startswith("run rebuild-index")
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert state["status"] == "DONE"


def test_automated_cli_lifecycle_interaction_budget(project: Path) -> None:
    fill_spec(project)
    tasks = [task("T01"), task("T02", depends_on=["T01"]), task("T03", depends_on=["T02"])]
    observations: list[str] = []

    def call(arguments: list[str], *, stdin: str = "") -> dict[str, Any]:
        code, output, error = invoke(arguments, stdin=stdin)
        assert code == 0, error
        observations.append(output)
        return json.loads(output)

    call(["context", str(project), "--validate"])
    call(
        ["update", str(project), "-", "--expected-revision", "0", "--json"],
        stdin=json.dumps({"status": "PLANNING", "tasks": tasks}),
    )
    call(["task", str(project), "start", "T01", "--expected-revision", "1"])
    revision = 2
    for index, task_id in enumerate(("T01", "T02", "T03")):
        evidence = call([
            "record-evidence", str(project), "--task", task_id, "--json", "--",
            sys.executable, "-c", "pass",
        ])
        finish = [
            "task", str(project), "finish", task_id, "--evidence", evidence["record_id"],
            "--expected-revision", str(revision),
        ]
        if index < 2:
            finish.extend(["--start-next", f"T0{index + 2}"])
        call(finish)
        revision += 1
    call([
        "close", str(project), "--expected-revision", "5", "--reflection", "# Reflection\n\nVerified.",
        "--expected-reflection-sha256", "missing",
    ])
    metrics = {
        "observations": len(observations),
        "output_bytes": sum(len(item.encode()) for item in observations),
        "result_bytes": [len(item.encode()) for item in observations],
    }
    assert metrics["observations"] == 10
    assert metrics["output_bytes"] < 15000
    print(json.dumps(metrics, sort_keys=True))
