"""Review risk, the standing review fallback and amendments at policy 3 (T04).

`low` and `standard` use one combined reviewer and differ in what forces a re-audit; `high` keeps the
multi-stage review. A waiver recorded once at alignment stands for spec and planning below `high`. An
explicit editorial or reviewer-requested edit after a passing close is an amendment, not a new review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_goldfish as goldfish
import workspace_lib
from workspace_journal import snapshot
from workspace_lib import WorkspaceError
from workspace_session import prepare_update, update_project_data
from workspace_workflows import _settings, input_schema, workflow

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
from tests.plugins.research.project.test_goldfish_gates import confirm
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    load_state,
    make_project,
    make_repository,
    record,
)

USER = {"source": "user reply in the test", "response": "Use that setting"}
ONE = ("combined",)
LADDER = ("comprehension", "design-critic", "zoom-out", "readiness")
COUNTER = iter(range(1, 10_000))


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def project(tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(workspace_lib, "NEW_PROJECT_POLICY", 3)
    path = make_project(tmp_path, repository)
    stored = json.loads((path / "project.json").read_text(encoding="utf-8"))
    assert stored["policy_version"] == 3
    # A target of `local` owes no merge request, so the code task below needs no MR record.
    (path / "project.json").write_text(json.dumps({**stored, "delivery_target": "local"}), encoding="utf-8")
    record(path, add_worktree(repository))
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text(DESIGN_BODY, encoding="utf-8")
    spec = (path / "spec.md").read_text(encoding="utf-8")
    (path / "spec.md").write_text(spec + "\n" + SPEC_BODY, encoding="utf-8")
    return path


def settings(project: Path, **fields: Any) -> dict[str, Any]:
    token = snapshot(project / "spec.md")[1]
    request = {
        "id": f"s-{next(COUNTER)}", "expected_revision": load_state(project)["revision"],
        "tokens": {"spec": token}, "confirmation": USER, **fields,
    }
    return workflow(project, "settings", request)


def spec_edit(project: Path, text: str = "A changed sentence.\n") -> None:
    path = project / "spec.md"
    marker = "## Current specification\n"
    path.write_text(path.read_text(encoding="utf-8").replace(marker, marker + "\n" + text, 1), encoding="utf-8")


def passing(project: Path, target: str = "spec") -> dict[str, Any]:
    opened = open_round(project, target, ONE)
    return close(project, opened, reports=report(ONE))


def amend(project: Path, target: str = "spec", **fields: Any) -> dict[str, Any]:
    request = {"operation": "amend", "target": target, "kind": "editorial", "reason": "fixed a typo", **fields}
    return gold(project, **request)


# --- the settings action and its fields ----------------------------------------------------------


def test_settings_records_the_review_risk_and_one_decision_line(project: Path) -> None:
    result = settings(project, review_risk="high")
    assert load_state(project)["review_risk"] == "high"
    assert result["review_risk"] == "high"
    spec = (project / "spec.md").read_text(encoding="utf-8")
    assert "Use that setting" in spec.split("## Decision history")[1]
    assert "review_risk" in spec.split("## Decision history")[1]


def test_settings_needs_the_users_words(project: Path) -> None:
    request = {"id": "s-x", "expected_revision": load_state(project)["revision"], "review_risk": "low",
               "tokens": {"spec": snapshot(project / "spec.md")[1]}}
    with pytest.raises(WorkspaceError, match="confirmation"):
        workflow(project, "settings", request)
    assert "review_risk" not in load_state(project)


@pytest.mark.parametrize("bad", ["", "medium", "HIGH", 3, True, None, ["low"]])
def test_settings_refuses_an_unknown_review_risk(project: Path, bad: object) -> None:
    if bad is None:
        settings(project, review_risk="low")
        settings(project, review_risk=None)
        assert "review_risk" not in load_state(project)
        return
    with pytest.raises(WorkspaceError, match="review_risk"):
        settings(project, review_risk=bad)


def test_the_fallback_needs_a_reason_to_waive(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="reason"):
        settings(project, review_fallback={"mode": "waive-recorded"})
    with pytest.raises(WorkspaceError, match="mode"):
        settings(project, review_fallback={"mode": "skip", "reason": "x"})
    settings(project, review_fallback={"mode": "waive-recorded", "reason": "no fresh agents on this host"})
    stored = load_state(project)["review_fallback"]
    assert stored == {"mode": "waive-recorded", "reason": "no fresh agents on this host"}
    settings(project, review_fallback={"mode": "ask"})
    assert load_state(project)["review_fallback"]["mode"] == "ask"


def test_options_merge_per_key_and_refuse_unknown_keys(project: Path) -> None:
    settings(project, options={"reports": True})
    settings(project, options={"style_advice": False})
    assert load_state(project)["options"] == {"reports": True, "style_advice": False}
    with pytest.raises(WorkspaceError, match="options"):
        settings(project, options={"colour": True})
    with pytest.raises(WorkspaceError, match="options"):
        settings(project, options={"reports": "yes"})


def test_update_cannot_write_the_settings_fields(project: Path) -> None:
    state = load_state(project)
    for patch in ({"review_risk": "low"}, {"review_fallback": {"mode": "ask"}}, {"options": {"reports": True}}):
        with pytest.raises(WorkspaceError):
            prepare_update(state, patch)
    assert "review_risk" not in load_state(project)


def test_the_settings_fields_are_refused_below_policy_three(project: Path) -> None:
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    for policy in (0, 1, 2):
        errors = workspace_lib.validate_v4_state(
            {**state, "policy_version": policy, "review_risk": "low"}, project, check_files=False
        ).errors
        assert any("review_risk" in text for text in errors)


def test_settings_is_refused_below_policy_three_and_when_done(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert state["policy_version"] == 3
    schema = input_schema("settings")
    assert {"review_risk", "review_fallback", "options", "confirmation"} <= set(schema["allowed"])
    assert "confirmation" in schema["required"]


# --- the stage table -----------------------------------------------------------------------------


@pytest.mark.parametrize("risk", ["low", "standard"])
@pytest.mark.parametrize("target", ["spec", "planning", "design"])
def test_low_and_standard_use_exactly_one_combined_reviewer(project: Path, risk: str, target: str) -> None:
    settings(project, review_risk=risk)
    refuses(project, "exactly", operation="open", target=target, stages=["comprehension", "combined"])
    refuses(project, "exactly", operation="open", target=target, stages=list(LADDER))
    assert open_round(project, target, ONE)["round"] == 1


@pytest.mark.parametrize("target", ["spec", "planning"])
def test_high_uses_one_combined_reviewer_for_documents(project: Path, target: str) -> None:
    settings(project, review_risk="high")
    refuses(project, "exactly", operation="open", target=target, stages=["comprehension", "combined"])
    assert open_round(project, target, ONE)["round"] == 1


def test_high_keeps_the_ladder_for_design_and_diff(project: Path) -> None:
    settings(project, review_risk="high")
    refuses(project, "exactly", operation="open", target="design", stages=["combined"])
    refuses(project, "exactly", operation="open", target="design", stages=["comprehension", "combined"])
    assert open_round(project, "design", LADDER)["round"] == 1


def test_an_unset_risk_reads_as_standard(project: Path) -> None:
    refuses(project, "exactly", operation="open", target="design", stages=list(LADDER))
    assert open_round(project, "design", ONE)["round"] == 1


def test_a_delta_round_follows_the_same_table(project: Path) -> None:
    settings(project, review_risk="standard")
    passing(project, "planning")
    edit_planning(project)
    refuses(project, "exactly", operation="open", target="planning", stages=["comprehension", "combined"])
    assert open_round(project, "planning", ONE)["delta_of"] == 1


def test_policies_below_three_keep_the_effort_stage_rules(tmp_path: Path, repository: Path) -> None:
    path = make_project(tmp_path, repository)  # the suite's default: no policy, today's rules
    record(path, add_worktree(repository))
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    assert open_round(path, "planning", ("comprehension", "combined"))["round"] == 1


# --- the re-audit rule ---------------------------------------------------------------------------


def test_at_low_an_unfixed_major_in_a_design_round_closes_accepted(project: Path) -> None:
    settings(project, review_risk="low")
    opened = open_round(project, "design", ONE)
    closed = close(project, opened, [finding("F1", "Major", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "accepted"


def test_at_standard_a_major_fix_in_a_design_round_needs_an_artifact(project: Path) -> None:
    settings(project, review_risk="standard")
    opened = open_round(project, "design", ONE)
    with pytest.raises(WorkspaceError, match="must name an artifact"):
        close(project, opened, [finding("F1", "Major", "FIX")], reports=report(ONE))


def test_at_low_a_blocker_still_forces_a_reaudit(project: Path) -> None:
    settings(project, review_risk="low")
    opened = open_round(project, "design", ONE)
    fixed = DESIGN_BODY.replace("Goal.", "Goal, fixed.")
    (project / "artifacts" / "architecture.md").write_text(fixed, encoding="utf-8")
    closed = close(project, opened, [finding("F1", "Blocker", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "reaudit-required"


@pytest.mark.parametrize("risk", ["standard", "high"])
def test_at_standard_and_high_a_major_in_design_forces_a_reaudit(project: Path, risk: str) -> None:
    settings(project, review_risk=risk)
    stages = LADDER if risk == "high" else ONE
    opened = open_round(project, "design", stages)
    fixed = DESIGN_BODY.replace("Goal.", "Goal, fixed.")
    (project / "artifacts" / "architecture.md").write_text(fixed, encoding="utf-8")
    closed = close(project, opened, [finding("F1", "Major", "FIX")], reports=report(stages))
    assert closed["outcome"] == "reaudit-required"


def test_the_risk_at_close_wins_over_the_risk_at_open(project: Path) -> None:
    settings(project, review_risk="standard")
    opened = open_round(project, "design", ONE)
    settings(project, review_risk="low")
    closed = close(project, opened, [finding("F1", "Major", "FIX")], reports=report(ONE))
    assert closed["outcome"] == "accepted"


def test_below_policy_three_the_risk_never_changes_the_outcome(tmp_path: Path, repository: Path) -> None:
    path = make_project(tmp_path, repository)
    record(path, add_worktree(repository))
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text(DESIGN_BODY, encoding="utf-8")
    opened = open_round(path, "design", ("comprehension", "combined"))
    with pytest.raises(WorkspaceError, match="must name an artifact"):
        close(path, opened, [finding("F1", "Major", "FIX")], reports=report(("comprehension", "combined")))


# --- the standing fallback -----------------------------------------------------------------------


STANDING = {"mode": "waive-recorded", "reason": "no fresh agents on this host"}


def test_a_standing_waiver_needs_no_confirmation_or_reason_for_a_document(project: Path) -> None:
    settings(project, review_fallback=STANDING)
    result = gold(project, operation="waive", target="spec")
    assert result["outcome"] == "waived"
    text = (project / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")
    assert "## Standing waiver" in text and "no fresh agents on this host" in text
    assert goldfish.standing_waivers(project, "spec") == [1]


def test_without_a_standing_fallback_a_waiver_still_needs_both(project: Path) -> None:
    refuses(project, "reason", operation="waive", target="spec")
    settings(project, review_fallback={"mode": "ask"})
    refuses(project, "reason", operation="waive", target="spec")
    refuses(project, "confirmation", operation="waive", target="spec", reason="no agent")


def test_a_standing_waiver_never_covers_design_or_diff(project: Path) -> None:
    settings(project, review_fallback=STANDING)
    refuses(project, "confirmation", operation="waive", target="design", reason="x")
    refuses(project, "confirmation", operation="waive", target="design")


def test_a_standing_waiver_is_ignored_at_high_but_a_fresh_confirmation_works(project: Path) -> None:
    settings(project, review_risk="high", review_fallback=STANDING)
    refuses(project, "reason", operation="waive", target="spec")
    result = gold(project, operation="waive", target="spec", reason="no agent today", confirmation=CONFIRMATION)
    assert result["outcome"] == "waived"
    assert goldfish.standing_waivers(project, "spec") == []


def test_a_standing_waiver_warns_until_a_review_passes(project: Path) -> None:
    settings(project, review_fallback=STANDING)
    gold(project, operation="waive", target="spec")
    assert any("standing waiver" in note for note in goldfish.goldfish_findings(project))
    passing(project, "spec")
    assert not any("standing waiver" in note for note in goldfish.goldfish_findings(project))


# --- amendments ----------------------------------------------------------------------------------


def test_an_amendment_reuses_the_reviewed_round_and_passes_the_gate(project: Path) -> None:
    passing(project, "spec")
    spec_edit(project)
    version = goldfish.version_for(project, load_state(project), "spec", None)
    assert goldfish.round_errors(project, "spec", None, version)  # an edit with no amendment needs a round
    result = amend(project, "spec", kind="reviewer-requested", reason="fixed wording a reviewer named")
    assert result["outcome"] == "waived" and result["reuses"] == 1
    assert goldfish.round_errors(project, "spec", None, version) == []
    text = (project / "reviews" / f"goldfish_{result['round']:02d}.md").read_text(encoding="utf-8")
    assert "## Amendment" in text and "reviewer-requested" in text and "fixed wording a reviewer named" in text
    assert goldfish.amendment_rounds(project, "spec") == [result["round"]]


def test_an_edit_with_no_matching_amendment_still_needs_a_round(project: Path) -> None:
    passing(project, "spec")
    spec_edit(project)
    amend(project, "spec")
    spec_edit(project, "Another change.\n")
    version = goldfish.version_for(project, load_state(project), "spec", None)
    assert goldfish.round_errors(project, "spec", None, version)


def test_amendments_chain_and_each_is_a_new_round(project: Path) -> None:
    passing(project, "planning")
    edit_planning(project, "One.\n")
    first = amend(project, "planning")
    edit_planning(project, "Two.\n")
    second = amend(project, "planning", reason="second typo")
    assert goldfish.amendment_rounds(project, "planning") == [first["round"], second["round"]]
    assert second["reuses"] == 1


def test_an_amendment_needs_a_passing_review_to_stand_on(project: Path) -> None:
    spec_edit(project)
    refuses(project, "passing", operation="amend", target="spec", kind="editorial", reason="x")


def test_a_waiver_alone_is_not_a_base_for_an_amendment(project: Path) -> None:
    gold(project, operation="waive", target="spec", reason="no agent", confirmation=CONFIRMATION)
    spec_edit(project)
    refuses(project, "passing", operation="amend", target="spec", kind="editorial", reason="x")


def test_nothing_to_amend_when_the_text_matches_the_reviewed_round(project: Path) -> None:
    passing(project, "spec")
    refuses(project, "nothing to amend", operation="amend", target="spec", kind="editorial", reason="x")


@pytest.mark.parametrize("target", ["design", "diff"])
def test_design_and_diff_cannot_be_amended(project: Path, target: str) -> None:
    refuses(project, "spec and planning", operation="amend", target=target, kind="editorial", reason="x")


def test_an_amendment_kind_is_closed_and_the_digest_is_never_supplied(project: Path) -> None:
    passing(project, "spec")
    spec_edit(project)
    refuses(project, "kind", operation="amend", target="spec", kind="cosmetic", reason="x")
    refuses(project, "unknown field", operation="amend", target="spec", kind="editorial", reason="x",
            version="a" * 64)


def test_an_amendment_is_refused_while_the_latest_round_is_not_passing(project: Path) -> None:
    passing(project, "spec")
    open_round(project, "spec", ONE)
    spec_edit(project)
    refuses(project, "passing", operation="amend", target="spec", kind="editorial", reason="x")


def test_amendments_are_listed_and_warned_until_a_review_passes(project: Path) -> None:
    passing(project, "spec")
    spec_edit(project)
    amend(project, "spec")
    assert any("amend" in note for note in goldfish.goldfish_findings(project))
    spec_edit(project, "More.\n")
    passing(project, "spec")
    assert not any("amend" in note for note in goldfish.goldfish_findings(project))


def test_the_amend_action_table_names_its_fields() -> None:
    schema = input_schema("goldfish")
    assert {"kind"} <= set(schema["allowed"])
    assert {"kind", "reason", "target"} <= set(schema["required_by"]["operation=amend"])


def test_policy_three_rounds_leave_the_marker_key_set_unchanged(project: Path) -> None:
    passing(project, "spec")
    spec_edit(project)
    amend(project, "spec")
    for item in rounds(project):
        marker = goldfish.render_marker(item)
        assert set(json.loads(marker.split(" ", 2)[2].rsplit(" -->", 1)[0])) == goldfish.MARKER_KEYS


def test_a_confirmation_lists_every_amendment_since_the_last_review(project: Path) -> None:
    passing(project, "spec")
    previous = goldfish.version_for(project, load_state(project), "spec", None)
    spec_edit(project)
    amend(project, "spec", kind="reviewer-requested", reason="a reviewer named the wording")
    details = goldfish.amendment_details(project)
    assert details == [{"round": 2, "target": "spec", "kind": "reviewer-requested",
                        "reason": "a reviewer named the wording", "previous": previous}]
    assert confirm(project, "requirements")["amendments"] == details


def test_a_confirmation_with_no_amendment_has_no_amendments_key(project: Path) -> None:
    passing(project, "spec")
    assert "amendments" not in confirm(project, "requirements")


def test_settings_refuses_below_policy_three_a_finished_project_and_an_empty_request() -> None:
    created = {"created": "2026-11-16T10:00:00+00:00", "status": "PLANNING"}
    with pytest.raises(WorkspaceError, match="needs policy_version 3, but this project is at policy 2"):
        _settings({**created, "policy_version": 2}, {"confirmation": USER, "review_risk": "low"})
    for status in ("DONE", "CANCELLED"):
        with pytest.raises(WorkspaceError, match=f"a {status} project keeps its settings"):
            _settings({**created, "policy_version": 3, "status": status}, {"confirmation": USER, "review_risk": "low"})
    with pytest.raises(WorkspaceError, match="at least one of"):
        _settings({**created, "policy_version": 3}, {"confirmation": USER})


def test_stored_settings_of_the_wrong_shape_are_findings(project: Path) -> None:
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    for field, value, fragment in (
        ("review_fallback", "ask", "review_fallback must be an object"),
        ("review_fallback", {"mode": "ask", "extra": 1}, "review_fallback must be an object"),
        ("options", 5, "options must be an object"),
        ("options", {"reports": "yes"}, "options.reports must be true or false"),
        ("review_risk", "medium", "review_risk must be one of"),
    ):
        errors = workspace_lib.validate_v4_state({**state, field: value}, project, check_files=False).errors
        assert any(fragment in text for text in errors), (field, value, errors)
