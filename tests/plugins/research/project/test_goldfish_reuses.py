"""P6: `goldfish waive` may name an earlier passing round it reuses; the user's words stay required.

Design: artifacts/architecture.md (project 2026-11-06-002) §4 "Workflow inputs and refusals" and
decision D9. `reuses` must name an accepted or accepted-residual round with the same target and
worktree. It is recorded in the round body; the `research-goldfish` marker keeps the 0.33.0 key set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import workspace_goldfish as goldfish
from workspace_goldfish import MARKER_KEYS
from workspace_session import update_project_data
from workspace_workflows import input_schema

from tests.plugins.research.project.test_goldfish import (
    CODE_TASK,
    CONFIRMATION,
    DESIGN_BODY,
    PLANNING_BODY,
    SPEC_BODY,
    close,
    edit_planning,
    finding,
    gold,
    open_round,
    refuses,
    report,
    rounds,
)
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    load_state,
    make_project,
    make_repository,
    record,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def project(tmp_path: Path, repository: Path) -> Path:
    path = make_project(tmp_path, repository)
    record(path, add_worktree(repository))
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text(DESIGN_BODY, encoding="utf-8")
    spec = (path / "spec.md").read_text(encoding="utf-8")
    (path / "spec.md").write_text(spec + "\n" + SPEC_BODY, encoding="utf-8")
    return path


def waive(project: Path, target: str = "planning", **fields: object) -> dict[str, object]:
    return gold(
        project, operation="waive", target=target, reason="an editorial edit", confirmation=CONFIRMATION, **fields
    )


def marker(project: Path, number: int) -> dict[str, object]:
    first = (project / "reviews" / f"goldfish_{number:02d}.md").read_text(encoding="utf-8").split("\n", 1)[0]
    return json.loads(first.removeprefix("<!-- research-goldfish ").removesuffix(" -->"))


def test_an_accepted_round_is_reused_and_recorded_in_the_body(project: Path) -> None:
    close(project, open_round(project))
    edit_planning(project, "One word changed.\n")
    waived = waive(project, reuses=1)
    assert waived["outcome"] == "waived" and waived["round"] == 2 and waived["reuses"] == 1
    text = (project / "reviews" / "goldfish_02.md").read_text(encoding="utf-8")
    assert "## Reuses" in text and "Reuses: round 01 (accepted, resolved " in text and "Response: accept" in text
    assert set(marker(project, 2)) == MARKER_KEYS and "reuses" not in marker(project, 2)
    assert [item.outcome for item in rounds(project)] == ["accepted", "waived"]


def test_an_accepted_residual_round_can_be_reused(project: Path) -> None:
    first = open_round(project)
    edit_planning(project)
    close(project, first, [finding("F1", "Blocker", "FIX")])
    second = open_round(project)
    edit_planning(project, "Fixed after the re-audit.\n")
    close(project, second, [finding("F2", "Blocker", "FIX")], confirmation=CONFIRMATION)
    assert rounds(project)[-1].outcome == "accepted-residual"
    assert waive(project, reuses=2)["reuses"] == 2


def test_a_waiver_cannot_back_a_waiver(project: Path) -> None:
    waive(project)
    refuses(
        project,
        "round 01 is waived",
        operation="waive",
        target="planning",
        reason="r",
        confirmation=CONFIRMATION,
        reuses=1,
    )


def test_a_failed_or_open_round_cannot_be_reused(project: Path) -> None:
    opened = open_round(project)
    refuses(
        project,
        "round 01 is open",
        operation="waive",
        target="planning",
        reason="r",
        confirmation=CONFIRMATION,
        reuses=1,
    )
    edit_planning(project)
    close(project, opened, [finding("F1", "Blocker", "FIX")])
    assert rounds(project)[-1].outcome == "reaudit-required"
    refuses(
        project,
        "round 01 is reaudit-required",
        operation="waive",
        target="planning",
        reason="r",
        confirmation=CONFIRMATION,
        reuses=1,
    )


def test_a_round_of_another_target_cannot_be_reused(project: Path) -> None:
    close(project, open_round(project))
    refuses(
        project,
        "round 01 reviewed planning",
        operation="waive",
        target="spec",
        reason="r",
        confirmation=CONFIRMATION,
        reuses=1,
    )


def test_a_round_of_another_worktree_cannot_be_reused(project: Path) -> None:
    first = Path(load_state(project)["worktrees"][0]["path"])
    other = goldfish.Round(1, "diff", "/elsewhere", False, "a" * 40, "a" * 40, "accepted", ("combined",), None, None)
    (project / "reviews").mkdir(exist_ok=True)
    (project / "reviews" / "goldfish_01.md").write_text(goldfish.render_marker(other) + "\n\nAccepted.\n")
    refuses(
        project,
        "round 01 reviewed diff of /elsewhere",
        operation="waive",
        target="diff",
        worktree=str(first),
        reason="r",
        confirmation=CONFIRMATION,
        reuses=1,
    )
    opened = open_round(project, "diff", ("combined",), worktree=str(first))
    close(project, opened, reports=report(("combined",)))
    waived = gold(
        project, operation="waive", target="diff", worktree=str(first), reason="r", confirmation=CONFIRMATION, reuses=2
    )
    assert waived["reuses"] == 2


@pytest.mark.parametrize("value", [9, 0, -1, True, "1", 1.0])
def test_a_missing_or_malformed_round_is_refused(project: Path, value: object) -> None:
    close(project, open_round(project))
    refuses(
        project, "reuses", operation="waive", target="planning", reason="r", confirmation=CONFIRMATION, reuses=value
    )


def test_the_users_words_stay_required(project: Path) -> None:
    close(project, open_round(project))
    refuses(project, "confirmation", operation="waive", target="planning", reason="r", reuses=1)


def test_the_schema_lists_reuses_as_optional() -> None:
    schema = input_schema("goldfish")
    assert "reuses" in schema["allowed"]
    assert "reuses" not in schema["required_by"]["operation=waive"]
    assert "reuses" in goldfish.OPERATION_FIELDS["waive"]
