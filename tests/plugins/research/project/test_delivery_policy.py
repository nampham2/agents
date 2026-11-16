"""delivery_rules, policy_version and delivery_target: derivation, schema, guards (T04)."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib
from workspace_lib import (
    DELIVERY_GATES_FROM,
    GUARDED_WRITERS,
    DeliveryRules,
    WorkspaceError,
    allocate_project,
    check_state_candidate,
    delivery_rules,
    merge_demanded,
    removal_merge,
    validate_v4_state,
)
from workspace_session import UPDATE_FIELDS, prepare_update

BEFORE = "2026-09-01T00:00:00+00:00"
AFTER = "2026-11-01T00:00:00+00:00"
PR_ENTRY = {"pull_request": {"url": "https://example.test/pr/1"}}
LOCAL_ONLY = {"pr_exemption": {"reason": "local_only"}}
NO_REMOTE = {"pr_exemption": {"reason": "no_remote"}}


@pytest.fixture(autouse=True)
def _real_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/conftest.py moves the cutoff to 2099; these tests need the shipped one."""
    monkeypatch.setattr(workspace_lib, "DELIVERY_GATES_FROM", DELIVERY_GATES_FROM)


@pytest.fixture(autouse=True)
def _policy_two_at_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "NEW_PROJECT_POLICY", 2)


def _state(created: str = AFTER, **fields: Any) -> dict:
    return {"created": created, **fields}


def test_absent_policy_derives_from_created_with_todays_cutoff() -> None:
    assert delivery_rules(_state(BEFORE)).policy == 0
    assert delivery_rules(_state(AFTER)).policy == 1
    assert delivery_rules(_state(DELIVERY_GATES_FROM)).policy == 1
    assert delivery_rules({"created": "not a date"}).policy == 0
    assert delivery_rules({}).policy == 0


def test_explicit_policy_wins_over_created() -> None:
    assert delivery_rules(_state(BEFORE, policy_version=1)).policy == 1
    assert delivery_rules(_state(AFTER, policy_version=0)).policy == 0


@pytest.mark.parametrize("bad", [True, False, 4, -1, "1", 1.0, None])
def test_invalid_policy_takes_policy_one_values_never_zero(bad: object) -> None:
    rules = delivery_rules(_state(BEFORE, policy_version=bad))
    assert rules == delivery_rules(_state(AFTER, policy_version=1))


def test_policy_zero_and_one_values() -> None:
    zero = delivery_rules(_state(BEFORE))
    assert zero.warn_unrecorded_worktree and zero.warn_unrecorded_merge
    assert zero.diff_events == frozenset({"closure_exempt", "merge", "release"})
    assert not zero.mr_required and not zero.release_demanded and zero.target is None
    one = delivery_rules(_state(AFTER))
    assert one.mr_required and one.release_demanded
    assert one.diff_events == zero.diff_events
    assert one.warn_unrecorded_worktree and one.warn_unrecorded_merge


MATRIX = {
    # target: (warn worktree, mr_required, release_demanded, warn merge, diff_events)
    "research": (False, False, False, False, frozenset()),
    "local": (True, False, False, False, frozenset()),
    "pr": (True, True, False, False, frozenset({"closure"})),
    "merged": (True, True, False, True, frozenset({"merge", "closure_exempt"})),
    "release": (True, True, True, True, frozenset({"merge", "release", "closure_exempt"})),
}


@pytest.mark.parametrize("target", MATRIX)
def test_policy_two_matrix(target: str) -> None:
    rules = delivery_rules(_state(policy_version=2, delivery_target=target))
    warn_worktree, mr, release, warn_merge, events = MATRIX[target]
    assert (rules.policy, rules.target) == (2, target)
    assert rules.warn_unrecorded_worktree is warn_worktree
    assert rules.mr_required is mr
    assert rules.release_demanded is release
    assert rules.warn_unrecorded_merge is warn_merge
    assert rules.diff_events == events
    assert rules.forbids_target_writes is (target == "research")
    assert rules.accepts_new_local_only is False


@pytest.mark.parametrize("target", [None, "bogus", "", 5])
def test_policy_two_without_a_valid_target_behaves_as_policy_one(target: object) -> None:
    fields: dict[str, Any] = {} if target is None else {"delivery_target": target}
    rules = delivery_rules(_state(policy_version=2, **fields))
    one = delivery_rules(_state(AFTER, policy_version=1))
    assert rules.policy == 2 and rules.target is None
    assert rules.mr_required == one.mr_required
    assert rules.release_demanded == one.release_demanded
    assert rules.diff_events == one.diff_events
    assert rules.forbids_target_writes is False
    assert rules.accepts_new_local_only is False


def test_rules_are_frozen() -> None:
    rules = delivery_rules(_state())
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(rules, "policy", 9)  # noqa: B010  (a plain assignment is a type error, the point of the test)
    assert isinstance(delivery_rules(_state()), DeliveryRules)


def _rules(target: str | None, policy: int = 2) -> DeliveryRules:
    fields = {} if target is None else {"delivery_target": target}
    return delivery_rules(_state(policy_version=policy, **fields))


@pytest.mark.parametrize(
    ("rules", "entry", "demanded"),
    [
        (_rules(None, 0), PR_ENTRY, False),
        (_rules(None, 1), PR_ENTRY, True),
        (_rules(None, 1), LOCAL_ONLY, True),
        (_rules(None, 1), NO_REMOTE, False),
        (_rules("merged"), PR_ENTRY, True),
        (_rules("release"), LOCAL_ONLY, True),
        (_rules("release"), NO_REMOTE, False),
        (_rules("pr"), PR_ENTRY, False),
        (_rules("local"), PR_ENTRY, False),
        (_rules("research"), PR_ENTRY, False),
    ],
)
def test_merge_demanded(rules: DeliveryRules, entry: dict, demanded: bool) -> None:
    assert merge_demanded(entry, rules) is demanded


@pytest.mark.parametrize(
    ("rules", "entry", "kind"),
    [
        (_rules("merged"), PR_ENTRY, "merged"),
        (_rules("release"), LOCAL_ONLY, "decision"),
        (_rules(None, 1), PR_ENTRY, "merged"),
        (_rules(None, 0), LOCAL_ONLY, "decision"),
        (_rules("pr"), PR_ENTRY, "none"),
        (_rules("local"), PR_ENTRY, "none"),
        (_rules("research"), LOCAL_ONLY, "none"),
        (_rules("merged"), NO_REMOTE, "none"),
        (_rules("merged"), "not an entry", "none"),
    ],
)
def test_removal_merge(rules: DeliveryRules, entry: object, kind: str) -> None:
    assert removal_merge(entry, rules) == kind


def _new_project(tmp_path: Path) -> tuple[Path, dict]:
    workspace, target = tmp_path / "workspace", tmp_path / "target"
    workspace.mkdir()
    target.mkdir()
    project_dir = allocate_project(workspace, title="Policy project", working_directory=target)
    return project_dir, json.loads((project_dir / "project.json").read_text(encoding="utf-8"))


def test_new_project_starts_at_policy_two_without_a_target(tmp_path: Path) -> None:
    _, state = _new_project(tmp_path)
    assert state["policy_version"] == 2
    assert "delivery_target" not in state


def _errors(state: dict, project_dir: Path) -> list[str]:
    return validate_v4_state(state, project_dir, check_files=False).errors


def test_validation_accepts_a_fresh_project_and_each_target(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    assert _errors(state, project_dir) == []
    for target in MATRIX:
        assert _errors({**state, "delivery_target": target}, project_dir) == []


def test_validation_rejects_bad_values(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    for bad in (True, 4, "2"):
        assert any("policy_version" in e for e in _errors({**state, "policy_version": bad}, project_dir))
    assert any("delivery_target" in e for e in _errors({**state, "delivery_target": "bogus"}, project_dir))


def test_target_is_refused_under_policy_zero_and_one(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    for policy in (0, 1):
        errors = _errors({**state, "policy_version": policy, "delivery_target": "pr"}, project_dir)
        assert any("delivery_target" in e and "policy" in e for e in errors)
    absent = {key: value for key, value in state.items() if key != "policy_version"}
    assert any("delivery_target" in e for e in _errors({**absent, "delivery_target": "pr"}, project_dir))


def test_policy_two_beyond_aligning_needs_a_target(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    errors = _errors({**state, "status": "PLANNING"}, project_dir)
    assert any("delivery_target" in e for e in errors)
    aligned = {**state, "status": "PLANNING", "delivery_target": "pr"}
    assert not any("delivery_target" in e for e in _errors(aligned, project_dir))


def test_a_cancelled_policy_two_project_may_have_no_target(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    errors = _errors({**state, "status": "CANCELLED", "cancellation_reason": "dropped"}, project_dir)
    assert not any("delivery_target" in e for e in errors)


def test_plain_update_cannot_name_either_field(tmp_path: Path) -> None:
    _, state = _new_project(tmp_path)
    assert "policy_version" not in UPDATE_FIELDS and "delivery_target" not in UPDATE_FIELDS
    for patch in ({"policy_version": 1}, {"delivery_target": "pr"}):
        with pytest.raises(WorkspaceError, match="unsupported update fields"):
            prepare_update(state, patch)


def test_internal_update_may_carry_the_fields(tmp_path: Path) -> None:
    _, state = _new_project(tmp_path)
    assert prepare_update(state, {"delivery_target": "pr"}, internal=True)["delivery_target"] == "pr"


def test_guarded_writers_are_a_code_constant() -> None:
    assert GUARDED_WRITERS == {
        "confirm": {"delivery_target"},
        "retarget": {"delivery_target"},
        "migrate_policy": {"policy_version", "delivery_target"},
        "settings": {"review_risk", "review_fallback", "options"},
    }


@pytest.mark.parametrize("field", ["policy_version", "delivery_target"])
def test_candidate_cannot_change_a_guarded_field_without_its_writer(tmp_path: Path, field: str) -> None:
    project_dir, state = _new_project(tmp_path)
    candidate = copy.deepcopy(state)
    candidate[field] = 1 if field == "policy_version" else "pr"
    candidate["revision"] = state["revision"]
    for action in (None, "tag", "worktree", "unknown"):
        report = check_state_candidate(project_dir, candidate, expected_revision=state["revision"], check_files=False,
                                       action=action)
        assert any(f"guarded field: {field}" in e for e in report.errors), (action, report.errors)


def test_the_named_writer_may_change_its_own_fields_only(tmp_path: Path) -> None:
    project_dir, state = _new_project(tmp_path)
    candidate = copy.deepcopy(state)
    candidate["delivery_target"] = "pr"

    def report(action: str, cand: dict) -> list[str]:
        return [e for e in check_state_candidate(project_dir, cand, expected_revision=state["revision"],
                                                 check_files=False, action=action).errors if "guarded" in e]

    assert report("confirm", candidate) == []
    assert report("retarget", candidate) == []
    assert report("migrate_policy", candidate) == []
    candidate["policy_version"] = 1
    assert any("policy_version" in e for e in report("confirm", candidate))
    assert any("policy_version" in e for e in report("retarget", candidate))
    assert report("migrate_policy", candidate) == []


def test_policy_fields_are_absent_from_a_fresh_v3_shape(tmp_path: Path) -> None:
    from workspace_lib import PROJECT_FIELDS, PROJECT_V4_FIELDS

    assert {"policy_version", "delivery_target"} <= PROJECT_V4_FIELDS
    assert not {"policy_version", "delivery_target"} & PROJECT_FIELDS
