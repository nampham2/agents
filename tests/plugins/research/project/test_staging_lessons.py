"""Staging a lesson is one command, and promoting one twice adds it once.

Most projects closed with `memory-staging.md` untouched and rebuilt their lessons from memory at the
end, because the only way to stage one was to hand-edit a file. A promotion whose output was lost was
retried and appended its incident a second time. These tests pin the replacements.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_documents
import workspace_lib as lib
import workspace_operations
from workspace_lib import MEMORY_STAGING_PLACEHOLDER, WorkspaceConflict, WorkspaceError
from workspace_operations import task_operation
from workspace_workflows import workflow

from tests.plugins.research.project.test_workflow_automation import invoke, plan


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Staging", working_directory=target, create_root=True)


def stage_lesson(*arguments: Any, **keywords: Any) -> dict[str, Any]:
    """Resolved when called, so a build without the command fails each test instead of the import."""
    return workspace_documents.stage_lesson(*arguments, **keywords)


def staging(project: Path) -> str:
    return (project / "memory-staging.md").read_text(encoding="utf-8")


class TestStageLesson:
    def test_the_first_lesson_replaces_the_placeholder_and_becomes_a_section(self, project: Path) -> None:
        assert MEMORY_STAGING_PLACEHOLDER in staging(project)
        result = stage_lesson(project, "Sync before reading", "Conditions: x. Lesson: y. Evidence: T01.")
        text = staging(project)
        assert result["existing"] is False and result["title"] == "Sync before reading"
        assert MEMORY_STAGING_PLACEHOLDER not in text
        assert "\n## Sync before reading\n\nConditions: x. Lesson: y. Evidence: T01.\n" in text
        assert text.startswith("# Staged lessons"), "the scaffold's own heading survives"
        assert lib.memory_staging_warnings(project), "a staged lesson is reported at close"

    def test_several_lessons_accumulate_in_order(self, project: Path) -> None:
        stage_lesson(project, "First", "one")
        stage_lesson(project, "Second", "two")
        text = staging(project)
        assert text.index("## First") < text.index("## Second")
        assert text.count("## ") == 2

    def test_a_missing_file_is_created_from_the_scaffold(self, project: Path) -> None:
        (project / "memory-staging.md").unlink()
        stage_lesson(project, "Only", "body")
        assert staging(project).startswith("# Staged lessons") and "## Only" in staging(project)

    def test_the_same_lesson_twice_is_a_no_op_and_a_changed_one_conflicts(self, project: Path) -> None:
        first = stage_lesson(project, "Retry safe", "same text")
        before = staging(project)
        again = stage_lesson(project, "Retry safe", "  same text\n")
        assert again["existing"] is True and again["document_sha256"] == first["document_sha256"]
        assert staging(project) == before
        with pytest.raises(WorkspaceConflict, match="different text"):
            stage_lesson(project, "Retry safe", "other text")
        assert staging(project) == before

    @pytest.mark.parametrize(
        ("title", "body", "message"),
        [
            ("", "b", "title must be one"),
            ("two\nlines", "b", "title must be one"),
            ("# heading", "b", "title must be one"),
            ("x" * 121, "b", "title must be one"),
            ("ok", "   ", "non-empty body"),
            ("ok", "text\n\n### Deeper\n\nmore", "may not contain headings"),
            ("ok", "# Top", "may not contain headings"),
            ("Staged lessons", "b", "collides with another heading"),
        ],
    )
    def test_a_title_or_body_triage_could_not_select_is_refused(
        self, project: Path, title: str, body: str, message: str
    ) -> None:
        before = staging(project)
        with pytest.raises(WorkspaceError, match=message):
            stage_lesson(project, title, body)
        assert staging(project) == before

    def test_a_fenced_hash_line_is_not_a_heading(self, project: Path) -> None:
        stage_lesson(project, "Code sample", "```\n# just a comment\n```")
        assert "# just a comment" in staging(project)

    def test_a_duplicate_heading_in_the_file_is_reported_as_a_collision(self, project: Path) -> None:
        stage_lesson(project, "Twice", "a")
        (project / "memory-staging.md").write_text(staging(project) + "\n## Twice\n\nb\n", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="collides"):
            stage_lesson(project, "Twice", "a")

    def test_an_unwritable_file_is_a_workspace_error(self, project: Path) -> None:
        with patch.object(lib.os, "replace", side_effect=OSError("disk full")):
            with pytest.raises(WorkspaceError, match="cannot write"):
                stage_lesson(project, "Cannot", "write")


class TestTriageDrain:
    def test_staged_lessons_triage_and_leave_the_file_reading_as_empty(self, project: Path) -> None:
        from tests.plugins.research.project.test_lifecycle_commands import done, request

        done(project)
        stage_lesson(project, "Reuse", "A general lesson.")
        stage_lesson(project, "Drop", "Duplicate of another.")
        payload = request(
            project,
            "drain",
            items=[
                {
                    "section": "Reuse",
                    "disposition": "promote",
                    "reason": "Reusable",
                    "promotion": {
                        "topic": "staged-topic", "body": "A general lesson.", "description": "Staged",
                        "scope": "any", "kind": "method", "expected_sha256": "missing",
                    },
                },
                {"section": "Drop", "disposition": "discard", "reason": "Duplicate"},
            ],
        )
        payload["tokens"] = {k: v for k, v in payload["tokens"].items() if k in ("memory-staging", "reflection")}
        workflow(project, "triage", payload)
        assert "Resolved lesson records" in staging(project), "triage leaves its own record behind"
        assert lib.staged_lesson_lines(project) == []
        assert lib.memory_staging_warnings(project) == [], "a drained file must not warn at close"

    def test_a_triage_record_alone_is_not_a_lesson_but_a_real_line_beside_it_is(self, project: Path) -> None:
        (project / "memory-staging.md").write_text(
            lib.MEMORY_STAGING_SKELETON.replace(MEMORY_STAGING_PLACEHOLDER, "")
            + "<!-- Resolved lesson records: .operations/x.json -->\n",
            encoding="utf-8",
        )
        assert lib.staged_lesson_lines(project) == []
        (project / "memory-staging.md").write_text(staging(project) + "\n## Still open\n\nbody\n", encoding="utf-8")
        assert lib.staged_lesson_lines(project) == ["body"]

    def test_no_file_and_an_unreadable_file_hold_no_lessons(self, project: Path) -> None:
        (project / "memory-staging.md").unlink()
        assert lib.staged_lesson_lines(project) == []
        (project / "memory-staging.md").write_bytes(b"\xff\xfe\x00bad")
        with patch.object(lib, "read_text", side_effect=WorkspaceError("unreadable")):
            assert lib.staged_lesson_lines(project) == []


class TestStageCli:
    def test_stage_through_the_cli_with_a_body_and_with_stdin(self, project: Path) -> None:
        code, stdout, _ = invoke(["stage", str(project), "--title", "From body", "--body", "inline lesson"])
        assert code == 0 and json.loads(stdout)["existing"] is False
        code, stdout, _ = invoke(
            ["stage", str(project), "--title", "From stdin", "--body-file", "-"], stdin="piped lesson\n"
        )
        assert code == 0
        assert "inline lesson" in staging(project) and "piped lesson" in staging(project)
        code, stdout, _ = invoke(["stage", str(project), "--title", "From body", "--body", "inline lesson"])
        assert code == 0 and json.loads(stdout)["existing"] is True

    def test_a_refused_stage_exits_one_with_a_message_and_writes_nothing(self, project: Path) -> None:
        before = staging(project)
        code, _, stderr = invoke(["stage", str(project), "--title", "Bad", "--body", "## heading"])
        assert code == 1 and "may not contain headings" in stderr
        assert staging(project) == before


class TestIdempotentPromotion:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        root = tmp_path / "ws"
        (root / "memory").mkdir(parents=True)
        return root

    def promote(
        self, workspace: Path, *extra: str, body: str = "A lesson about retries."
    ) -> tuple[int, dict[str, Any], str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = [
            "promote-memory", "retry-topic", "--body", body, "--description", "Retries", "--kind", "method",
            "--scope", "any retry", "--create", "--workspace-root", str(workspace), *extra,
        ]
        with (
            patch.object(sys, "argv", ["research-project", *arguments]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = manage_workspace.main()
        return code, json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}, stderr.getvalue()

    def topic(self, workspace: Path) -> str:
        return (workspace / "memory" / "retry-topic.md").read_text(encoding="utf-8")

    def test_a_retry_adds_nothing_and_says_so(self, workspace: Path) -> None:
        code, first, _ = self.promote(workspace)
        assert code == 0 and first["duplicate"] is False and first["entry_id"].startswith("promote-")
        once = self.topic(workspace)
        code, second, stderr = self.promote(workspace)
        assert code == 0 and second["duplicate"] is True and second["entry_id"] == first["entry_id"]
        assert "already recorded" in stderr
        assert self.topic(workspace) == once
        assert once.count("A lesson about retries.") == 1

    def test_a_different_body_is_a_new_entry_not_a_conflict(self, workspace: Path) -> None:
        self.promote(workspace)
        code, second, _ = self.promote(workspace, body="A second, different lesson.")
        assert code == 0 and second["duplicate"] is False
        text = self.topic(workspace)
        assert "A lesson about retries." in text and "A second, different lesson." in text

    def test_an_explicit_entry_id_is_honoured_and_a_changed_body_under_it_conflicts(self, workspace: Path) -> None:
        code, result, _ = self.promote(workspace, "--entry-id", "my-entry")
        assert code == 0 and result["entry_id"] == "my-entry"
        code, _, stderr = self.promote(workspace, "--entry-id", "my-entry", body="changed")
        assert code == 1 and "different content" in stderr
        code, _, stderr = self.promote(workspace, "--entry-id", "Bad ID")
        assert code == 1 and "invalid memory entry ID" in stderr

    def test_a_retry_after_the_index_rebuild_failed_does_not_duplicate_the_incident(self, workspace: Path) -> None:
        with patch.object(manage_workspace, "rebuild_index", side_effect=WorkspaceError("index failed")):
            code, _, stderr = self.promote(workspace)
        assert code == 1 and "index failed" in stderr
        assert self.topic(workspace).count("A lesson about retries.") == 1, "the amendment landed before the failure"
        code, result, _ = self.promote(workspace)
        assert code == 0 and result["duplicate"] is True
        assert self.topic(workspace).count("A lesson about retries.") == 1

    def test_the_default_id_is_a_function_of_the_body_alone(self) -> None:
        assert lib.promotion_entry_id("a") == lib.promotion_entry_id("a") != lib.promotion_entry_id("b")
        assert lib.memory_entry_marker("id", "a") != lib.memory_entry_marker("id", "b")


class TestLessonHint:
    def finish_after(self, project: Path, *attempts: int) -> dict[str, Any]:
        plan(project)
        task_operation(project, "start", "T01", expected_revision=1)
        record = ""
        for code in attempts:
            record = lib.record_evidence_result(project, "T01", [sys.executable, "-c", f"raise SystemExit({code})"])
            record = record.record_id
        return task_operation(project, "finish", "T01", expected_revision=2, evidence_record_ids=[record])

    def test_a_task_that_failed_before_it_passed_is_pointed_at_stage(self, project: Path) -> None:
        result = self.finish_after(project, 3, 0)
        assert "research-project stage" in result["lesson_hint"] and "T01" in result["lesson_hint"]

    def test_a_clean_task_gets_no_hint(self, project: Path) -> None:
        assert "lesson_hint" not in self.finish_after(project, 0)

    def test_nothing_is_said_when_a_lesson_is_already_staged(self, project: Path) -> None:
        stage_lesson(project, "Already", "staged")
        assert "lesson_hint" not in self.finish_after(project, 3, 0)

    def test_every_page_of_evidence_is_searched(self, project: Path) -> None:
        pages = [
            {"entries": [{"passed": True}] * 100, "next_offset": 100},
            {"entries": [{"passed": False}], "next_offset": None},
        ]
        with patch.object(workspace_operations, "evidence_entries", side_effect=pages) as listing:
            assert workspace_operations._had_failed_attempt(project, "T01") is True
        assert [call.kwargs["offset"] for call in listing.call_args_list] == [0, 100]
        with patch.object(
            workspace_operations, "evidence_entries", return_value={"entries": [{"passed": True}], "next_offset": None}
        ):
            assert workspace_operations._had_failed_attempt(project, "T01") is False
