"""Policy 3: a superset of policy 2 for delivery targets, with the Goldfish gates always on (T03)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_lib import (
    DELIVERY_GATES_FROM,
    GOLDFISH_FROM,
    POLICY_VERSIONS,
    STYLE_CHECKED_FROM,
    WorkspaceError,
    delivery_rules,
    has_target_rules,
    is_goldfish_gated,
    is_style_checked,
    project_options,
    validate_v4_state,
)
from workspace_operations import migration_plan
from workspace_workflows import _alignment_target, _retarget

USER = {"source": "user reply in the test", "response": "Go with that target"}
BEFORE = "2020-01-01T00:00:00+00:00"
AFTER = "2099-01-01T00:00:00+00:00"


def _state(**fields: Any) -> dict[str, Any]:
    return {"created": "2026-11-16T10:00:00+00:00", **fields}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Three", working_directory=target, create_root=True)


def _stored(project: Path, **changes: Any) -> dict[str, Any]:
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    state.update(changes)
    return state  # type: ignore[no-any-return]


def _errors(project: Path, **changes: Any) -> list[str]:
    return validate_v4_state(_stored(project, **changes), project, check_files=False).errors


# --- the policy value and the one predicate ----------------------------------------------------


def test_policy_three_is_a_valid_value() -> None:
    assert POLICY_VERSIONS == (0, 1, 2, 3)


@pytest.mark.parametrize(("policy", "expected"), [(0, False), (1, False), (2, True), (3, True)])
def test_has_target_rules_is_true_for_policy_two_and_three(policy: int, expected: bool) -> None:
    assert has_target_rules(policy) is expected


def test_a_new_project_starts_at_policy_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py sets the constant to None for the suite; the shipped value is read from the source.
    monkeypatch.undo()
    assert lib.NEW_PROJECT_POLICY == 3
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(tmp_path / "workspace", title="Fresh", working_directory=target, create_root=True)
    assert json.loads((created / "project.json").read_text(encoding="utf-8"))["policy_version"] == 3


@pytest.mark.parametrize("target", ["research", "local", "pr", "merged", "release"])
def test_policy_three_reads_the_same_target_rules_as_policy_two(target: str) -> None:
    two = delivery_rules(_state(policy_version=2, delivery_target=target))
    three = delivery_rules(_state(policy_version=3, delivery_target=target))
    assert three.policy == 3 and three.target == target
    assert (three.mr_required, three.release_demanded, three.diff_events, three.merge_required) == (
        two.mr_required, two.release_demanded, two.diff_events, two.merge_required,
    )
    assert three.forbids_target_writes is two.forbids_target_writes
    assert three.accepts_new_local_only is False


@pytest.mark.parametrize("target", [None, "bogus", "", 5])
def test_policy_three_without_a_valid_target_takes_policy_one_values(target: object) -> None:
    fields: dict[str, Any] = {} if target is None else {"delivery_target": target}
    rules = delivery_rules(_state(policy_version=3, **fields))
    one = delivery_rules(_state(policy_version=1))
    assert rules.policy == 3 and rules.target is None
    assert (rules.mr_required, rules.release_demanded, rules.diff_events) == (
        one.mr_required, one.release_demanded, one.diff_events,
    )


def test_an_unknown_policy_is_still_refused(project: Path) -> None:
    for bad in (4, -1, "3", 3.0, True):
        assert any("policy_version must be one of" in text for text in _errors(project, policy_version=bad))
    assert not any("policy_version must be one of" in text for text in _errors(project, policy_version=3))


# --- the target sites: each one that tested == 2 now accepts 3 ----------------------------------


def test_validation_accepts_a_target_at_policy_three(project: Path) -> None:
    errors = _errors(project, policy_version=3, delivery_target="merged")
    assert not any("delivery_target needs policy_version" in text for text in errors)


def test_validation_requires_a_target_past_aligning_at_policy_three(project: Path) -> None:
    missing = _errors(project, policy_version=3, status="PLANNING")
    assert any("needs a delivery_target" in text for text in missing)
    recorded = _errors(project, policy_version=3, status="PLANNING", delivery_target="pr")
    assert not any("needs a delivery_target" in text for text in recorded)


def test_a_policy_three_project_cannot_leave_aligning_without_a_target(project: Path) -> None:
    previous = _stored(project, policy_version=3)
    candidate = {**previous, "status": "PLANNING"}
    messages = lib.alignment_gate_errors(project, previous, candidate)
    assert any("delivery_target" in text for text in messages)


def test_the_alignment_confirmation_records_a_target_at_policy_three() -> None:
    state = _state(policy_version=3, status="ALIGNING")
    assert _alignment_target(state, "alignment", {"delivery_target": "merged"}) == "merged"
    with pytest.raises(WorkspaceError, match="delivery_target"):
        _alignment_target(state, "alignment", {})


def test_retarget_works_at_policy_three() -> None:
    state = _state(policy_version=3, status="PLANNING", delivery_target="pr")
    patch, _body, result = _retarget(state, {"delivery_target": "local", "reason": "changed", "confirmation": USER})
    assert patch["delivery_target"] == "local"
    assert result["delivery_target"] == {"from": "pr", "to": "local"}


def test_migrate_policy_refuses_policy_two_and_three_and_never_moves_down() -> None:
    for policy in (2, 3):
        state = {"schema_version": 4, "status": "PLANNING", "tasks": [], "created": "2026-11-16T10:00:00+00:00",
                 "policy_version": policy, "delivery_target": "pr"}
        with pytest.raises(WorkspaceError, match=f"already at policy {policy}"):
            migration_plan(state, "local")


# --- the Goldfish and style gates ---------------------------------------------------------------


def test_policy_three_is_goldfish_gated_whatever_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "GOLDFISH_FROM", "2099-01-01T00:00:00+00:00")
    assert is_goldfish_gated(_state(policy_version=3)) is True


@pytest.mark.parametrize("policy", [0, 1, 2])
def test_policies_zero_to_two_keep_the_goldfish_date(policy: int, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(policy_version=policy)
    monkeypatch.setattr(lib, "GOLDFISH_FROM", "2099-01-01T00:00:00+00:00")
    assert is_goldfish_gated(state) is False
    monkeypatch.setattr(lib, "GOLDFISH_FROM", "2020-01-01T00:00:00+00:00")
    assert is_goldfish_gated(state) is True


def test_a_project_with_no_policy_keeps_the_goldfish_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "GOLDFISH_FROM", "2099-01-01T00:00:00+00:00")
    assert is_goldfish_gated(_state()) is False
    monkeypatch.setattr(lib, "GOLDFISH_FROM", "2020-01-01T00:00:00+00:00")
    assert is_goldfish_gated(_state()) is True


def test_the_shipped_goldfish_instant_is_unchanged() -> None:
    assert GOLDFISH_FROM == "2026-10-27T12:13:54+01:00"
    assert STYLE_CHECKED_FROM == "2026-10-05T10:40:00+00:00"
    assert DELIVERY_GATES_FROM == "2026-10-05T14:56:34+00:00"


@pytest.mark.parametrize("policy", [0, 1, 2])
def test_policies_zero_to_two_keep_the_style_date(policy: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "2099-01-01T00:00:00+00:00")
    assert is_style_checked(_state(policy_version=policy)) is False
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "2020-01-01T00:00:00+00:00")
    assert is_style_checked(_state(policy_version=policy)) is True


def test_policy_three_style_follows_the_option_not_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "STYLE_CHECKED_FROM", "2099-01-01T00:00:00+00:00")
    assert is_style_checked(_state(policy_version=3)) is True
    assert is_style_checked(_state(policy_version=3, options={"style_advice": False})) is False


def test_project_options_merges_the_defaults() -> None:
    assert project_options(_state(policy_version=3)) == {"style_advice": True, "memory_staging": True, "reports": False}
    merged = project_options(_state(policy_version=3, options={"reports": True}))
    assert merged == {"style_advice": True, "memory_staging": True, "reports": True}
    assert project_options({}) == {"style_advice": True, "memory_staging": True, "reports": False}
    assert project_options({"options": "nonsense"}) == {"style_advice": True, "memory_staging": True, "reports": False}
