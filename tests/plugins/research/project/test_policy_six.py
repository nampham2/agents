"""Policy 6: the `coordinator` and `automated` options, and `delegating` kept at policy 5 only (T04, C1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_lib import POLICY_VERSIONS, WorkspaceError, task_durable_enabled, task_packets_enabled
from workspace_packets import build_packet

from tests.plugins.research.project.test_options import at_policy, settings
from tests.plugins.research.project.test_packets import HEADING, _set
from tests.plugins.research.project.test_packets import _project as _packet_project
from tests.plugins.research.project.test_worktrees import load_state

OLD_KEYS = ("style_advice", "memory_staging", "reports")
CROSS = "options.automated needs options.coordinator"


@pytest.fixture
def six(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return at_policy(tmp_path, monkeypatch, 6)


def _stored(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _errors(project: Path, **changes: Any) -> list[str]:
    return lib.validate_v4_state({**_stored(project), **changes}, project, check_files=False).errors


def _store_options(project: Path, options: dict[str, Any]) -> None:
    """Write `options` straight into project.json, so a test can hold a state `settings` would refuse."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["options"] = options
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _files(project: Path) -> dict[str, bytes]:
    """Every file of the project directory with its bytes: a refused call must leave all of them alone."""
    return {str(path.relative_to(project)): path.read_bytes() for path in sorted(project.rglob("*")) if path.is_file()}


# --- the new-project policy and the defaults --------------------------------------------------------


def test_a_new_project_starts_at_policy_six_with_coordinator_on_and_automated_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert 6 in POLICY_VERSIONS
    # tests/conftest.py sets the constant to None for the suite; the shipped value is read from the source.
    monkeypatch.undo()
    assert lib.NEW_PROJECT_POLICY == 6
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(tmp_path / "workspace", title="Fresh", working_directory=target, create_root=True)
    state = _stored(created)
    assert state["policy_version"] == 6 and "options" not in state
    assert lib.option_keys(state) == (*OLD_KEYS, "coordinator", "automated")
    assert lib.project_options(state) == {
        "style_advice": True, "memory_staging": True, "reports": False, "coordinator": True, "automated": False,
    }
    assert lib.modes_enabled(state) is True
    assert lib.coordinator_enabled(state) is True
    assert lib.automated_enabled(state) is False
    assert lib.packet_warnings_enabled(state) is True


def test_policy_six_keeps_the_durable_and_the_packet_fields() -> None:
    assert task_durable_enabled({"policy_version": 6}) is True
    assert task_packets_enabled({"policy_version": 6}) is True


def test_the_predicates_tolerate_malformed_state_and_older_policies() -> None:
    not_a_state: Any = []
    for state in (not_a_state, {}, {"policy_version": 7}, {"policy_version": "6"}, {"policy_version": 5}):
        assert lib.modes_enabled(state) is False
        assert lib.coordinator_enabled(state) is False
        assert lib.automated_enabled(state) is False
    assert lib.packet_warnings_enabled(not_a_state) is False
    assert lib.automated_enabled({"policy_version": 6, "options": {"automated": True}}) is True
    assert lib.coordinator_enabled({"policy_version": 6, "options": {"coordinator": False}}) is False
    assert lib.coordinator_enabled({"policy_version": 6, "options": {"coordinator": "no"}}) is True


# --- which options each policy has ------------------------------------------------------------------


def test_unlisted_option_keys_are_returned_at_every_policy() -> None:
    not_a_state: Any = "not a state"
    for state in (not_a_state, {}, {"policy_version": 7}, {"policy_version": "5"}):
        assert lib.option_keys(state) == OLD_KEYS
    assert [lib.option_keys({"policy_version": p}) for p in POLICY_VERSIONS] == [
        *[OLD_KEYS] * 5, (*OLD_KEYS, "delegating"), (*OLD_KEYS, "coordinator", "automated"),
    ]


def test_policy_six_refuses_delegating(six: Path) -> None:
    message = "options.delegating ends at policy_version 5; this project is at policy_version 6"
    before = _files(six)
    with pytest.raises(WorkspaceError, match=message):
        settings(six, options={"delegating": True})
    assert _files(six) == before
    assert any(message in text for text in _errors(six, options={"delegating": False}))
    assert "delegating" not in lib.project_options({**_stored(six), "options": {"delegating": True}})


def test_policy_five_still_accepts_delegating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    five = at_policy(tmp_path, monkeypatch, 5)
    settings(five, options={"delegating": True})
    state = load_state(five)
    assert lib.project_options(state)["delegating"] is True and lib.packet_warnings_enabled(state) is True
    assert lib.modes_enabled(state) is False and lib.coordinator_enabled(state) is False
    for key in ("coordinator", "automated"):
        with pytest.raises(WorkspaceError, match=rf"options\.{key} needs policy_version 6"):
            settings(five, options={key: True})
    settings(five, options={"delegating": False})
    assert lib.packet_warnings_enabled(load_state(five)) is False


def test_an_invalid_policy_names_the_first_policy_of_a_listed_key() -> None:
    assert lib.options_errors({"coordinator": True}, {"policy_version": "6"}) == [
        "options.coordinator needs policy_version 6"
    ]
    not_a_state: Any = None
    assert lib.options_errors({"delegating": True}, not_a_state) == ["options.delegating needs policy_version 5"]


# --- the cross rule: automated needs coordinator ----------------------------------------------------


def test_automated_turns_on_over_the_default_coordinator(six: Path) -> None:
    settings(six, options={"automated": True})
    state = load_state(six)
    assert lib.automated_enabled(state) is True and lib.coordinator_enabled(state) is True


@pytest.mark.parametrize(
    ("stored", "requested"),
    [
        ({"automated": True}, {"coordinator": False}),
        (None, {"automated": True, "coordinator": False}),
        ({"coordinator": False}, {"automated": True}),
    ],
    ids=["coordinator-off-while-automated-on", "both-in-one-call", "automated-on-while-coordinator-off"],
)
def test_settings_refuses_automated_on_with_coordinator_off(
    six: Path, stored: "dict[str, bool] | None", requested: dict[str, bool]
) -> None:
    if stored is not None:
        settings(six, options=stored)
    before = load_state(six)
    with pytest.raises(WorkspaceError, match=CROSS):
        settings(six, options=requested)
    assert load_state(six) == before


def test_one_settings_call_turns_both_modes_off(six: Path) -> None:
    settings(six, options={"automated": True})
    settings(six, options={"coordinator": False, "automated": False})
    state = load_state(six)
    assert lib.coordinator_enabled(state) is False and lib.automated_enabled(state) is False
    settings(six, options=None)
    state = load_state(six)
    assert "options" not in state
    assert lib.coordinator_enabled(state) is True and lib.automated_enabled(state) is False


def test_a_refused_settings_call_writes_no_journal_and_no_decision_line(six: Path) -> None:
    settings(six, options={"automated": True})
    before = _files(six)
    with pytest.raises(WorkspaceError, match=CROSS):
        settings(six, options={"coordinator": False})
    assert _files(six) == before


def test_a_stored_automated_without_coordinator_is_a_validation_finding(six: Path) -> None:
    errors = _errors(six, options={"automated": True, "coordinator": False})
    assert (
        "project: options.automated needs options.coordinator: automated is on and coordinator is off; "
        "turn automated off too (one settings call may turn both off)"
    ) in errors
    assert not any(CROSS in text for text in _errors(six, options={"automated": False, "coordinator": False}))
    assert not any(CROSS in text for text in _errors(six, options={"automated": True}))


# --- the packet warnings follow coordinator ---------------------------------------------------------


def test_packet_warnings_follow_coordinator_at_policy_six(tmp_path: Path) -> None:
    project = _packet_project(tmp_path, 6)
    _set(project, "T02", spec_refs=[HEADING], depends_on=["T01"])  # T01 is RUNNING
    state = _stored(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == ["dependency T01 is RUNNING, not DONE"]
    _store_options(project, {"coordinator": False})
    state = _stored(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == []
    _store_options(project, {"coordinator": False, "delegating": True})  # retired at 6: ignored
    state = _stored(project)
    assert build_packet(project, state, state["tasks"][1])["warnings"] == []
