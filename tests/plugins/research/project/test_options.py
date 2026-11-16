"""The optional policies of a policy-3 project: `style_advice`, `memory_staging` and `reports` (T05).

The defaults are style advice on, memory staging on and reports off. Turning one off changes only what its
name says; projects at policy 0 to 2 see no change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_checks import control_warnings
from workspace_journal import snapshot
from workspace_lib import WorkspaceError
from workspace_workflows import workflow

from tests.plugins.research.project.test_finalize_staging import LESSON_BODY, LESSON_REFLECTION
from tests.plugins.research.project.test_lifecycle_commands import done, planned, request
from tests.plugins.research.project.test_worktrees import load_state

USER = {"source": "user reply in the test", "response": "Use that option"}
COUNTER = iter(range(1, 10_000))
CHECK = {"task": "T01", "argv": [sys.executable, "-c", "print('checked')"]}
CODE_TASK = {
    "id": "T09", "name": "Code", "status": "DONE", "effect": {"kind": "local_write", "description": "edit"},
    "outputs": [{"root": "target", "path": "src/app.py", "required": True}],
}


def at_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: "int | None") -> Path:
    monkeypatch.setattr(lib, "NEW_PROJECT_POLICY", policy)
    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(tmp_path / "workspace", title="Options", working_directory=target, create_root=True)
    if policy in (2, 3):
        # A target of `local` lets the project leave ALIGNING; the write leaves the revision alone.
        stored = json.loads((created / "project.json").read_text(encoding="utf-8"))
        (created / "project.json").write_text(json.dumps({**stored, "delivery_target": "local"}), encoding="utf-8")
    return created


@pytest.fixture
def three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return at_policy(tmp_path, monkeypatch, 3)


def settings(project: Path, **fields: Any) -> dict[str, Any]:
    token = snapshot(project / "spec.md")[1]
    payload = {
        "id": f"o-{next(COUNTER)}", "expected_revision": load_state(project)["revision"],
        "tokens": {"spec": token}, "confirmation": USER, **fields,
    }
    return workflow(project, "settings", payload)


def report(project: Path) -> dict[str, Any]:
    return workflow(project, "report", request(project, f"r-{next(COUNTER)}"))


def finalize(project: Path, reflection: str) -> dict[str, Any]:
    payload = request(project, f"f-{next(COUNTER)}", reflection=reflection, continuation={"next": "none"})
    return workflow(project, "finalize", payload)


# --- the record: defaults and closed keys ---------------------------------------------------------


def test_the_defaults_are_style_on_staging_on_reports_off(three: Path) -> None:
    assert lib.project_options(load_state(three)) == {"style_advice": True, "memory_staging": True, "reports": False}


def test_options_records_each_key_and_keeps_the_others(three: Path) -> None:
    settings(three, options={"reports": True})
    assert lib.project_options(load_state(three)) == {"style_advice": True, "memory_staging": True, "reports": True}
    settings(three, options={"memory_staging": False})
    assert lib.project_options(load_state(three)) == {"style_advice": True, "memory_staging": False, "reports": True}
    settings(three, options=None)
    assert "options" not in load_state(three)


@pytest.mark.parametrize("key", ["delegating", "delegate", "style", "Reports"])
def test_an_unknown_key_such_as_delegating_is_rejected(three: Path, key: str) -> None:
    with pytest.raises(WorkspaceError, match="unknown key"):
        settings(three, options={key: True})
    assert "options" not in load_state(three)


def test_a_stored_unknown_key_is_a_validation_finding(three: Path) -> None:
    stored = json.loads((three / "project.json").read_text(encoding="utf-8"))
    errors = lib.validate_v4_state({**stored, "options": {"delegating": True}}, three, check_files=False).errors
    assert any("unknown key: delegating" in text for text in errors)


# --- reports: off by default at policy 3 ---------------------------------------------------------


def test_the_report_action_writes_nothing_while_reports_are_off(three: Path) -> None:
    planned(three)
    result = report(three)
    assert not (three / "artifacts" / "report.md").exists()
    assert "options.reports" in result["notice"] and "settings" in result["notice"]
    assert result["committed"] is False


def test_the_report_action_writes_once_reports_are_on(three: Path) -> None:
    planned(three)
    settings(three, options={"reports": True})
    result = report(three)
    assert (three / "artifacts" / "report.md").is_file()
    assert "notice" not in result


@pytest.mark.parametrize("policy", [None, 2])
def test_below_policy_three_the_report_action_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: "int | None"
) -> None:
    project = at_policy(tmp_path, monkeypatch, policy)
    planned(project)
    report(project)
    assert (project / "artifacts" / "report.md").is_file()


# --- memory staging: lessons staged at close, and the warning that follows ------------------------


def test_finalize_stages_lessons_by_default(three: Path) -> None:
    done(three)
    result = finalize(three, LESSON_REFLECTION)
    assert result["reflected_lessons"]["staged"]
    assert LESSON_BODY in (three / "memory-staging.md").read_text(encoding="utf-8")
    assert result["memory_staging_warnings"]


def test_finalize_stages_nothing_and_warns_of_nothing_when_staging_is_off(three: Path) -> None:
    done(three)
    settings(three, options={"memory_staging": False})
    before = (three / "memory-staging.md").read_text(encoding="utf-8")
    result = finalize(three, LESSON_REFLECTION)
    assert result["reflected_lessons"] == {"staged": [], "skipped": []}
    assert result["memory_staging_warnings"] == []
    assert (three / "memory-staging.md").read_text(encoding="utf-8") == before


def test_validate_close_is_silent_about_staged_lessons_when_staging_is_off(three: Path) -> None:
    done(three)
    (three / "memory-staging.md").write_text(
        (three / "memory-staging.md").read_text(encoding="utf-8") + "\n## A lesson\n\nStaged by hand.\n",
        encoding="utf-8",
    )
    assert lib.memory_staging_warnings(three)
    settings(three, options={"memory_staging": False})
    assert lib.memory_staging_warnings(three) == []
    assert not any("staged lessons" in text for text in lib.validate_project(three, close=True).warnings)


@pytest.mark.parametrize("policy", [None, 2])
def test_below_policy_three_staging_is_always_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: "int | None"
) -> None:
    project = at_policy(tmp_path, monkeypatch, policy)
    done(project)
    assert finalize(project, LESSON_REFLECTION)["reflected_lessons"]["staged"]


# --- the finalize control warning -----------------------------------------------------------------


def verified(project: Path) -> None:
    workflow(project, "verify", {"id": f"v-{next(COUNTER)}", "checks": [CHECK]})


def test_at_policy_three_a_project_with_no_code_task_gets_no_control_warning(three: Path) -> None:
    planned(three)
    verified(three)
    assert control_warnings(three, load_state(three)) == []


@pytest.mark.parametrize("status", ["RUNNING", "DONE"])
def test_at_policy_three_a_code_task_brings_the_control_warning_back(three: Path, status: str) -> None:
    planned(three)
    verified(three)
    state = load_state(three)
    state["tasks"].append({**CODE_TASK, "status": status})
    assert len(control_warnings(three, state)) == 1


def test_a_code_task_that_has_not_started_does_not_bring_it_back(three: Path) -> None:
    planned(three)
    verified(three)
    state = load_state(three)
    state["tasks"].append({**CODE_TASK, "status": "TODO"})
    assert control_warnings(three, state) == []


@pytest.mark.parametrize("policy", [None, 2])
def test_below_policy_three_the_control_warning_follows_todays_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: "int | None"
) -> None:
    project = at_policy(tmp_path, monkeypatch, policy)
    planned(project)
    verified(project)
    assert len(control_warnings(project, load_state(project))) == 1
    assert len(control_warnings(project)) == 1


def test_finalize_passes_the_state_so_a_policy_three_project_closes_quietly(three: Path) -> None:
    done(three)
    verified(three)
    assert finalize(three, LESSON_REFLECTION.replace("## Lessons", "## Notes"))["control_warnings"] == []
