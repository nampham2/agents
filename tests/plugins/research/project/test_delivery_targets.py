"""Recording and changing the delivery target: alignment confirm, the gate, and retarget (T04)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_lib import WorkspaceError
from workspace_workflows import workflow

from tests.conftest import backdate_project
from tests.plugins.research.project.test_gate_new_projects import (
    PLANNING,
    TestConfirmationMarker,
    plan_patch,
)
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_workflow_automation import fill_spec, task

USER = {"source": "user reply in the test", "response": "Go with that target"}


@pytest.fixture(autouse=True)
def _policy_two_at_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lib, "NEW_PROJECT_POLICY", 2)
    monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(tmp_path / "workspace", title="Targets", working_directory=target, create_root=True)


@pytest.fixture
def drafted(project: Path) -> Path:
    fill_spec(project)
    (project / "planning.md").write_text(PLANNING, encoding="utf-8")
    return project


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))


def tokens(project: Path) -> dict[str, str]:
    return {
        "requirements_sha256": lib.document_sha256((project / "spec.md").read_text(encoding="utf-8")),
        "planning_sha256": lib.document_sha256((project / "planning.md").read_text(encoding="utf-8")),
    }


def confirm(project: Path, identity: str = "align-1", **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "kind": "alignment", **tokens(project), "review_id": "A1", "response": "Yes, go ahead",
        "source": "user reply", "scope": "requirements and design together",
    }
    fields.update(overrides)
    fields = {key: value for key, value in fields.items() if value is not None}
    return workflow(project, "confirm", request(project, identity, **fields))


def retarget(project: Path, identity: str, target: str, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "delivery_target": target, "reason": "the user changed the goal", "confirmation": dict(USER),
    }
    fields.update(overrides)
    return workflow(project, "retarget", request(project, identity, **fields))


def refusal(call: Any) -> str:
    with pytest.raises(Exception) as caught:  # WorkspaceError or the journal's OperationError
        call()
    return str(caught.value)


def leave(project: Path, status: str = "PLANNING") -> None:
    from workspace_session import update_project_data

    update_project_data(project, plan_patch(status), expected_revision=state_of(project)["revision"])


class TestAlignmentRecordsTheTarget:
    def test_the_confirmation_stores_the_target_and_says_so_in_the_history(self, drafted: Path) -> None:
        result = confirm(drafted, delivery_target="pr")
        state = state_of(drafted)
        assert state["delivery_target"] == "pr" and state["revision"] == 1
        assert result["delivery_target"] == "pr"
        history = (drafted / "spec.md").read_text(encoding="utf-8")
        assert "delivery_target: pr" in history and "Yes, go ahead" in history
        leave(drafted)
        assert state_of(drafted)["status"] == "PLANNING"

    def test_a_policy_two_alignment_without_a_target_is_refused_naming_the_values(self, drafted: Path) -> None:
        message = refusal(lambda: confirm(drafted))
        assert "research, local, pr, merged, release" in message
        assert "delivery_target" not in state_of(drafted) and state_of(drafted)["revision"] == 0

    def test_an_unknown_target_is_refused(self, drafted: Path) -> None:
        assert "must be one of" in refusal(lambda: confirm(drafted, delivery_target="ship-it"))

    def test_a_second_target_goes_through_retarget(self, drafted: Path) -> None:
        confirm(drafted, delivery_target="pr")
        message = refusal(lambda: confirm(drafted, "align-2", delivery_target="local"))
        assert "retarget" in message
        assert state_of(drafted)["delivery_target"] == "pr"

    def test_a_reconfirmation_may_leave_the_target_out_once_it_is_recorded(self, drafted: Path) -> None:
        confirm(drafted, delivery_target="pr")
        confirm(drafted, "align-2")
        assert state_of(drafted)["delivery_target"] == "pr"

    def test_only_alignment_takes_a_target(self, drafted: Path) -> None:
        planning = lib.document_sha256((drafted / "planning.md").read_text(encoding="utf-8"))
        message = refusal(lambda: workflow(drafted, "confirm", request(
            drafted, "plan-1", kind="planning", proposal_sha256=planning, review_id="A1", response="ok",
            source="user", scope="design", delivery_target="pr")))
        assert "only kind alignment takes delivery_target" in message

    def test_a_project_below_policy_two_refuses_a_target(self, drafted: Path) -> None:
        state = state_of(drafted)
        state.pop("policy_version")
        (drafted / "project.json").write_text(json.dumps(state), encoding="utf-8")
        backdate_project(drafted)
        assert "needs policy_version 2" in refusal(lambda: confirm(drafted, delivery_target="pr"))
        confirm(drafted)  # no target is needed or recorded
        assert "delivery_target" not in state_of(drafted)

    def test_the_decision_is_retry_safe(self, drafted: Path) -> None:
        payload = request(drafted, "align-1", kind="alignment", **tokens(drafted), review_id="A1", response="Yes",
                          source="user reply", scope="requirements and design", delivery_target="merged")
        first = workflow(drafted, "confirm", payload)
        again = workflow(drafted, "confirm", payload)
        assert first["complete"] and again["complete"]
        assert state_of(drafted)["revision"] == 1


class TestTheGateNeedsATarget:
    def separate_confirmations(self, project: Path) -> None:
        confirmer = TestConfirmationMarker()
        confirmer.confirm(project, "requirements", "R1", "confirm-r1")
        confirmer.confirm(project, "planning", "A1", "confirm-a1")

    def test_separate_confirmations_do_not_set_a_target_and_the_gate_refuses(self, drafted: Path) -> None:
        self.separate_confirmations(drafted)
        message = refusal(lambda: leave(drafted))
        assert "delivery_target" in message and "research, local, pr, merged, release" in message
        retarget(drafted, "set-1", "local")
        leave(drafted)
        assert state_of(drafted)["status"] == "PLANNING"

    def test_the_check_comes_before_the_legacy_return(self, drafted: Path) -> None:
        # An old `created` makes the project legacy for every other gate; policy 2 is still checked.
        backdate_project(drafted, "2019-01-01T00:00:00+00:00")
        state = state_of(drafted)
        state["policy_version"] = 2
        (drafted / "project.json").write_text(json.dumps(state), encoding="utf-8")
        errors = lib.alignment_gate_errors(drafted, state, {**state, "status": "PLANNING"})
        assert errors and "delivery_target" in errors[0]
        assert lib.alignment_gate_errors(drafted, state, {**state, "status": "PLANNING", "delivery_target": "pr"}) == []

    def test_other_moves_are_not_gated_by_the_target(self, drafted: Path) -> None:
        state = state_of(drafted)
        assert lib.alignment_gate_errors(drafted, state, {**state, "status": "CANCELLED"}) == []


class TestRetarget:
    @pytest.fixture
    def aligned(self, drafted: Path) -> Path:
        confirm(drafted, delivery_target="pr")
        return drafted

    def test_it_changes_the_target_and_reports_what_is_gained_and_dropped(self, aligned: Path) -> None:
        result = retarget(aligned, "re-1", "release")
        assert state_of(aligned)["delivery_target"] == "release"
        assert result["delivery_target"] == {"from": "pr", "to": "release"}
        assert any("release record" in item for item in result["gained"])
        assert any("merge record" in item for item in result["gained"])
        dropped = retarget(aligned, "re-2", "local")
        assert any("merge request" in item for item in dropped["dropped"])
        assert dropped["gained"] == []

    def test_it_records_the_users_words_and_the_diff_in_the_decision_history(self, aligned: Path) -> None:
        retarget(aligned, "re-1", "merged", confirmation={"source": "chat", "response": "make it merged"})
        history = (aligned / "spec.md").read_text(encoding="utf-8")
        assert "kind: retarget" in history and "from: pr" in history and "to: merged" in history
        assert "make it merged" in history and "gained:" in history and "dropped:" in history

    def test_it_works_from_an_unset_target_in_aligning(self, drafted: Path) -> None:
        result = retarget(drafted, "re-1", "research")
        assert result["delivery_target"] == {"from": None, "to": "research"}
        assert state_of(drafted)["delivery_target"] == "research"

    def test_it_refuses_the_same_target_and_a_bad_one(self, aligned: Path) -> None:
        assert "already pr" in refusal(lambda: retarget(aligned, "re-1", "pr"))
        assert "must be one of" in refusal(lambda: retarget(aligned, "re-2", "everything"))

    def test_it_needs_the_reason_and_the_users_words(self, aligned: Path) -> None:
        assert "reason" in refusal(lambda: workflow(aligned, "retarget", request(
            aligned, "re-1", delivery_target="local", confirmation=dict(USER))))
        assert "confirmation" in refusal(lambda: workflow(aligned, "retarget", request(
            aligned, "re-2", delivery_target="local", reason="why")))

    def test_it_refuses_below_policy_two(self, drafted: Path) -> None:
        state = state_of(drafted)
        state.pop("policy_version")
        (drafted / "project.json").write_text(json.dumps(state), encoding="utf-8")
        backdate_project(drafted)
        assert "needs policy_version 2" in refusal(lambda: retarget(drafted, "re-1", "pr"))

    def test_it_refuses_a_finished_project(self, aligned: Path) -> None:
        state = state_of(aligned)
        state["status"] = "CANCELLED"
        state["cancellation_reason"] = "dropped"
        (aligned / "project.json").write_text(json.dumps(state), encoding="utf-8")
        assert "keeps its delivery_target" in refusal(lambda: retarget(aligned, "re-1", "local"))

    def test_it_refuses_a_target_that_would_drop_a_recorded_pull_request(self, aligned: Path) -> None:
        state = state_of(aligned)
        state["worktrees"] = [{
            "path": state["working_directory"], "role": "target", "kind": "created", "status": "active",
            "repository": str(aligned), "branch": "feat/x", "base_commit": "a" * 40,
            "recorded_at": "2026-11-16T10:00:00+01:00", "closure": None,
            "confirmation": {"source": "test", "response": "yes"},
            "pull_request": {
                "host": "github", "url": "https://github.com/o/r/pull/1", "number": 1,
                "recorded_at": "2026-11-16T10:00:00+01:00",
            },
        }]
        (aligned / "project.json").write_text(json.dumps(state), encoding="utf-8")
        message = refusal(lambda: retarget(aligned, "re-1", "local"))
        assert "retarget would drop records" in message and "pull_request" in message
        assert state_of(aligned)["delivery_target"] == "pr"

    def test_research_refuses_a_worktree_and_a_running_target_task(self, aligned: Path) -> None:
        state = state_of(aligned)
        from workspace_session import update_project_data

        writing = task("T01")
        writing["outputs"] = [{"root": "target", "path": "src", "required": True}]
        update_project_data(aligned, {"tasks": [writing]}, expected_revision=state["revision"])
        assert "task T01 still writes to the target" in refusal(lambda: retarget(aligned, "re-1", "research"))

    @pytest.mark.parametrize(
        ("entry", "target", "named"),
        [
            ({"merge": {"state": "merged"}}, "pr", "merge record"),
            ({"version_line": {"release": "1.0.0"}}, "pr", "release record"),
            ({"kind": "created"}, "research", "created worktree"),
            ({"kind": "none"}, "research", None),
        ],
    )
    def test_each_dropped_record_is_named(self, entry: dict, target: str, named: str | None) -> None:
        from workspace_workflows import _retarget

        state = {"policy_version": 2, "delivery_target": "release", "status": "PLANNING", "tasks": [],
                 "worktrees": [{"path": "/w", **entry}]}
        call = {"delivery_target": target, "reason": "why", "confirmation": dict(USER)}
        if named is None:
            patch, _, _ = _retarget(state, call)
            assert patch == {"delivery_target": target}
            return
        with pytest.raises(WorkspaceError, match="retarget would drop records") as caught:
            _retarget(state, call)
        text = str(caught.value)
        assert "/w" in text and (named.split()[0] in text)

    def test_a_plain_update_still_cannot_change_the_target(self, aligned: Path) -> None:
        from workspace_session import update_project_data

        with pytest.raises(WorkspaceError, match="unsupported update fields"):
            update_project_data(aligned, {"delivery_target": "local"}, expected_revision=state_of(aligned)["revision"])

    def test_the_operation_is_retry_safe(self, aligned: Path) -> None:
        payload = request(aligned, "re-1", delivery_target="merged", reason="why", confirmation=dict(USER))
        workflow(aligned, "retarget", payload)
        again = workflow(aligned, "retarget", payload)
        assert again["complete"] and state_of(aligned)["delivery_target"] == "merged"
        assert state_of(aligned)["revision"] == 2
