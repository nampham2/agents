"""Policy-2 delivery gates per target: the call sites read `delivery_rules`, not the creation date (T05)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import workspace_context
import workspace_delivery
import workspace_goldfish
import workspace_lib
import workspace_workflows
from workspace_delivery import _pull_request, closure_findings, mr_gate_errors, removal_findings, start_findings
from workspace_goldfish import delivery_gate_errors, goldfish_event_warnings
from workspace_lib import WorkspaceError, delivery_rules
from workspace_workflows import readiness_worktrees

TARGETS = ("research", "local", "pr", "merged", "release")
CONFIRMATION = {"source": "user reply", "response": "yes"}
MERGED = {"state": "merged", "method": "squash", "evidence": "host", "recorded_at": "2026-11-20T10:00:00+00:00"}


def entry(**fields: Any) -> dict[str, Any]:
    base = {
        "path": "/w/linked",
        "repository": "/w/repo",
        "branch": "feat/x",
        "base_commit": "a" * 40,
        "kind": "created",
        "role": "target",
        "status": "active",
    }
    return {**base, **fields}


def state_for(target: "str | None", *entries: dict[str, Any], policy: int = 2, **fields: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "created": "2026-11-16T10:00:00+00:00",
        "policy_version": policy,
        "working_directory": "/w/linked",
        "worktrees": list(entries),
        **fields,
    }
    if target is not None:
        state["delivery_target"] = target
    return state


def writing_task(task_id: str = "T01") -> dict[str, Any]:
    return {"id": task_id, "depends_on": [], "outputs": [{"root": "target", "path": "x.txt", "required": False}]}


# --- start_findings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "refuses"),
    [("research", True), ("local", False), ("pr", True), ("merged", True), ("release", True)],
)
def test_start_findings_per_target(target: str, refuses: bool) -> None:
    assert bool(start_findings(state_for(target, entry()))) is refuses


def test_research_start_names_retarget_with_or_without_a_worktree() -> None:
    for state in (state_for("research", entry()), state_for("research")):
        findings = start_findings(state)
        assert findings
        assert "retarget" in findings[0]


def test_an_exemption_or_pull_request_satisfies_start_for_pr_target() -> None:
    assert start_findings(state_for("pr", entry(pull_request={"number": 1}))) == []
    assert start_findings(state_for("pr", entry(pr_exemption={"reason": "no_remote"}))) == []


def test_policy_one_start_is_unchanged() -> None:
    assert start_findings(state_for(None, entry(), policy=1))
    assert start_findings(state_for(None, entry(), policy=0)) == []


# --- closure_findings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("research", []),
        ("local", []),
        ("pr", ["no pull_request or pr_exemption"]),
        ("merged", ["no pull_request or pr_exemption"]),
        ("release", ["no pull_request or pr_exemption", "no release record"]),
    ],
)
def test_closure_findings_for_a_bare_entry(target: str, expected: list[str]) -> None:
    findings = closure_findings(state_for(target, entry()))
    assert len(findings) == len(expected)
    for fragment, finding in zip(expected, findings, strict=True):
        assert fragment in finding


def test_closure_findings_for_an_entry_with_a_pull_request() -> None:
    pull = {"pull_request": {"number": 1}}
    assert closure_findings(state_for("pr", entry(**pull))) == []
    assert any("merge decision" in item for item in closure_findings(state_for("merged", entry(**pull))))
    assert closure_findings(state_for("merged", entry(**pull, merge=MERGED))) == []
    assert any("release record" in item for item in closure_findings(state_for("release", entry(**pull, merge=MERGED))))


def test_a_legacy_local_only_entry_still_owes_its_merge_decision_under_merged() -> None:
    legacy = entry(pr_exemption={"reason": "local_only"})
    assert any("merge decision" in item for item in closure_findings(state_for("merged", legacy)))
    assert closure_findings(state_for("pr", legacy)) == []
    assert closure_findings(state_for("local", legacy)) == []


def test_a_no_remote_entry_owes_no_merge() -> None:
    assert closure_findings(state_for("merged", entry(pr_exemption={"reason": "no_remote"}))) == []


def test_policy_two_without_a_target_behaves_as_policy_one() -> None:
    assert closure_findings(state_for(None, entry())) == closure_findings(state_for(None, entry(), policy=1))


# --- removal_findings -------------------------------------------------------------------------


def removal(target: str, item: dict[str, Any]) -> list[str]:
    return removal_findings(item, rules=delivery_rules(state_for(target, item)))


@pytest.mark.parametrize(("target", "count"), [("research", 0), ("local", 0), ("pr", 1), ("merged", 1), ("release", 2)])
def test_removal_of_a_bare_entry(target: str, count: int) -> None:
    assert len(removal(target, entry())) == count


def test_removal_needs_the_merge_only_for_merged_and_release() -> None:
    pull = entry(pull_request={"number": 1}, version_exemption={"reason": "x"})
    assert removal("local", pull) == []
    assert removal("pr", pull) == []
    assert any("not merged" in item for item in removal("merged", pull))
    assert removal("merged", {**pull, "merge": MERGED}) == []


def test_removal_of_a_legacy_local_only_entry_needs_a_decision_under_merged() -> None:
    legacy = entry(pr_exemption={"reason": "local_only"})
    assert any("no merge decision" in item for item in removal("merged", legacy))
    assert removal("pr", legacy) == []


def test_policy_one_removal_still_demands_the_merge_of_a_pull_request() -> None:
    item = entry(pull_request={"number": 1}, version_exemption={"reason": "x"})
    rules = delivery_rules(state_for(None, item, policy=0))
    assert any("not merged" in found for found in removal_findings(item, rules=rules))


# --- mr_gate_errors ---------------------------------------------------------------------------


def candidate(target: str, *entries: dict[str, Any], tasks: "list[dict[str, Any]] | None" = None) -> dict[str, Any]:
    return state_for(target, *entries, tasks=tasks if tasks is not None else [writing_task()])


def test_research_planning_refuses_a_target_output_before_the_early_exits(tmp_path: Path) -> None:
    previous = {"tasks": []}
    for state in (candidate("research"), candidate("research", entry())):
        errors, warnings = mr_gate_errors(tmp_path, previous, state)
        assert errors and "retarget" in errors[0]
        assert warnings == []


def test_research_planning_accepts_a_plan_without_target_outputs(tmp_path: Path) -> None:
    quiet = {"id": "T01", "depends_on": [], "outputs": []}
    assert mr_gate_errors(tmp_path, {"tasks": []}, candidate("research", tasks=[quiet])) == ([], [])


def test_local_planning_needs_no_merge_request(tmp_path: Path) -> None:
    assert mr_gate_errors(tmp_path, {"tasks": []}, candidate("local", entry())) == ([], [])


def test_pr_planning_still_needs_a_merge_request(tmp_path: Path) -> None:
    errors, _ = mr_gate_errors(tmp_path, {"tasks": []}, candidate("pr", entry()))
    assert errors


# --- worktree_status --------------------------------------------------------------------------


def observed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workspace_context,
        "worktree_observation",
        lambda path: {"available": True, "is_repository": True, "is_worktree": False},
    )


@pytest.mark.parametrize(("target", "warns"), [("research", False), ("local", True), ("pr", True), ("merged", True)])
def test_worktree_status_warning_per_target(monkeypatch: pytest.MonkeyPatch, target: str, warns: bool) -> None:
    observed(monkeypatch)
    status = workspace_context.worktree_status(state_for(target))
    assert bool(status["warnings"]) is warns


def test_policy_one_status_still_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    observed(monkeypatch)
    assert workspace_context.worktree_status(state_for(None, policy=1))["warnings"]


def test_research_status_still_warns_when_git_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_context, "worktree_observation", lambda path: {"available": False})
    assert workspace_context.worktree_status(state_for("research"))["warnings"]


# --- local_only is refused under policy 2 -----------------------------------------------------


def test_local_only_is_refused_at_policy_two_and_accepted_before(tmp_path: Path) -> None:
    for target in TARGETS:
        item = entry()
        with pytest.raises(WorkspaceError, match="local_only"):
            _pull_request(item, {"exempt": "local_only"}, tmp_path, CONFIRMATION, delivery_rules(state_for(target)))
    item = entry()
    _pull_request(item, {"exempt": "local_only"}, tmp_path, CONFIRMATION, delivery_rules(state_for(None, policy=1)))
    assert item["pr_exemption"]["reason"] == "local_only"


# --- readiness --------------------------------------------------------------------------------


def test_readiness_lists_what_removal_lacks_by_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workspace_workflows,
        "worktree_observation",
        lambda path: {"available": True, "exists": True},
    )
    item = entry()
    local = readiness_worktrees(state_for("local", item), rules=delivery_rules(state_for("local")))
    release = readiness_worktrees(state_for("release", item), rules=delivery_rules(state_for("release")))
    assert local[0]["missing"] == []
    assert len(release[0]["missing"]) == 2


# --- Goldfish diff events ---------------------------------------------------------------------


@pytest.fixture
def goldfish_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "is_goldfish_gated", lambda: True)
    monkeypatch.setattr(workspace_goldfish, "has_code_task", lambda state: True)


def gate(target: str, item: dict[str, Any], event: str, request: "dict[str, Any] | None" = None) -> list[str]:
    state = state_for(target, item)
    return delivery_gate_errors(Path("/nowhere"), state, item, event, request or {})


@pytest.mark.parametrize(
    ("target", "event", "owed"),
    [
        ("research", "merge", False),
        ("local", "merge", False),
        ("pr", "merge", False),
        ("merged", "merge", True),
        ("release", "merge", True),
        ("research", "release", False),
        ("local", "release", False),
        ("pr", "release", False),
        ("merged", "release", False),
        ("release", "release", True),
    ],
)
def test_merge_and_release_branches_follow_diff_events(goldfish_on: None, target: str, event: str, owed: bool) -> None:
    errors = gate(target, entry(), event)
    assert bool(errors) is owed


@pytest.mark.parametrize(
    ("target", "item", "owed"),
    [
        ("research", entry(pr_exemption={"reason": "no_remote"}), False),
        ("local", entry(pr_exemption={"reason": "no_remote"}), False),
        ("pr", entry(pr_exemption={"reason": "no_remote"}), True),
        ("pr", entry(pull_request={"number": 1}), True),
        ("merged", entry(pr_exemption={"reason": "no_remote"}), True),
        ("merged", entry(pull_request={"number": 1}), False),
        ("release", entry(pr_exemption={"reason": "no_remote"}), True),
    ],
)
def test_closure_branch_follows_diff_events(goldfish_on: None, target: str, item: dict[str, Any], owed: bool) -> None:
    assert bool(gate(target, item, "closure")) is owed


def test_closure_round_exits_for_a_pr_target(goldfish_on: None) -> None:
    assert gate("pr", entry(pull_request={"number": 1}, merge=MERGED), "closure") == []
    assert gate("pr", entry(pull_request={"number": 1}, merge={"state": "declined"}), "closure") == []


def test_policy_one_diff_branches_are_unchanged(goldfish_on: None) -> None:
    item = entry(pr_exemption={"reason": "no_remote"})
    state = state_for(None, item, policy=1)
    assert delivery_gate_errors(Path("/nowhere"), state, item, "merge", {})
    assert delivery_gate_errors(Path("/nowhere"), state, item, "closure", {})
    pull = entry(pull_request={"number": 1})
    assert delivery_gate_errors(Path("/nowhere"), state, pull, "closure", {}) == []


def event_warnings(target: "str | None", item: dict[str, Any], policy: int = 2) -> list[str]:
    state = state_for(target, item, policy=policy, tasks=[])
    return goldfish_event_warnings(Path("/nowhere-project"), state)


@pytest.fixture
def warnings_on(monkeypatch: pytest.MonkeyPatch, goldfish_on: None) -> None:
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(workspace_goldfish, "round_documents", lambda project: [])
    monkeypatch.setattr(workspace_goldfish, "has_code_task", lambda state: True)


@pytest.mark.parametrize(
    ("target", "warns"),
    [("research", False), ("local", False), ("pr", False), ("merged", True), ("release", True)],
)
def test_unrecorded_merge_warning_follows_the_rules(warnings_on: None, target: str, warns: bool) -> None:
    closed = entry(
        status="kept",
        pull_request={"number": 1},
        closure={"decision": "keep", "observed_at": "2026-11-20T10:00:00+00:00"},
    )
    found = event_warnings(target, closed)
    assert any("no merge recorded" in item for item in found) is warns


def test_the_release_and_merge_lines_follow_diff_events(warnings_on: None) -> None:
    shipped = entry(pull_request={"number": 1}, merge=MERGED)
    assert any("merge of" in item for item in event_warnings("merged", shipped))
    assert not any("merge of" in item for item in event_warnings("pr", shipped))
    assert not any("merge of" in item for item in event_warnings("local", shipped))


# --- routing: no check reads the creation date or `needs_merge` directly ---------------------------


@pytest.mark.parametrize(
    "function",
    [
        workspace_delivery.start_findings,
        workspace_delivery.closure_findings,
        workspace_delivery.removal_findings,
        workspace_delivery.mr_gate_errors,
        workspace_workflows.readiness_worktrees,
        workspace_goldfish.delivery_gate_errors,
        workspace_goldfish._worktree_event_warnings,
    ],
)
def test_delivery_checks_do_not_branch_on_the_date_or_needs_merge(function: Any) -> None:
    source = inspect.getsource(function)
    assert "is_delivery_gated" not in source
    assert not re.search(r"\bneeds_merge\(", source), function.__name__


def test_workflows_no_longer_import_the_date_gate() -> None:
    assert "is_delivery_gated" not in inspect.getsource(workspace_workflows)


# --- routing over every script: who may read the date gate or `needs_merge` -------------------------


def _callers(name: str) -> set[tuple[str, str]]:
    """(module, enclosing function) of every call or reference to `name` in the plugin scripts."""
    import ast

    root = Path(workspace_lib.__file__).parent
    found: set[tuple[str, str]] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                    found.add((path.stem, function.name))
    return found


def test_the_date_gate_is_read_only_by_the_policy_derivation() -> None:
    assert _callers("is_delivery_gated") == {("workspace_lib", "_policy_value")}


def test_needs_merge_is_read_only_where_a_merge_basis_is_the_question() -> None:
    assert _callers("needs_merge") == {
        ("workspace_lib", "merge_demanded"),
        ("workspace_lib", "merge_warned"),
        ("workspace_lib", "removal_merge"),
        ("workspace_delivery", "_merge"),
        ("workspace_delivery", "delivery_errors"),
        ("workspace_delivery", "abandoned_note"),
        ("workspace_dashboard", "_local_label"),
    }
    forbidden = {"closure_findings", "removal_findings", "start_findings", "mr_gate_errors", "delivery_gate_errors"}
    assert not {function for _, function in _callers("needs_merge")} & forbidden


# --- populations 0 and 1 keep today's obligations, check by check ----------------------------------


def population(policy: int, *entries: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return state_for(None, *entries, policy=policy, **fields)


def test_population_zero_owes_no_delivery_record_and_still_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    observed(monkeypatch)
    bare = entry()
    state = population(0, bare, tasks=[writing_task()])
    assert start_findings(state) == []
    assert closure_findings(state) == []
    assert mr_gate_errors(Path("/p"), {"tasks": []}, state) == ([], [])
    assert workspace_context.worktree_status(population(0))["warnings"]
    assert removal_findings(bare, rules=delivery_rules(state)) == []
    pulled = entry(pull_request={"number": 1})
    assert any("not merged" in item for item in removal_findings(pulled, rules=delivery_rules(state)))


def test_population_one_owes_every_delivery_record(monkeypatch: pytest.MonkeyPatch) -> None:
    observed(monkeypatch)
    bare = entry()
    state = population(1, bare, tasks=[writing_task()])
    assert len(start_findings(state)) == 1
    assert [fragment in finding for fragment, finding in zip(
        ("pull_request or pr_exemption", "release record"), closure_findings(state), strict=True)] == [True, True]
    errors, _ = mr_gate_errors(Path("/p"), {"tasks": []}, state)
    assert errors
    assert workspace_context.worktree_status(population(1))["warnings"]
    assert len(removal_findings(bare, rules=delivery_rules(state))) == 2
    local = entry(pr_exemption={"reason": "local_only"})
    assert any("merge decision" in item for item in closure_findings(population(1, local)))
    assert any("no merge decision" in item for item in removal_findings(local, rules=delivery_rules(state)))


@pytest.mark.parametrize("policy", [0, 1])
def test_populations_zero_and_one_keep_every_diff_event_and_the_merge_warning(
    goldfish_on: None, warnings_on: None, policy: int
) -> None:
    item = entry(pr_exemption={"reason": "no_remote"})
    state = population(policy, item)
    for event in ("merge", "closure", "release"):
        assert delivery_gate_errors(Path("/nowhere"), state, item, event, {}), event
    closed = entry(
        status="kept",
        pull_request={"number": 1},
        closure={"decision": "keep", "observed_at": "2026-11-20T10:00:00+00:00"},
    )
    assert any("no merge recorded" in found for found in event_warnings(None, closed, policy=policy))


@pytest.mark.parametrize("policy", [0, 1])
def test_a_stored_policy_beats_the_creation_date(policy: int) -> None:
    for created in ("2020-01-01T00:00:00+00:00", "2026-11-16T10:00:00+00:00"):
        assert delivery_rules({"created": created, "policy_version": policy}).policy == policy


# --- review findings: ordering of the record check, the confirm guard, BLOCKED ----------------------


def test_a_policy_one_entry_without_a_branch_keeps_the_record_error_at_closure(goldfish_on: None) -> None:
    bare = entry(branch=None)
    state = population(1, bare)
    assert "no branch or path" in " ".join(delivery_gate_errors(Path("/nowhere"), state, bare, "closure", {}))


def test_a_target_that_owes_nothing_ignores_an_entry_without_a_branch(goldfish_on: None) -> None:
    bare = entry(branch=None)
    assert delivery_gate_errors(Path("/nowhere"), state_for("local", bare), bare, "closure", {}) == []


def test_a_merged_or_declined_pr_entry_without_a_branch_owes_no_closure_round(goldfish_on: None) -> None:
    for merge in (MERGED, {"state": "declined"}):
        item = entry(branch=None, pull_request={"number": 1}, merge=merge)
        assert delivery_gate_errors(Path("/nowhere"), state_for("pr", item), item, "closure", {}) == []
    open_pr = entry(branch=None, pull_request={"number": 1})
    assert delivery_gate_errors(Path("/nowhere"), state_for("pr", open_pr), open_pr, "closure", {})


def test_a_confirmation_may_only_set_the_target_from_absent() -> None:
    from workspace_lib import _immutable_field_changes

    assert _immutable_field_changes({}, {"delivery_target": "pr"}, "confirm") == []
    changes = _immutable_field_changes({"delivery_target": "pr"}, {"delivery_target": "local"}, "confirm")
    assert changes == ["candidate cannot change guarded field: delivery_target"]
    assert _immutable_field_changes({"delivery_target": "pr"}, {"delivery_target": "local"}, "retarget") == []


def test_a_blocked_policy_two_project_without_a_target_is_valid_but_planning_is_not() -> None:
    from workspace_lib import ValidationReport, _validate_delivery_policy

    for status, ok in (("ALIGNING", True), ("BLOCKED", True), ("CANCELLED", True), ("PLANNING", False)):
        report = ValidationReport()
        _validate_delivery_policy({"policy_version": 2, "status": status}, report)
        assert (report.errors == []) is ok, status
