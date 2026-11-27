"""c4: a write that makes a closed passing Goldfish round stale returns a warning.

`edit` and `round` return the warning; `decision append` never does, because the spec digest ignores the
decision history. Fixtures use a temporary repository only to allocate a project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import workspace_goldfish as goldfish
from workspace_documents import append_record, edit_document
from workspace_journal import snapshot
from workspace_lib import WorkspaceError
from workspace_workflows import workflow

from tests.plugins.research.project.test_goldfish import (
    CONFIRMATION,
    PLANNING_BODY,
    close,
    finding,
    gold,
    open_round,
)
from tests.plugins.research.project.test_worktrees import load_state, make_project, make_repository

HEADING = "Objective and audience"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = make_project(tmp_path, make_repository(tmp_path))
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    spec = (path / "spec.md").read_text(encoding="utf-8")
    spec = spec.replace("Alignment in progress.\n", f"### {HEADING}\n\nA test objective.\n")
    (path / "spec.md").write_text(spec, encoding="utf-8")
    return path


def passed(project: Path, target: str) -> int:
    opened = open_round(project, target, ("combined",))
    return close(project, opened, reports=[{"stage": "combined", "summary": "read it"}])["round"]


def edit_planning(project: Path, body: str) -> dict[str, Any]:
    token = snapshot(project / "planning.md")[1]
    return edit_document(project, "planning", expected_sha256=token, body=body)


def edit_spec(project: Path, text: str) -> dict[str, Any]:
    token = snapshot(project / "spec.md")[1]
    return edit_document(project, "spec", expected_sha256=token, sections={HEADING: text})


def message(number: int, outcome: str, subject: str) -> str:
    return (
        f"goldfish round {number:02d} ({outcome}) no longer covers {subject} after this write; "
        "the next confirmation needs a delta round. Edit before closing a round."
    )


def test_a_planning_edit_after_a_passing_round_warns(project: Path) -> None:
    number = passed(project, "planning")
    result = edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert result["warnings"] == [message(number, "accepted", "the planning document")]


def test_a_spec_edit_after_a_passing_round_warns(project: Path) -> None:
    number = passed(project, "spec")
    assert edit_spec(project, "Another objective.")["warnings"] == [message(number, "accepted", "spec.md")]


def test_a_waived_round_warns_too(project: Path) -> None:
    gold(project, operation="waive", target="planning", reason="no subagent", confirmation=CONFIRMATION)
    result = edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert result["warnings"] == [message(1, "waived", "the planning document")]


def test_a_reflow_that_keeps_the_digest_gives_no_warning(project: Path) -> None:
    passed(project, "planning")
    assert "warnings" not in edit_planning(project, PLANNING_BODY.replace("\n\n", "\n\n\n"))


def test_a_round_that_was_already_stale_gets_no_second_warning(project: Path) -> None:
    passed(project, "planning")
    assert "warnings" in edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert "warnings" not in edit_planning(project, PLANNING_BODY + "\nEven more.\n")


def test_an_edit_back_to_the_covered_text_gives_no_warning_and_then_warns_again(project: Path) -> None:
    passed(project, "planning")
    edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert "warnings" not in edit_planning(project, PLANNING_BODY)
    assert "warnings" in edit_planning(project, PLANNING_BODY + "\nAgain.\n")


def test_a_reaudit_required_round_gives_no_warning(project: Path) -> None:
    opened = open_round(project)
    edit_planning(project, PLANNING_BODY + "\nFix.\n")
    closed = close(project, opened, [finding("F1", "Blocker", "FIX")])
    assert closed["outcome"] == "reaudit-required"
    assert "warnings" not in edit_planning(project, PLANNING_BODY + "\nMore fix.\n")


def test_an_open_round_or_no_round_gives_no_warning(project: Path) -> None:
    assert "warnings" not in edit_planning(project, PLANNING_BODY + "\nOne.\n")
    open_round(project)
    assert "warnings" not in edit_planning(project, PLANNING_BODY + "\nTwo.\n")


def test_only_the_latest_closed_round_of_the_target_counts(project: Path) -> None:
    passed(project, "planning")
    edit_planning(project, PLANNING_BODY + "\nMore.\n")
    second = passed(project, "planning")
    result = edit_planning(project, PLANNING_BODY + "\nLater.\n")
    assert result["warnings"] == [message(second, "accepted", "the planning document")]


def test_a_round_of_the_other_target_is_not_named(project: Path) -> None:
    passed(project, "spec")
    assert "warnings" not in edit_planning(project, PLANNING_BODY + "\nMore.\n")


def test_a_decision_append_gives_no_warning_and_keeps_the_round_current(project: Path) -> None:
    passed(project, "spec")
    result = append_record(project, "decision", "The user chose X.", entry_id="d1")
    assert "warnings" not in result
    state = load_state(project)
    assert goldfish.version_for(project, state, "spec") == goldfish.round_documents(project)[-1].resolved


def test_the_round_action_returns_a_warning_for_each_stale_round(project: Path) -> None:
    first, second = passed(project, "planning"), passed(project, "spec")
    result = workflow(
        project,
        "round",
        {
            "id": "round-c4",
            "expected_revision": load_state(project)["revision"],
            "tokens": {"spec": snapshot(project / "spec.md")[1], "planning": snapshot(project / "planning.md")[1]},
            "sections": {HEADING: "Changed objective."},
            "planning": PLANNING_BODY + "\nMore.\n",
        },
    )
    assert result["warnings"] == [
        message(second, "accepted", "spec.md"),
        message(first, "accepted", "the planning document"),
    ]


def test_the_round_action_gives_no_warning_when_nothing_goes_stale(project: Path) -> None:
    passed(project, "planning")
    request = {
        "id": "round-quiet",
        "expected_revision": load_state(project)["revision"],
        "tokens": {"spec": snapshot(project / "spec.md")[1]},
        "decisions": [{"id": "d2", "body": "The user chose Y."}],
    }
    assert "warnings" not in workflow(project, "round", request)


def test_the_helper_warns_only_for_the_targets_it_is_given(project: Path) -> None:
    passed(project, "planning")
    changed = {"planning": PLANNING_BODY + "\nMore.\n"}
    assert goldfish.stale_round_warnings(project, {}, {}) == []
    assert goldfish.stale_round_warnings(project, {"planning": PLANNING_BODY}, {}) == []
    assert goldfish.stale_round_warnings(project, {}, changed) == []
    assert len(goldfish.stale_round_warnings(project, {"planning": PLANNING_BODY}, changed)) == 1


@pytest.mark.parametrize(
    ("document", "fields", "fragment"),
    [
        ("planning", {}, "provide exactly one of body or sections"),
        ("planning", {"body": "x", "sections": {"A": "b"}}, "provide exactly one of body or sections"),
        ("spec", {"body": "x"}, "spec edits require a sections mapping"),
        ("planning", {"sections": {"A": "b"}}, "planning edits require a body"),
        ("notes", {"body": "x"}, "editable document must be spec, reflection, planning, architecture, or handoff"),
    ],
)
def test_an_edit_request_that_is_malformed_is_refused_before_any_write(
    project: Path, document: str, fields: dict[str, Any], fragment: str
) -> None:
    before = (project / "planning.md").read_bytes()
    with pytest.raises(WorkspaceError, match=fragment):
        edit_document(project, document, expected_sha256="x", **fields)
    assert (project / "planning.md").read_bytes() == before


def test_an_empty_body_is_refused_and_writes_nothing(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="planning body must not be empty"):
        edit_planning(project, "  \n")
    assert (project / "planning.md").read_text(encoding="utf-8") == PLANNING_BODY


def test_an_open_round_on_top_of_a_passing_one_does_not_hide_the_passing_one(project: Path) -> None:
    number = passed(project, "planning")
    open_round(project)
    result = edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert result["warnings"] == [message(number, "accepted", "the planning document")]


def test_a_later_round_of_the_other_target_does_not_hide_the_round_of_this_one(project: Path) -> None:
    number = passed(project, "planning")
    passed(project, "spec")
    result = edit_planning(project, PLANNING_BODY + "\nMore.\n")
    assert result["warnings"] == [message(number, "accepted", "the planning document")]


def test_a_replayed_round_returns_the_stored_result_without_the_advisory(project: Path) -> None:
    number = passed(project, "planning")
    request = {
        "id": "round-replay",
        "expected_revision": load_state(project)["revision"],
        "tokens": {"planning": snapshot(project / "planning.md")[1]},
        "planning": PLANNING_BODY + "\nMore.\n",
    }
    first = workflow(project, "round", request)
    assert first["warnings"] == [message(number, "accepted", "the planning document")]
    replay = workflow(project, "round", request)
    assert replay == {key: value for key, value in first.items() if key != "warnings"}
    assert workflow(project, "round", request) == replay
