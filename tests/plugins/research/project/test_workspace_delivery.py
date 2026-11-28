"""c5: a `pull_request` record replaces a `local_only` exemption when the request says `replaces_exemption`."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import workspace_delivery
from workspace_context import worktree_registered
from workspace_delivery import delivery_errors, required_for
from workspace_fields import EXEMPTION_REPLACED_FIELDS, WORKTREE_OPTIONAL_FIELDS
from workspace_lib import WorkspaceError
from workspace_workflows import WORKTREE_REQUIRED_BY

from tests.plugins.research.project.test_delivery import URL, entry_of, fetched, op, refuse
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    close,
    entry,
    git,
    load_state,
    make_project,
    make_repository,
    record,
    refused,
    run,
)

PULL_REQUEST = {"host": "github", "url": URL, "number": 7}
STAMP = "2026-01-02T10:00:00+00:00"
REPLACED = {
    "reason": "local_only", "source": "user", "response": "no push to github",
    "replaced_at": STAMP, "replaced_source": "user reply", "replaced_response": "approve",
}


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


@pytest.fixture
def recorded(tmp_path: Path, repository: Path, linked: Path) -> Path:
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    fetched(repository)
    project = make_project(tmp_path, repository)
    record(project, linked)
    return project


def at_policy(project: Path, policy: int) -> None:
    """Set the policy in the state file, as the delivery scenarios do, to pick the rules under test."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["policy_version"] = policy
    if policy >= 2:
        state["delivery_target"] = "pr"
    path.write_text(json.dumps(state), encoding="utf-8")


def hold(project: Path, linked: Path, reason: str) -> None:
    """Give the worktree an exemption of `reason` without going through the tool."""
    if reason == "local_only":
        op(project, linked, "pull_request", exempt="local_only")
        return
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["worktrees"][0]["pr_exemption"] = {"reason": reason, "source": "user", "response": "none"}
    path.write_text(json.dumps(state), encoding="utf-8")


class TestReplacesExemption:
    @pytest.mark.parametrize("policy", [0, 1])
    def test_a_pull_request_replaces_a_local_only_exemption(self, recorded: Path, linked: Path, policy: int) -> None:
        hold(recorded, linked, "local_only")
        at_policy(recorded, policy)
        op(recorded, linked, "pull_request", replaces_exemption=True, **PULL_REQUEST)
        item = entry_of(recorded)
        assert "pr_exemption" not in item
        assert (item["pull_request"]["host"], item["pull_request"]["number"]) == ("github", 7)
        replaced = item["pr_exemption_replaced"]
        assert set(replaced) == set(EXEMPTION_REPLACED_FIELDS)
        assert (replaced["reason"], replaced["source"], replaced["response"]) == ("local_only", "user reply", "approve")
        assert (replaced["replaced_source"], replaced["replaced_response"]) == ("user reply", "approve")
        assert replaced["replaced_at"] == item["pull_request"]["recorded_at"]
        assert delivery_errors(item, "worktree #1") == []

    def test_the_two_confirmations_stay_apart(self, recorded: Path, linked: Path) -> None:
        run(recorded, {
            "operation": "pull_request", "path": str(linked), "exempt": "local_only",
            "confirmation": {"source": "first reply", "response": "never pushed"},
        })
        run(recorded, {
            "operation": "pull_request", "path": str(linked), "replaces_exemption": True, **PULL_REQUEST,
            "confirmation": {"source": "second reply", "response": "now pushed"},
        })
        replaced = entry_of(recorded)["pr_exemption_replaced"]
        assert (replaced["source"], replaced["response"]) == ("first reply", "never pushed")
        assert (replaced["replaced_source"], replaced["replaced_response"]) == ("second reply", "now pushed")

    def test_the_record_survives_a_later_write(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        op(recorded, linked, "pull_request", replaces_exemption=True, **PULL_REQUEST)
        op(recorded, linked, "merge", merge_state="declined")
        assert "pr_exemption_replaced" in entry_of(recorded)

    def test_without_the_flag_a_held_exemption_still_refuses(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        refuse(recorded, linked, "already has a pull_request or a pr_exemption", "pull_request", **PULL_REQUEST)
        refuse(recorded, linked, "already has a pull_request", "pull_request", replaces_exemption=False, **PULL_REQUEST)

    def test_no_exemption_to_replace_names_none(self, recorded: Path, linked: Path) -> None:
        refuse(
            recorded, linked, "replaces_exemption needs a local_only pr_exemption to replace; the worktree has none",
            "pull_request", replaces_exemption=True, **PULL_REQUEST,
        )
        assert "pr_exemption_replaced" not in entry_of(recorded)

    def test_a_no_remote_exemption_is_named_and_kept(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "no_remote")
        refuse(
            recorded, linked,
            "replaces_exemption needs a local_only pr_exemption to replace; the worktree has no_remote",
            "pull_request", replaces_exemption=True, **PULL_REQUEST,
        )
        assert entry_of(recorded)["pr_exemption"]["reason"] == "no_remote"

    def test_a_recorded_pull_request_is_not_an_exemption(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", **PULL_REQUEST)
        refuse(recorded, linked, "the worktree has none", "pull_request", replaces_exemption=True, **PULL_REQUEST)

    def test_the_flag_does_not_apply_to_an_exemption_request(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        refuse(
            recorded, linked, "replaces_exemption applies to a pull_request record, not to an exemption",
            "pull_request", replaces_exemption=True, exempt="local_only",
        )

    @pytest.mark.parametrize("value", ["yes", 1, None, "true"])
    def test_the_flag_is_true_or_false(self, recorded: Path, linked: Path, value: Any) -> None:
        hold(recorded, linked, "local_only")
        refuse(
            recorded, linked, "replaces_exemption must be true or false", "pull_request", replaces_exemption=value,
            **PULL_REQUEST,
        )

    def test_a_refused_pull_request_keeps_the_exemption(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        refuse(
            recorded, linked, "url must be an https URL", "pull_request", replaces_exemption=True,
            **{**PULL_REQUEST, "url": "https://example.com/o/r/pull/7"},
        )
        assert entry_of(recorded)["pr_exemption"]["reason"] == "local_only"

    def test_the_replaced_record_is_not_a_second_exemption(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        op(recorded, linked, "pull_request", replaces_exemption=True, **PULL_REQUEST)
        refuse(recorded, linked, "already has", "pull_request", exempt="local_only")


class TestPolicyTwoKeepsItsRefusals:
    def test_a_held_exemption_meets_the_existing_refusal(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        at_policy(recorded, 2)
        refuse(
            recorded, linked, "the worktree already has a pull_request or a pr_exemption", "pull_request",
            replaces_exemption=True, **PULL_REQUEST,
        )
        assert entry_of(recorded)["pr_exemption"]["reason"] == "local_only"

    def test_the_flag_is_ignored_even_when_it_is_not_a_boolean(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        at_policy(recorded, 2)
        refuse(recorded, linked, "already has", "pull_request", replaces_exemption="yes", **PULL_REQUEST)

    def test_a_pull_request_with_the_flag_and_no_exemption_is_recorded(self, recorded: Path, linked: Path) -> None:
        at_policy(recorded, 2)
        op(recorded, linked, "pull_request", replaces_exemption=True, **PULL_REQUEST)
        assert "pr_exemption_replaced" not in entry_of(recorded)
        assert entry_of(recorded)["pull_request"]["number"] == 7

    def test_a_new_local_only_exemption_keeps_its_text(self, recorded: Path, linked: Path) -> None:
        at_policy(recorded, 2)
        refuse(recorded, linked, "a local_only exemption is no longer recorded at policy 2", "pull_request",
               exempt="local_only")


class TestSchemaAndShape:
    def test_the_operation_table_lists_the_key(self) -> None:
        key = "operation=pull_request,replaces_exemption=true"
        assert WORKTREE_REQUIRED_BY[key] == {"host", "number", "replaces_exemption", "url"}

    def test_the_request_still_requires_the_pull_request_keys(self) -> None:
        assert required_for("pull_request", {"replaces_exemption": True}, None) == {"host", "url", "number"}

    def test_the_field_is_an_optional_worktree_key(self) -> None:
        assert "pr_exemption_replaced" in WORKTREE_OPTIONAL_FIELDS

    def test_the_validator_accepts_a_replaced_exemption_beside_a_pull_request(self) -> None:
        record_ = {"host": "github", "url": URL, "number": 7, "recorded_at": STAMP}
        assert delivery_errors({**entry(), "pull_request": record_, "pr_exemption_replaced": REPLACED}, "w") == []

    @pytest.mark.parametrize("key", sorted(EXEMPTION_REPLACED_FIELDS))
    def test_the_validator_refuses_a_broken_record(self, key: str) -> None:
        found = delivery_errors({**entry(), "pr_exemption_replaced": {**REPLACED, key: ""}}, "w")
        assert any("w.pr_exemption_replaced: needs" in problem for problem in found)

    def test_the_validator_refuses_a_missing_or_extra_key(self) -> None:
        without = {key: value for key, value in REPLACED.items() if key != "reason"}
        assert delivery_errors({**entry(), "pr_exemption_replaced": without}, "w") != []
        assert delivery_errors({**entry(), "pr_exemption_replaced": {**REPLACED, "other": "x"}}, "w") != []
        assert delivery_errors({**entry(), "pr_exemption_replaced": "x"}, "w") != []

    def test_the_two_records_do_not_confuse_each_other(self) -> None:
        found = delivery_errors({**entry(), "version_exemption_replaced": {**REPLACED, "reason": ""}}, "w")
        assert any("w.version_exemption_replaced: needs" in problem for problem in found)
        assert not any("pr_exemption_replaced" in problem for problem in found)

    def test_a_state_written_with_the_field_loads(self, recorded: Path, linked: Path) -> None:
        hold(recorded, linked, "local_only")
        op(recorded, linked, "pull_request", replaces_exemption=True, **PULL_REQUEST)
        assert "pr_exemption_replaced" in load_state(recorded)["worktrees"][0]


ORDER = "Order: delivery records, the user's words, git worktree remove, the close record."


class TestCloseWithAMissingDirectory:
    """c10: a removal that already happened is recorded; the refusals that remain name the order."""

    @staticmethod
    def vanish(linked: Path) -> None:
        shutil.rmtree(linked)
        assert not linked.exists()

    def test_the_order_is_one_constant(self) -> None:
        assert workspace_delivery.REMOVAL_ORDER == ORDER

    def test_a_deleted_but_registered_directory_is_recorded_with_a_prune_note(
        self, recorded: Path, repository: Path, linked: Path
    ) -> None:
        self.vanish(linked)
        assert worktree_registered(repository, linked) is True
        result = close(recorded, linked, "remove")
        state = load_state(recorded)
        assert state["worktrees"][0]["status"] == "removed"
        assert state["working_directory"] == str(repository.resolve())
        assert len(result["notes"]) == 1 and "git worktree prune" in result["notes"][0]

    def test_a_pruned_directory_is_recorded_without_a_note(
        self, recorded: Path, repository: Path, linked: Path
    ) -> None:
        self.vanish(linked)
        git(repository, "worktree", "prune")
        result = close(recorded, linked, "remove")
        assert load_state(recorded)["worktrees"][0]["status"] == "removed"
        assert "notes" not in result

    def test_a_present_directory_still_refuses_and_names_the_order(self, recorded: Path, linked: Path) -> None:
        refused(recorded, re.escape("the worktree is still present or registered; ") + ".*" + re.escape(ORDER),
                close, linked, "remove")

    def test_a_missing_directory_with_unreadable_registration_still_refuses(
        self, recorded: Path, linked: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.vanish(linked)
        monkeypatch.setattr("workspace_workflows.worktree_registered", lambda repository, path: None)
        refused(recorded, "still present or registered.*" + re.escape(ORDER), close, linked, "remove")

    def test_keep_for_a_missing_directory_keeps_its_start_and_names_the_order(
        self, recorded: Path, linked: Path
    ) -> None:
        self.vanish(linked)
        refused(recorded, re.escape("the worktree is missing; restore it or record its removal: ") + ".*"
                + re.escape(ORDER), close, linked, "keep")

    def test_incomplete_records_end_with_the_order(self, recorded: Path, linked: Path) -> None:
        at_policy(recorded, 2)
        self.vanish(linked)
        with pytest.raises(WorkspaceError, match="delivery records are complete") as caught:
            close(recorded, linked, "remove")
        assert str(caught.value).endswith(ORDER)
