"""Finalize stages the reflection's lesson sections so triage has to decide on them.

Candidate `c3` of retro `2026-10-16-001`. The reflection carries what did not work, and nothing read
it again: staging and its triage gate existed, but only a hand edit could put anything there. Closing
now appends the reflection's lesson sections in one commit, and the existing warning then forces a
promote, reflection or discard decision at triage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import workspace_documents
import workspace_lib as lib
from workspace_lib import MEMORY_STAGING_PLACEHOLDER, WorkspaceError
from workspace_session import _load_state, validated_project_context
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import done, request

FAILURE_BODY = "The first attempt read the staging file after the commit guard had already refused it."
WORKED_BODY = "Measuring the selection rule against every closed reflection before writing any code."
LESSON_BODY = "Fetch the document token in the same read that fetches the reflection token."
LONG_TITLE = ("What did not work: " + "measured twice and still too long " * 4).rstrip()

LESSON_REFLECTION = (
    "# Reflection\n\n"
    "## Outcome\n\n"
    "Every success criterion was met and the evidence records each check passing.\n\n"
    "## What did not work\n\n"
    f"{FAILURE_BODY}\n\n"
    "## What worked\n\n"
    f"{WORKED_BODY}\n\n"
    "## Lessons\n\n"
    f"{LESSON_BODY}\n\n"
    "## Retro\n\n"
    "Nothing blocked the work, no reference was loaded without need, and no plugin change is needed.\n"
)

PLAIN_REFLECTION = (
    "# Reflection\n\n"
    "## Outcome\n\n"
    "Every success criterion was met and no work is left open for a later session.\n\n"
    "## Retro\n\n"
    "Nothing blocked the work, no reference was loaded without need, and no plugin change is needed.\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(
        tmp_path / "workspace", title="Finalize staging", working_directory=target, create_root=True
    )
    done(created)
    return created


def staging(project: Path) -> str:
    return (project / "memory-staging.md").read_text(encoding="utf-8")


def finalize(project: Path, reflection: str, identity: str = "close", **fields: Any) -> dict[str, Any]:
    payload = request(project, identity, reflection=reflection, continuation={"next": "none"}, **fields)
    return workflow(project, "finalize", payload)


class TestReflectedLessons:
    def test_matched_sections_are_staged_verbatim_and_reported(self, project: Path) -> None:
        result = finalize(project, LESSON_REFLECTION)
        assert result["reflected_lessons"]["staged"] == ["What did not work", "Lessons"]
        assert result["reflected_lessons"]["skipped"] == []
        text = staging(project)
        assert MEMORY_STAGING_PLACEHOLDER not in text
        assert f"## What did not work\n\n{FAILURE_BODY}\n" in text
        assert f"## Lessons\n\n{LESSON_BODY}\n" in text
        assert "## Outcome" not in text, "an unmatched heading is not a staged lesson"
        assert "## What worked" not in text, "an unmatched heading is not a staged lesson"
        assert result["memory_staging_warnings"], "the close names the entries it just staged"
        assert result["memory_staging_warnings"] == lib.memory_staging_warnings(project)

    def test_a_reflection_without_lesson_headings_leaves_the_file_byte_identical(self, project: Path) -> None:
        before = staging(project)
        result = finalize(project, PLAIN_REFLECTION)
        assert result["reflected_lessons"] == {"staged": [], "skipped": []}
        assert staging(project) == before
        assert result["memory_staging_warnings"] == []
        assert "memory-staging" not in result["tokens"], "an untouched document is not rewritten"

    def test_a_title_already_staged_differently_keeps_the_file_and_reports_a_conflict(self, project: Path) -> None:
        workspace_documents.stage_lesson(project, "Lessons", "A line staged mid project.")
        result = finalize(project, LESSON_REFLECTION)
        assert result["reflected_lessons"]["staged"] == ["What did not work"]
        assert result["reflected_lessons"]["skipped"] == [
            {"title": "Lessons", "reason": "title conflict"},
        ]
        text = staging(project)
        assert "A line staged mid project." in text, "the existing text is kept"
        assert LESSON_BODY not in text, "the reflection's body does not overwrite it"

    def test_a_title_already_staged_with_the_same_body_is_not_written_twice(self, project: Path) -> None:
        workspace_documents.stage_lesson(project, "Lessons", LESSON_BODY)
        result = finalize(project, LESSON_REFLECTION)
        assert result["reflected_lessons"]["staged"] == ["What did not work"]
        assert result["reflected_lessons"]["skipped"] == [
            {"title": "Lessons", "reason": "already staged"},
        ]
        assert staging(project).count(LESSON_BODY) == 1

    def test_a_matched_section_whose_body_carries_a_heading_is_skipped(self, project: Path) -> None:
        before = staging(project)
        reflection = LESSON_REFLECTION.replace(
            LESSON_BODY, "Fetch the document token.\n\n### Where it is published\n\nIn context."
        )
        result = finalize(project, reflection)
        assert result["reflected_lessons"]["staged"] == ["What did not work"]
        skipped = result["reflected_lessons"]["skipped"]
        assert [entry["title"] for entry in skipped] == ["Lessons"]
        assert "may not contain headings" in skipped[0]["reason"]
        assert "### Where it is published" not in staging(project)
        assert staging(project) != before, "the matched section before it is still staged"

    def test_a_matched_title_over_the_staging_limit_is_skipped(self, project: Path) -> None:
        reflection = LESSON_REFLECTION.replace("## Retro\n", f"## {LONG_TITLE}\n\nToo long.\n\n## Retro\n")
        assert len(LONG_TITLE) > 120
        result = finalize(project, reflection)
        assert result["reflected_lessons"]["staged"] == ["What did not work", "Lessons"]
        skipped = result["reflected_lessons"]["skipped"]
        assert [entry["title"] for entry in skipped] == [LONG_TITLE]
        assert "title must be one" in skipped[0]["reason"]
        assert LONG_TITLE not in staging(project)

    def test_a_matched_section_with_an_empty_body_is_skipped(self, project: Path) -> None:
        reflection = LESSON_REFLECTION.replace(LESSON_BODY, "")
        result = finalize(project, reflection)
        assert result["reflected_lessons"]["staged"] == ["What did not work"]
        skipped = result["reflected_lessons"]["skipped"]
        assert [entry["title"] for entry in skipped] == ["Lessons"]
        assert "non-empty body" in skipped[0]["reason"]

    def test_a_staging_file_removed_by_hand_comes_back_from_the_skeleton(self, project: Path) -> None:
        (project / "memory-staging.md").unlink()
        result = finalize(project, LESSON_REFLECTION)
        assert result["reflected_lessons"]["staged"] == ["What did not work", "Lessons"]
        text = staging(project)
        assert text.startswith("# Staged lessons"), "the scaffold is rebuilt"
        assert MEMORY_STAGING_PLACEHOLDER not in text

    def test_replaying_the_close_does_not_append_the_sections_twice(self, project: Path) -> None:
        payload = request(project, "close", reflection=LESSON_REFLECTION, continuation={"next": "none"})
        first = workflow(project, "finalize", payload)
        text = staging(project)
        second = workflow(project, "finalize", payload)
        assert second["reflected_lessons"] == first["reflected_lessons"]
        assert staging(project) == text, "the journal replays the stored result, not the append"
        assert text.count("## Lessons") == 1

    def test_a_stale_staging_token_refuses_the_close_before_any_write(self, project: Path) -> None:
        payload = request(project, "close", reflection=LESSON_REFLECTION, continuation={"next": "none"})
        workspace_documents.stage_lesson(project, "Mid project", "Something surprising.")
        with pytest.raises(WorkspaceError) as refused:
            workflow(project, "finalize", payload)
        message = str(refused.value)
        assert "document conflict: memory-staging" in message
        assert f"expected token {payload['tokens']['memory-staging']}" in message
        assert f"research-project context {project}" in message
        assert not (project / "reflection.md").exists()
        assert _load_state(project)["status"] != "DONE"

    def test_context_publishes_the_staging_token_the_close_requires(self, project: Path) -> None:
        context = validated_project_context(project)
        assert context["documents"]["memory-staging"] == {
            "path": str(project / "memory-staging.md"),
            "exists": True,
            "sha256": lib.document_sha256(staging(project)),
        }
