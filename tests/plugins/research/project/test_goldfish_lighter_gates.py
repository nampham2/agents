"""The lighter Goldfish gates: a spec or planning round needs one combined reviewer and only a Blocker forces
a re-audit. Design and diff rounds keep their stage and outcome rules (S1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from workspace_lib import WorkspaceError
from workspace_session import update_project_data

from tests.plugins.research.project.test_goldfish import (
    CODE_TASK,
    CONFIRMATION,
    DESIGN_BODY,
    PLANNING_BODY,
    SPEC_BODY,
    close,
    edit_planning,
    finding,
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

ONE = ("combined",)


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


@pytest.mark.parametrize("target", ["spec", "planning"])
def test_a_spec_or_planning_round_needs_only_the_combined_stage(project: Path, target: str) -> None:
    opened = open_round(project, target, ONE)
    closed = close(project, opened, reports=report(ONE))
    assert closed["outcome"] == "accepted"


@pytest.mark.parametrize("target", ["spec", "planning"])
def test_a_design_critic_only_round_is_still_valid_for_spec_and_planning(project: Path, target: str) -> None:
    assert open_round(project, target, ("design-critic",))["round"] == 1


def test_a_round_without_combined_or_design_critic_is_still_refused(project: Path) -> None:
    refuses(project, "combined stage or the design-critic", operation="open", target="spec", stages=["comprehension"])


@pytest.mark.parametrize("target", ["spec", "planning"])
def test_a_fixed_major_closes_accepted_without_a_re_audit(project: Path, target: str) -> None:
    opened = open_round(project, target, ONE)
    edit_planning(project) if target == "planning" else _edit_spec(project)
    closed = close(project, opened, [finding("F1", "Major", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "accepted"
    assert closed["resolved"] != opened["reviewed"]


def test_a_fixed_blocker_forces_one_re_audit_and_a_clean_re_audit_is_accepted(project: Path) -> None:
    first = open_round(project, "planning", ONE)
    edit_planning(project)
    closed = close(project, first, [finding("F1", "Blocker", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "reaudit-required"
    second = open_round(project, "planning", ONE)
    assert second["reaudit_of"] == 1
    assert close(project, second, reports=report(ONE))["outcome"] == "accepted"


def test_a_re_audit_that_holds_only_majors_needs_no_confirmation(project: Path) -> None:
    first = open_round(project, "planning", ONE)
    edit_planning(project)
    close(project, first, [finding("F1", "Blocker", "FIX")], reports=report(ONE))
    second = open_round(project, "planning", ONE)
    edit_planning(project, "Fixed after the re-audit.\n")
    closed = close(project, second, [finding("F2", "Major", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "accepted"


def test_a_re_audit_that_holds_a_blocker_needs_the_users_words(project: Path) -> None:
    first = open_round(project, "planning", ONE)
    edit_planning(project)
    close(project, first, [finding("F1", "Blocker", "FIX")], reports=report(ONE))
    second = open_round(project, "planning", ONE)
    edit_planning(project, "Fixed after the re-audit.\n")
    with pytest.raises(WorkspaceError, match="re-audit that still holds a Blocker or Major"):
        close(project, second, [finding("F2", "Blocker", "FIX")], reports=report(ONE))
    closed = close(
        project, second, [finding("F2", "Blocker", "FIX")], reports=report(ONE), confirmation=CONFIRMATION
    )
    assert closed["outcome"] == "accepted-residual"


def test_a_delta_round_on_a_spec_or_plan_needs_only_the_combined_stage(project: Path) -> None:
    close(project, open_round(project, "planning", ONE), reports=report(ONE))
    edit_planning(project)
    delta = open_round(project, "planning", ONE)
    assert delta["delta_of"] == 1
    assert close(project, delta, reports=report(ONE))["outcome"] == "accepted"


def test_a_contested_major_still_needs_the_users_words(project: Path) -> None:
    opened = open_round(project, "planning", ONE)
    with pytest.raises(WorkspaceError, match="contested or escalated needs the user's confirmation"):
        close(project, opened, [finding("F1", "Major", "CONTEST")], reports=report(ONE))


def test_a_delta_round_on_a_design_still_needs_comprehension_and_combined(project: Path) -> None:
    close(project, open_round(project, "design", ("comprehension", "combined")))
    _edit_design(project, "A change.")
    refuses(
        project, "delta round on a design or diff target lists", operation="open", target="design",
        stages=["combined"],
    )
    assert open_round(project, "design", ("comprehension", "combined"))["delta_of"] == 1


def test_a_design_round_keeps_the_major_driven_outcome(project: Path) -> None:
    opened = open_round(project, "design", ("comprehension", "combined"))
    _edit_design(project, "A fix.")
    closed = close(project, opened, [finding("F1", "Major", "FIX")])
    assert closed["outcome"] == "reaudit-required"
    again = open_round(project, "design", ("comprehension", "combined"))
    _edit_design(project, "Another fix.")
    with pytest.raises(WorkspaceError, match="re-audit that still holds a Blocker or Major"):
        close(project, again, [finding("F2", "Major", "FIX")])
    assert rounds(project)[-1].outcome == "open"


def _edit_spec(project: Path) -> None:
    path = project / "spec.md"
    text = path.read_text(encoding="utf-8")
    assert "## Current specification\n" in text
    changed = text.replace("## Current specification\n", "## Current specification\nA spec change.\n", 1)
    path.write_text(changed, encoding="utf-8")


def _edit_design(project: Path, text: str) -> None:
    (project / "artifacts" / "architecture.md").write_text(f"# Design\n\n{text}\n", encoding="utf-8")
