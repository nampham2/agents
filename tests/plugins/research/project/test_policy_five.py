"""Policy 5: policy 4's rules plus the optional packet reference fields `spec_refs` and `decision_refs` (T03)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_documents import append_record
from workspace_lib import (
    ASSIGNMENT_DEFAULTS,
    ASSIGNMENT_FIELDS,
    POLICY_VERSIONS,
    TASK_PACKET_FIELDS,
    TASK_V4_FIELDS,
    WorkspaceError,
    stamp_transitions,
    task_durable_enabled,
    task_packets_enabled,
    validate_v4_state,
)
from workspace_session import _load_state, prepare_update

from tests.plugins.research.project.test_durability_effects import make_running


def _rewrite(project: Path, policy: int) -> None:
    """Move a freshly built project to `policy`, with the durable keys a policy 4 or 5 task carries."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["policy_version"] = policy
    state["delivery_target"] = "local"
    if policy >= 4:
        for task in state["tasks"]:
            task["assignment_counter"] = 1 if task["status"] == "RUNNING" else 0
            task["block_generation"] = 1 if task["status"] == "BLOCKED" else 0
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _project(tmp_path: Path, policy: int) -> Path:
    project = make_running(tmp_path)
    _rewrite(project, policy)
    return project


def _errors(project: Path, state: dict[str, Any]) -> list[str]:
    return validate_v4_state(state, project, check_files=False).errors


# --- the policy value and the predicate ---------------------------------------------------------


def test_policy_five_is_valid_and_new_projects_start_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert 5 in POLICY_VERSIONS
    # tests/conftest.py sets the constant to None for the suite; the shipped value is read from the source.
    monkeypatch.undo()
    assert lib.NEW_PROJECT_POLICY == 5
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(tmp_path / "workspace", title="Fresh", working_directory=target, create_root=True)
    assert json.loads((created / "project.json").read_text(encoding="utf-8"))["policy_version"] == 5


def test_task_packets_enabled_only_at_a_valid_policy_five() -> None:
    assert task_packets_enabled({"policy_version": 5}) is True
    assert not any(task_packets_enabled({"policy_version": p}) for p in (0, 1, 2, 3, 4, 6, True, "5", None))
    assert task_packets_enabled({}) is False
    assert task_packets_enabled([]) is False  # ty: ignore[invalid-argument-type]


def test_policy_five_keeps_the_durable_fields() -> None:
    assert task_durable_enabled({"policy_version": 5}) is True


def test_a_policy_above_five_is_not_a_valid_value(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    state = _load_state(project)
    state["policy_version"] = 6  # what a tool older than this one sees in a newer project
    assert any("policy_version must be one of 0, 1, 2, 3, 4, 5" in e for e in _errors(project, state))


# --- the fields ----------------------------------------------------------------------------------


def test_the_packet_fields_are_task_and_assignment_fields() -> None:
    assert TASK_PACKET_FIELDS == ("spec_refs", "decision_refs")
    assert set(TASK_PACKET_FIELDS) <= TASK_V4_FIELDS
    assert set(TASK_PACKET_FIELDS) <= set(ASSIGNMENT_FIELDS)
    assert all(ASSIGNMENT_DEFAULTS[name] == [] for name in TASK_PACKET_FIELDS)


@pytest.mark.parametrize("name", TASK_PACKET_FIELDS)
def test_a_policy_five_task_may_carry_the_fields(tmp_path: Path, name: str) -> None:
    project = _project(tmp_path, 5)
    state = _load_state(project)
    state["tasks"][0][name] = ["Objective and audience", "Scope"]
    assert _errors(project, state) == []


@pytest.mark.parametrize("name", TASK_PACKET_FIELDS)
@pytest.mark.parametrize("bad", ["Scope", 3, None, {"a": "b"}, [""], ["ok", 4], [None]])
def test_a_malformed_value_is_a_finding_not_an_exception(tmp_path: Path, name: str, bad: object) -> None:
    project = _project(tmp_path, 5)
    state = _load_state(project)
    state["tasks"][0][name] = bad
    assert any(f"{name} must be a list of non-empty strings" in e for e in _errors(project, state))


@pytest.mark.parametrize("policy", [2, 3, 4])
@pytest.mark.parametrize("name", TASK_PACKET_FIELDS)
def test_the_fields_are_errors_below_policy_five(tmp_path: Path, policy: int, name: str) -> None:
    project = _project(tmp_path, policy)
    state = _load_state(project)
    state["tasks"][0][name] = ["Scope"]
    assert any(f"{name} needs policy_version 5" in e for e in _errors(project, state))


def test_a_task_without_the_fields_is_valid_at_every_policy(tmp_path: Path) -> None:
    for policy in (3, 4, 5):
        (tmp_path / str(policy)).mkdir()
        project = _project(tmp_path / str(policy), policy)
        assert _errors(project, _load_state(project)) == []


def test_an_update_patch_with_the_fields_is_refused_below_policy_five(tmp_path: Path) -> None:
    project = _project(tmp_path, 4)
    state = _load_state(project)
    candidate = prepare_update(state, {"tasks": [{"id": "T01", "spec_refs": ["Scope"]}]})
    with pytest.raises(WorkspaceError, match="spec_refs needs policy_version 5"):
        lib.commit_state(project, candidate, expected_revision=state["revision"])


def test_an_update_patch_with_the_fields_is_accepted_at_policy_five(tmp_path: Path) -> None:
    project = _project(tmp_path, 5)
    append_record(project, "decision", "A decision.", entry_id="q1")  # references must resolve when written
    state = _load_state(project)
    patch = {"tasks": [{"id": "T01", "spec_refs": ["Objective and audience"], "decision_refs": ["q1"]}]}
    lib.commit_state(project, prepare_update(state, patch), expected_revision=state["revision"])
    task = _load_state(project)["tasks"][0]
    assert task["spec_refs"] == ["Objective and audience"] and task["decision_refs"] == ["q1"]


# --- the assignment counter --------------------------------------------------------------------


@pytest.mark.parametrize("name", TASK_PACKET_FIELDS)
def test_editing_a_reference_bumps_the_counter(tmp_path: Path, name: str) -> None:
    project = _project(tmp_path, 5)
    before = _load_state(project)["tasks"][0]
    after = {**copy.deepcopy(before), name: ["Scope"]}
    assert stamp_transitions(before, after)["assignment_counter"] == before["assignment_counter"] + 1


@pytest.mark.parametrize("name", TASK_PACKET_FIELDS)
def test_an_empty_list_on_a_task_without_the_field_is_not_an_edit(tmp_path: Path, name: str) -> None:
    project = _project(tmp_path, 5)
    before = _load_state(project)["tasks"][0]
    after = {**copy.deepcopy(before), name: []}
    assert stamp_transitions(before, after)["assignment_counter"] == before["assignment_counter"]
