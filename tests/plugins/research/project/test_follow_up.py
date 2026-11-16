"""Follow-up projects: the reflection's `## Follow-up` section, carried-over findings, `finalize`'s
`follow_up` list and the `follow-up` command that creates the next project and does nothing more."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_lib as lib
from workspace_documents import (
    follow_up_items,
    is_follow_up_title,
    near_follow_up_titles,
    reflection_lesson_sections,
)
from workspace_goldfish import Round, _finding_line, render_marker, residual_findings
from workspace_lib import WorkspaceError, allocate_project, atomic_write_json, load_json, validate_v4_state
from workspace_operations import follow_up_project, require_new_tags
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import done, request

RETRO = (
    "## Retro\n\nNothing blocked the work, no reference was loaded without need, and no plugin change is needed.\n"
)


def reflection(*sections: str) -> str:
    body = "".join(f"{section.rstrip()}\n\n" for section in sections)
    outcome = "## Outcome\n\nEvery success criterion was met and the evidence records each check.\n\n"
    return f"# Reflection\n\n{outcome}{body}{RETRO}"


FOLLOW_UP = "## Follow-up\n\n- Backfill the 177 old projects with tags.\n- Check the dashboard on a phone."


# --- parsing the reflection ------------------------------------------------------------------------


def test_each_top_level_bullet_is_one_item_and_wrapped_and_nested_lines_join_it() -> None:
    text = reflection(
        "## Follow-up\n\n- first item\n- second item wraps\n  onto a second line\n  - and a nested point\n"
        "* star bullet\n1. numbered item\n10. tenth item"
    )
    items, loose = follow_up_items(text)
    assert items == [
        "first item",
        "second item wraps onto a second line and a nested point",
        "star bullet",
        "numbered item",
        "tenth item",
    ]
    assert loose is False


@pytest.mark.parametrize("title", ["Follow-up", "Follow-ups", "FOLLOW-UP", "follow-ups"])
def test_the_section_title_matches_ignoring_case_and_an_s(title: str) -> None:
    assert follow_up_items(reflection(f"## {title}\n\n- one"))[0] == ["one"]
    assert is_follow_up_title(title)


@pytest.mark.parametrize("title", ["Follow-up after the failure", "Followup", "Open work", "Follow up"])
def test_other_titles_are_not_the_follow_up_section(title: str) -> None:
    assert follow_up_items(reflection(f"## {title}\n\n- one")) == ([], False)
    assert not is_follow_up_title(title)


def test_a_missing_or_empty_section_has_no_items_and_text_without_bullets_is_flagged() -> None:
    assert follow_up_items(reflection()) == ([], False)
    assert follow_up_items(reflection("## Follow-up\n\n")) == ([], False)
    assert follow_up_items(reflection("## Follow-up\n\nSomething is left but nobody wrote a bullet.")) == ([], True)


def test_a_level_three_heading_is_not_the_section_and_the_first_section_wins() -> None:
    assert follow_up_items(reflection("### Follow-up\n\n- nested heading")) == ([], False)
    two = reflection("## Follow-up\n\n- first section", "## Follow-ups\n\n- second section")
    assert follow_up_items(two)[0] == ["first section"]


def test_bullets_inside_a_fenced_block_are_not_items() -> None:
    quoted = "```\n- quoted bullet\n## not a heading\n```"
    text = reflection(f"## Follow-up\n\n- real item\n\n{quoted}\n\n- another real item")
    assert follow_up_items(text)[0] == ["real item", "another real item"]


def test_an_empty_bullet_is_not_an_item() -> None:
    assert follow_up_items(reflection("## Follow-up\n\n- \n-\n* \n- real\n- "))[0] == ["real"]
    assert follow_up_items(reflection("## Follow-up\n\n- \n*   \n")) == ([], False)


def test_a_longer_fence_is_not_closed_by_a_shorter_one() -> None:
    quoted = "````\n- inside\n```\n- still inside\n````"
    text = reflection(f"## Follow-up\n\n- before\n\n{quoted}\n\n- after")
    assert follow_up_items(text)[0] == ["before", "after"]
    tilde = reflection("## Follow-up\n\n- a\n\n~~~\n- hidden\n~~~\n\n- b")
    assert follow_up_items(tilde)[0] == ["a", "b"]


def test_a_fence_that_is_never_closed_is_flagged_not_silently_swallowed() -> None:
    items, flagged = follow_up_items(reflection("## Follow-up\n\n- kept\n\n```\n- swallowed by the open fence"))
    assert items == ["kept"] and flagged is True


def test_a_deeply_indented_fence_line_is_text_not_a_fence() -> None:
    items, flagged = follow_up_items(reflection("## Follow-up\n\n- a\n\n    ```\n- b"))
    assert items[-1] == "b" and flagged is False


def test_a_title_that_only_looks_like_the_section_is_named_so_it_can_be_warned_about() -> None:
    near = reflection(
        "## Follow-up work\n\n- lost?", "## Follow-up after the failure\n\n- lost too?",
        "## Follow up\n\n- lost three?", "## Followup:\n\n- lost four?", "## Follow-upstream\n\n- not a near miss",
    )
    assert near_follow_up_titles(near) == ["Follow-up work", "Follow-up after the failure", "Follow up", "Followup:"]
    assert near_follow_up_titles(reflection("## Follow-up\n\n- a", "## Follow-ups\n\n- b")) == []
    assert near_follow_up_titles(reflection("### Follow-up work\n\n- level three")) == []


def test_the_follow_up_section_is_never_staged_as_a_lesson() -> None:
    text = reflection(
        "## Follow-up after the failure\n\n- the title carries a lesson word",
        "## What did not work\n\nThe first attempt failed.",
    )
    titles = [title for title, _ in reflection_lesson_sections(text)]
    assert titles == ["What did not work"]


# --- carried-over findings -------------------------------------------------------------------------


def finding(identifier: str, severity: str, triage: str, scope: str = "design", summary: str = "A problem.") -> str:
    return _finding_line({
        "id": identifier, "reviewer": "combined", "severity": severity, "scope": scope, "summary": summary,
        "triage": triage,
    })


def write_round(
    project: Path, number: int, outcome: str, lines: list[str], target: str = "design", worktree: str | None = None
) -> None:
    round_ = Round(number, target, worktree, False, "a" * 64, "b" * 64, outcome, ("combined",), None, None)
    marker = render_marker(round_)
    text = "\n".join([
        marker, "", f"# Goldfish round {number:02d}: {target}", "", f"Outcome: {outcome}", "", "## Reports", "",
        "- **combined**: reviewed.", "", "## Findings", "", *lines, "",
    ])
    (project / "reviews").mkdir(exist_ok=True)
    (project / f"reviews/goldfish_{number:02d}.md").write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return allocate_project(
        tmp_path / "workspace", title="Follow-up source", working_directory=target, create_root=True, tags=["alpha"]
    )


def test_a_blocker_or_major_that_was_not_fixed_in_an_accepted_residual_round_is_carried_over(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [
        finding("a1", "Blocker", "ESCALATE", summary="The user accepted this risk."),
        finding("a2", "Major", "CONTEST", summary="Contested with evidence."),
        finding("a3", "Major", "FIX", summary="Fixed already."),
        finding("a4", "Nit", "DECLINE", summary="A nit."),
        finding("a5", "Nit", "FIX", summary="A fixed nit."),
    ])
    items = residual_findings(project)
    assert [item["pointer"] for item in items] == ["reviews/goldfish_01.md#a1", "reviews/goldfish_01.md#a2"]
    assert all(item["kind"] == "residual" for item in items)
    assert "The user accepted this risk." in items[0]["text"] and "a1" in items[0]["text"]


def test_a_re_audit_round_with_only_fixed_majors_closes_accepted_residual_and_yields_nothing(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [finding(f"m{n}", "Major", "FIX") for n in range(3)])
    assert residual_findings(project) == []


@pytest.mark.parametrize("outcome", ["accepted", "reaudit-required", "open", "waived"])
def test_only_the_accepted_residual_outcome_carries_findings(project: Path, outcome: str) -> None:
    write_round(project, 1, outcome, [finding("a1", "Major", "ESCALATE")])
    assert residual_findings(project) == []


def test_the_latest_accepted_residual_round_of_each_series_counts_even_after_a_later_pass(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [finding("old", "Major", "CONTEST")])
    write_round(project, 2, "accepted-residual", [finding("new", "Major", "ESCALATE")])
    write_round(project, 3, "accepted", [])
    write_round(project, 4, "accepted-residual", [finding("spec", "Blocker", "ESCALATE")], target="spec")
    pointers = sorted(item["pointer"] for item in residual_findings(project))
    assert pointers == ["reviews/goldfish_02.md#new", "reviews/goldfish_04.md#spec"]


def test_a_round_file_that_cannot_be_read_is_carried_as_an_item(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import workspace_goldfish

    write_round(project, 1, "accepted-residual", [finding("a1", "Major", "ESCALATE")])

    def unreadable(path: Path) -> str:
        raise WorkspaceError(f"cannot read {path}")

    monkeypatch.setattr(workspace_goldfish, "read_text", unreadable)
    [item] = residual_findings(project)
    assert item["pointer"] == "reviews/goldfish_01.md" and "could not be read" in item["text"]


def test_lines_in_the_findings_section_that_cannot_be_read_are_carried_as_one_item(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [
        finding("fine", "Major", "FIX"),
        "A paragraph that is not a finding line.",
        "- a bullet without the finding shape",
        "None.",
    ])
    [item] = residual_findings(project)
    assert item["kind"] == "residual" and item["pointer"] == "reviews/goldfish_01.md"
    assert "2 line" in item["text"] and "could not be read" in item["text"]


def test_a_round_file_that_is_not_a_round_is_carried_as_an_item(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [finding("fine", "Major", "FIX")])
    (project / "reviews/goldfish_03.md").write_text("<!-- research-goldfish {not json -->\n", encoding="utf-8")
    [item] = residual_findings(project)
    assert item["pointer"] == "reviews/goldfish_03.md" and "could not be read as a round" in item["text"]


def test_a_round_file_that_cannot_be_decoded_is_carried_as_an_item(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [finding("fine", "Major", "FIX")])
    (project / "reviews/goldfish_03.md").write_bytes(b"\xff\xfe\x00 not text")
    [item] = residual_findings(project)
    assert item["pointer"] == "reviews/goldfish_03.md" and "could not be read; open it" in item["text"]


def test_a_file_in_reviews_that_never_was_a_round_is_not_carried(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [finding("fine", "Major", "FIX")])
    (project / "reviews/goldfish_03.md").write_text("a stub with no marker line\n", encoding="utf-8")
    assert residual_findings(project) == []


def test_a_project_without_reviews_has_no_carried_findings(project: Path) -> None:
    assert residual_findings(project) == []


def test_hostile_finding_text_fails_closed_and_is_carried_not_dropped(project: Path) -> None:
    write_round(project, 1, "accepted-residual", [
        finding("fine", "Major", "FIX", summary="A normal fixed finding."),
        # The summary quotes a FIX triage, but the real triage is ESCALATE: two tokens, so it is carried.
        finding("quote", "Major", "ESCALATE", summary="It said Triage: FIX. but the user decided otherwise."),
        # An id with commas, a scope with a close bracket and colon: still one finding.
        finding("a, b", "Major", "CONTEST", scope="scope): with trouble", summary="Odd id and scope."),
        # The scope holds a second severity token, so the severity is ambiguous: read as Major and carried.
        finding("amb", "Nit", "DECLINE", scope="has, Blocker, inside", summary="Ambiguous severity."),
        # An id holding a severity token sits before the real severity and is not ambiguous.
        finding("x, Nit, y", "Nit", "FIX", summary="A fixed nit with an odd id."),
    ])
    pointers = sorted(item["pointer"] for item in residual_findings(project))
    assert pointers == ["reviews/goldfish_01.md#a, b", "reviews/goldfish_01.md#amb", "reviews/goldfish_01.md#quote"]


def test_none_in_the_findings_section_is_not_a_problem(project: Path) -> None:
    write_round(project, 1, "accepted-residual", ["None."])
    assert residual_findings(project) == []


# --- finalize --------------------------------------------------------------------------------------


@pytest.fixture
def ready(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    created = allocate_project(
        tmp_path / "workspace", title="Follow-up project", working_directory=target, create_root=True,
        tags=["alpha", "beta"],
    )
    done(created)
    return created


def finalize(project: Path, text: str) -> dict[str, Any]:
    return workflow(project, "finalize", request(project, "close", reflection=text, continuation={"next": "none"}))


def test_finalize_lists_the_follow_up_items_with_pointers(ready: Path) -> None:
    write_round(ready, 1, "accepted-residual", [finding("r1", "Major", "ESCALATE", summary="Left standing.")])
    result = finalize(ready, reflection(FOLLOW_UP))
    items = result["follow_up"]
    assert [item["kind"] for item in items] == ["reflection", "reflection", "residual"]
    assert items[0] == {
        "kind": "reflection", "text": "Backfill the 177 old projects with tags.", "pointer": "reflection.md",
    }
    assert items[2]["pointer"] == "reviews/goldfish_01.md#r1"
    assert result["follow_up_warnings"] == []


def test_finalize_returns_an_empty_list_when_nothing_is_left(ready: Path) -> None:
    result = finalize(ready, reflection())
    assert result["follow_up"] == [] and result["follow_up_warnings"] == []


def test_finalize_warns_about_follow_up_text_without_bullets(ready: Path) -> None:
    result = finalize(ready, reflection("## Follow-up\n\nSome work is left, written as a paragraph."))
    assert result["follow_up"] == []
    assert len(result["follow_up_warnings"]) == 1 and "bullet" in result["follow_up_warnings"][0]


def test_finalize_warns_when_a_title_only_looks_like_the_follow_up_section(ready: Path) -> None:
    result = finalize(ready, reflection("## Follow-up work\n\n- this would be lost silently"))
    assert result["follow_up"] == []
    [warning] = result["follow_up_warnings"]
    assert "Follow-up work" in warning and "Follow-up" in warning and "Follow-ups" in warning


def test_finalize_does_not_stage_the_follow_up_section_as_a_lesson(ready: Path) -> None:
    result = finalize(ready, reflection("## Follow-up after the failure\n\n- carry this over"))
    assert result["reflected_lessons"]["staged"] == []
    assert [item["text"] for item in result["follow_up"]] == []  # the title is not the section: no items


# --- the follow-up command -------------------------------------------------------------------------


AGREEMENT = "Yes, create the follow-up project for the open items."


@pytest.fixture
def source(ready: Path) -> Path:
    write_round(ready, 1, "accepted-residual", [finding("r1", "Major", "ESCALATE", summary="Left standing.")])
    finalize(ready, reflection(FOLLOW_UP))
    assert load_json(ready / "project.json")["status"] == "DONE"
    return ready


def names(workspace: Path) -> list[str]:
    return sorted(path.name for path in workspace.iterdir() if path.is_dir() and path.name[:2] == "20")


def tree(path: Path) -> dict[str, bytes]:
    return {str(item.relative_to(path)): item.read_bytes() for item in sorted(path.rglob("*")) if item.is_file()}


def test_follow_up_creates_an_aligning_project_with_nothing_else(source: Path) -> None:
    workspace = source.parent
    before = tree(source)
    new, warnings = follow_up_project(source, title="Next steps for tags", agreement=AGREEMENT)
    assert new.parent == workspace and new.name != source.name
    assert tree(source) == before  # the source is only read
    state = load_json(new / "project.json")
    assert state["status"] == "ALIGNING" and state["tasks"] == [] and state["revision"] == 0
    assert state["predecessor"] == source.name
    assert state["tags"] == ["alpha", "beta"]
    assert state["working_directory"] == load_json(source / "project.json")["working_directory"]
    assert "worktrees" not in state and state["execution"]["attempts"] == {}
    assert validate_v4_state(state, new, check_files=True).errors == []
    assert warnings == []


def test_the_agreement_is_recorded_verbatim_in_the_decision_history(source: Path) -> None:
    text = "  Yes, go ahead.\n\n- two bullets\n- inside the text\n\n```sh\nuse a fence\n```\n"
    new, _ = follow_up_project(source, title="Verbatim", agreement=text)
    spec = (new / "spec.md").read_text(encoding="utf-8")
    assert text in spec
    assert f"Follow-up of {source.name}" in spec
    fence = "````"  # one longer than the longest backtick run in the text
    assert f"\n{fence}\n{text}\n{fence}\n" in spec


def test_the_briefing_starts_with_the_marker_and_carries_the_items(source: Path) -> None:
    new, _ = follow_up_project(source, title="Briefing check", agreement=AGREEMENT)
    briefing = (new / "briefing.md").read_text(encoding="utf-8")
    lines = briefing.splitlines()
    assert lines[0] == f"Follow-up of: {source.name}" and lines[1] == ""
    assert lines[2] == "# Briefing check — briefing"
    assert "- [reflection] Backfill the 177 old projects with tags. (reflection.md)" in briefing
    assert "- [reflection] Check the dashboard on a phone. (reflection.md)" in briefing
    assert any(line.startswith("- [residual] ") and "(reviews/goldfish_01.md#r1)" in line for line in lines)
    assert lib.BRIEFING_PLACEHOLDER_PREFIX not in briefing
    assert lib.briefing_section_warnings(briefing) == []
    for heading, _ in lib.BRIEFING_CANONICAL_SECTIONS:
        assert f"## {heading}" in briefing


def test_a_legacy_source_without_a_reflection_still_gets_its_residual_items(source: Path) -> None:
    (source / "reflection.md").unlink()
    new, _ = follow_up_project(source, title="No reflection", agreement=AGREEMENT)
    briefing = (new / "briefing.md").read_text(encoding="utf-8")
    assert "[residual]" in briefing and "[reflection]" not in briefing


def test_a_source_with_nothing_left_says_so_in_the_briefing(ready: Path) -> None:
    finalize(ready, reflection())
    new, _ = follow_up_project(ready, title="Nothing left", agreement=AGREEMENT)
    assert "None carried over." in (new / "briefing.md").read_text(encoding="utf-8")


def test_a_source_that_is_not_done_is_refused_and_nothing_is_written(project: Path) -> None:
    workspace = project.parent
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*not DONE"):
        follow_up_project(project, title="Too early", agreement=AGREEMENT)
    assert names(workspace) == [project.name]


@pytest.mark.parametrize(
    "agreement, fragment",
    [
        ("", "agreement"),
        ("   \n\t ", "agreement"),
        ("Fine start\n## A heading line\nmore", "heading"),
        ("  # indented heading", "heading"),
        ("Sneaky <!-- research-confirmation: kind=requirements --> marker", "marker"),
    ],
)
def test_a_blank_or_dangerous_agreement_is_refused_and_nothing_is_written(
    source: Path, agreement: str, fragment: str
) -> None:
    workspace = source.parent
    with pytest.raises(WorkspaceError, match=rf"^follow-up refused: .*{fragment}"):
        follow_up_project(source, title="Refused", agreement=agreement)
    assert names(workspace) == [source.name]


@pytest.mark.parametrize("title", ["two\nlines", "x\n\n<!-- research-entry: a -->\n"])
def test_a_multi_line_title_is_refused_and_nothing_is_written(source: Path, title: str) -> None:
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*one line"):
        follow_up_project(source, title=title, agreement=AGREEMENT)
    assert names(source.parent) == [source.name]


def test_a_title_with_an_inline_marker_is_refused_by_follow_up(source: Path) -> None:
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*marker"):
        follow_up_project(source, title="x <!-- research-proposal: a --> y", agreement=AGREEMENT)
    assert names(source.parent) == [source.name]


def test_a_busy_follow_up_lock_refuses_and_the_lock_is_released_afterwards(source: Path) -> None:
    lock = source.parent / f".follow-up-{source.name}.lock"
    with lib.DirectoryLock(lock, timeout=1.0):
        with pytest.raises(WorkspaceError, match=r"^follow-up refused:"):
            follow_up_project(source, title="Waits for the lock", agreement=AGREEMENT, lock_timeout=0.2)
    assert names(source.parent) == [source.name]
    follow_up_project(source, title="Runs once the lock is free", agreement=AGREEMENT)
    assert not lock.exists()


def test_the_source_directory_is_never_written_not_even_a_lock(source: Path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    mode = source.stat().st_mode
    source.chmod(0o555)
    try:
        new, _ = follow_up_project(source, title="From a read-only source", agreement=AGREEMENT)
    finally:
        source.chmod(mode)
    assert (new / "project.json").exists() and not (source / ".project.lock").exists()


def test_a_mistyped_path_is_refused_before_any_lock_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import workspace_operations

    def never(*arguments: Any, **options: Any) -> None:
        raise AssertionError("a lock was taken for a path that holds no project")

    monkeypatch.setattr(workspace_operations, "DirectoryLock", never)
    (tmp_path / "stuff").mkdir()
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*project\.json"):
        follow_up_project(tmp_path / "stuff" / "2026-01-01-001", title="Typo", agreement=AGREEMENT)
    assert list((tmp_path / "stuff").iterdir()) == []


def test_a_follow_up_section_with_an_unclosed_fence_warns(source: Path) -> None:
    (source / "reflection.md").write_text(reflection("## Follow-up\n\n- kept\n\n```\n- hidden"), encoding="utf-8")
    new, warnings = follow_up_project(source, title="Fence trouble", agreement=AGREEMENT)
    assert len(warnings) == 1 and "could not be read in full" in warnings[0]
    assert "[reflection] kept" in (new / "briefing.md").read_text(encoding="utf-8")


def test_an_unreadable_reflection_is_a_warning_and_a_missing_one_is_not(source: Path) -> None:
    (source / "reflection.md").unlink()
    (source / "reflection.md").mkdir()  # exists but cannot be read as text
    _, warnings = follow_up_project(source, title="Odd reflection", agreement=AGREEMENT)
    assert len(warnings) == 1 and "reflection.md could not be read" in warnings[0]


def test_a_malformed_tags_field_in_the_source_is_named_not_called_missing(source: Path) -> None:
    state = load_json(source / "project.json")
    state["tags"] = "oops"
    atomic_write_json(source / "project.json", state)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*malformed"):
        follow_up_project(source, title="Corrupt tags", agreement=AGREEMENT)
    new, _ = follow_up_project(source, title="Given tags", agreement=AGREEMENT, tags=["fresh"])
    assert load_json(new / "project.json")["tags"] == ["fresh"]


def test_a_failure_after_the_project_exists_is_not_reported_as_a_refusal(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workspace_operations

    class BrokenRelease(lib.DirectoryLock):
        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            super().__exit__(exc_type, exc_value, traceback)
            raise WorkspaceError("cannot release the lock")

    monkeypatch.setattr(workspace_operations, "DirectoryLock", BrokenRelease)
    broken = source.parent / "2026-01-01-009"
    broken.mkdir()
    (broken / "project.json").write_text("{not json", encoding="utf-8")  # a warning that must not be lost
    with pytest.raises(WorkspaceError) as failure:
        follow_up_project(source, title="Created then failed", agreement=AGREEMENT)
    message = str(failure.value)
    assert "refused" not in message and "was created" in message and "cannot release the lock" in message
    assert "1 unreadable project" in message
    assert len(names(source.parent)) == 3  # the source, the broken neighbor and the new project


def test_an_index_rebuild_failure_after_the_commit_keeps_its_own_message(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: Any, **options: Any) -> None:
        raise WorkspaceError("boom")

    monkeypatch.setattr(lib, "rebuild_index", broken)
    with pytest.raises(lib.IndexNotRebuilt) as failure:
        follow_up_project(source, title="Index trouble", agreement=AGREEMENT)
    assert "refused" not in str(failure.value) and "nothing needs redoing" in str(failure.value)
    assert "is initialized" in str(failure.value)


def test_a_candidate_whose_briefing_cannot_be_read_is_a_warning_not_a_silent_skip(source: Path) -> None:
    child = allocate_project(source.parent, title="Odd child", working_directory=source.parent, predecessor=source.name)
    (child / "briefing.md").mkdir()  # exists, but cannot be read as text
    _, warnings = follow_up_project(source, title="With an odd neighbor", agreement=AGREEMENT)
    assert len(warnings) == 1 and child.name in warnings[0] and "could not be read" in warnings[0]


def test_the_scan_reads_the_briefing_where_the_project_really_lives(source: Path) -> None:
    first, _ = follow_up_project(source, title="First", agreement=AGREEMENT)
    state = load_json(first / "project.json")
    state["project"] = "renamed-in-the-record"  # the row's project field no longer names the directory
    atomic_write_json(first / "project.json", state)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*already a follow-up"):
        follow_up_project(source, title="Second", agreement=AGREEMENT)


def test_require_new_tags_has_one_refusal_wording() -> None:
    assert require_new_tags(["A Tag"]) == ["a-tag"]
    for bad in ([], ["x", "x"], ["Bad!"], [f"t{n}" for n in range(6)]):
        with pytest.raises(WorkspaceError, match=r"^init created nothing: .*suggest-tags"):
            require_new_tags(bad)


def test_a_crash_before_project_json_leaves_an_inert_directory_and_a_retry_works(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = lib.atomic_write_json

    def crash(path: Path, value: Any, *args: Any, **options: Any) -> None:
        if path.name == "project.json" and path.parent != source:
            raise OSError("disk full")
        real(path, value, *args, **options)

    monkeypatch.setattr(lib, "atomic_write_json", crash)
    before = tree(source)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*disk full"):
        follow_up_project(source, title="Interrupted", agreement=AGREEMENT)
    [inert] = [path for path in source.parent.iterdir() if path.is_dir() and path.name[:2] == "20" and path != source]
    assert not (inert / "project.json").exists() and (inert / "spec.md").exists()
    assert tree(source) == before
    monkeypatch.setattr(lib, "atomic_write_json", real)
    new, _ = follow_up_project(source, title="Retried", agreement=AGREEMENT)
    assert load_json(new / "project.json")["predecessor"] == source.name and new != inert


def test_a_second_follow_up_of_the_same_source_is_refused_and_names_the_first(source: Path) -> None:
    first, _ = follow_up_project(source, title="First follow-up", agreement=AGREEMENT)
    with pytest.raises(WorkspaceError, match=rf"^follow-up refused: .*{first.name}"):
        follow_up_project(source, title="Second follow-up", agreement=AGREEMENT)
    assert names(source.parent) == sorted([source.name, first.name])


def test_a_maintenance_child_does_not_count_as_a_follow_up(source: Path) -> None:
    allocate_project(
        source.parent, title="Maintenance of the source", working_directory=source, predecessor=source.name
    )
    new, _ = follow_up_project(source, title="Real follow-up", agreement=AGREEMENT)
    assert load_json(new / "project.json")["predecessor"] == source.name


def test_a_project_with_the_marker_but_another_predecessor_does_not_count(source: Path) -> None:
    other = allocate_project(source.parent, title="Unrelated", working_directory=source.parent)
    (other / "briefing.md").write_text(f"Follow-up of: {source.name}\n\n# x\n", encoding="utf-8")
    follow_up_project(source, title="Still allowed", agreement=AGREEMENT)


def test_unreadable_projects_cannot_be_checked_and_produce_a_warning(source: Path) -> None:
    broken = source.parent / "2026-01-01-009"
    broken.mkdir()
    (broken / "project.json").write_text("{not json", encoding="utf-8")
    _, warnings = follow_up_project(source, title="With a broken neighbor", agreement=AGREEMENT)
    assert len(warnings) == 1 and "1 unreadable" in warnings[0]


def test_tags_given_replace_the_source_tags(source: Path) -> None:
    new, _ = follow_up_project(source, title="Own tags", agreement=AGREEMENT, tags=["Fresh Tag"])
    assert load_json(new / "project.json")["tags"] == ["fresh-tag"]


def test_a_tagless_source_needs_tags_and_bad_tags_are_refused(source: Path) -> None:
    state = load_json(source / "project.json")
    del state["tags"]
    atomic_write_json(source / "project.json", state)
    workspace = source.parent
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*--tag"):
        follow_up_project(source, title="No tags anywhere", agreement=AGREEMENT)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*at most 5"):
        follow_up_project(source, title="Too many", agreement=AGREEMENT, tags=[f"t{n}" for n in range(6)])
    assert names(workspace) == [source.name]
    new, _ = follow_up_project(source, title="Tagged now", agreement=AGREEMENT, tags=["one"])
    assert load_json(new / "project.json")["tags"] == ["one"]


def set_working_directory(source: Path, value: Any) -> None:
    state = load_json(source / "project.json")
    if value is None:
        state.pop("working_directory", None)
    else:
        state["working_directory"] = str(value)
    atomic_write_json(source / "project.json", state)


def test_a_worktree_path_maps_to_its_repository(source: Path, tmp_path: Path) -> None:
    repository = tmp_path / "myrepo"
    worktree = tmp_path / "myrepo.worktrees" / source.name
    worktree.mkdir(parents=True)
    repository.mkdir()
    set_working_directory(source, worktree)
    new, _ = follow_up_project(source, title="From a worktree", agreement=AGREEMENT)
    assert load_json(new / "project.json")["working_directory"] == str(repository.resolve())


def test_a_gone_working_directory_is_refused_and_the_option_overrides_it(source: Path, tmp_path: Path) -> None:
    set_working_directory(source, tmp_path / "removed")
    workspace = source.parent
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*--working-directory"):
        follow_up_project(source, title="Gone", agreement=AGREEMENT)
    assert names(workspace) == [source.name]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    new, _ = follow_up_project(source, title="Override", agreement=AGREEMENT, working_directory=elsewhere)
    assert load_json(new / "project.json")["working_directory"] == str(elsewhere.resolve())


def test_an_option_that_is_not_a_directory_is_refused(source: Path, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*--working-directory"):
        follow_up_project(source, title="Missing option", agreement=AGREEMENT, working_directory=tmp_path / "nope")


def test_a_source_without_a_working_directory_is_refused(source: Path) -> None:
    state = load_json(source / "project.json")
    state["schema_version"] = 3
    state.pop("execution")
    state.pop("tags", None)
    state.pop("working_directory")
    atomic_write_json(source / "project.json", state)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*working directory"):
        follow_up_project(source, title="No directory", agreement=AGREEMENT, tags=["x"])


def test_a_v3_source_is_accepted_and_the_new_project_is_v4(source: Path) -> None:
    state = load_json(source / "project.json")
    state["schema_version"] = 3
    state.pop("execution")
    state.pop("tags")
    atomic_write_json(source / "project.json", state)
    new, _ = follow_up_project(source, title="From schema three", agreement=AGREEMENT, tags=["migrated"])
    assert load_json(new / "project.json")["schema_version"] == 4


def test_an_unreadable_or_mismatched_source_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    broken = workspace / "2026-01-01-001"
    broken.mkdir(parents=True)
    (broken / "project.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"^follow-up refused:"):
        follow_up_project(broken, title="Broken", agreement=AGREEMENT)
    with pytest.raises(WorkspaceError, match=r"^follow-up refused:"):
        follow_up_project(workspace / "2026-01-01-002", title="Absent", agreement=AGREEMENT)
    atomic_write_json(broken / "project.json", {"project": "someone-else", "status": "DONE"})
    with pytest.raises(WorkspaceError, match=r"^follow-up refused: .*match"):
        follow_up_project(broken, title="Mismatch", agreement=AGREEMENT)


# --- the command line ------------------------------------------------------------------------------


def _manage(arguments: list[Any]) -> int:
    with patch.object(sys, "argv", ["manage", *[str(argument) for argument in arguments]]):
        return manage_workspace.main()


def test_the_command_prints_the_new_project(source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _manage(["follow-up", source, "--title", "Via the CLI", "--agreement", AGREEMENT, "--tag", "cli", "--json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "follow-up" and result["revision"] == 0
    assert result["predecessor"] == source.name and result["tags"] == ["cli"] and result["warnings"] == []
    assert Path(result["project_directory"]).parent == source.parent


def test_the_command_without_json_prints_the_path(source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _manage(["follow-up", source, "--title", "Plain output", "--agreement", AGREEMENT]) == 0
    assert capsys.readouterr().out.strip().startswith(str(source.parent))


def test_a_refusal_exits_nonzero_with_the_prefix_and_writes_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _manage(["follow-up", project, "--title", "Too early", "--agreement", AGREEMENT])
    assert code != 0 and "follow-up refused:" in capsys.readouterr().err
    assert names(project.parent) == [project.name]


def test_the_command_prints_its_warnings_on_stderr(source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = source.parent / "2026-01-01-009"
    broken.mkdir()
    (broken / "project.json").write_text("{not json", encoding="utf-8")
    assert _manage(["follow-up", source, "--title", "Warned", "--agreement", AGREEMENT]) == 0
    assert "WARNING:" in capsys.readouterr().err
