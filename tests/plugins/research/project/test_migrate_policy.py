"""`research-project migrate-policy`: preview, apply and refusals for moving a project to policy 2 (T06)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import manage_workspace
import pytest
import workspace_lib as lib
from workspace_lib import WorkspaceError, delivery_rules
from workspace_operations import migration_plan
from workspace_workflows import migrate_policy

from tests.plugins.research.project.test_workflow_automation import fill_spec, task

WORDS = "Yes, move this project to the new delivery policy"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with no policy_version (derived policy 0 under the test cutoff), in PLANNING."""
    from workspace_session import update_project_data

    target = tmp_path / "target"
    target.mkdir()
    created = lib.allocate_project(tmp_path / "workspace", title="Legacy", working_directory=target, create_root=True)
    fill_spec(created)
    update_project_data(created, {"status": "PLANNING", "tasks": [task("T01")]}, expected_revision=0)
    return created


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def write_state(project: Path, **changes: Any) -> None:
    state = state_of(project)
    state.update(changes)
    (project / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def files(project: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in project.iterdir() if path.is_file()}


def apply(project: Path, target: str = "pr", **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "target": target, "apply": True, "expected_revision": state_of(project)["revision"], "confirm": WORDS,
    }
    fields.update(overrides)
    return migrate_policy(project, **fields)


def refusal(project: Path, **overrides: Any) -> str:
    before = files(project)
    with pytest.raises(WorkspaceError) as caught:
        apply(project, **overrides)
    assert files(project) == before, "a refused migration writes nothing"
    return str(caught.value)


def entry(**fields: Any) -> dict[str, Any]:
    base = {"path": "/w/linked", "kind": "created", "role": "target", "status": "active"}
    return {**base, **fields}


# --- preview ----------------------------------------------------------------------------------


def test_preview_is_the_default_and_writes_nothing(project: Path) -> None:
    before = files(project)
    result = migrate_policy(project, target="pr")
    assert files(project) == before
    assert result["applied"] is False
    assert result["from_policy"] == 0 and result["to_policy"] == 2 and result["delivery_target"] == "pr"
    assert any("merge request" in item for item in result["gained"])
    assert "migrated" not in json.dumps(result)


def test_preview_names_what_each_target_gains_and_drops() -> None:
    state = {"created": "2026-11-16T10:00:00+00:00", "schema_version": 4, "status": "PLANNING", "policy_version": 1}
    plan = migration_plan(state, "local")
    assert plan["from_policy"] == 1
    assert any("merge request" in item for item in plan["dropped"])
    assert any("release record" in item for item in plan["dropped"])
    assert plan["gained"] == []
    research = migration_plan({**state, "tasks": []}, "research")
    assert "no writes to the target" in research["gained"]


def test_preview_lists_obligations_existing_entries_do_not_satisfy() -> None:
    state = {"schema_version": 4, "status": "PLANNING", "policy_version": 0, "worktrees": [entry()]}
    plan = migration_plan(state, "release")
    text = " | ".join(plan["unsatisfied"])
    assert "/w/linked" in text
    assert "pr_exemption" in text and "release record" in text
    assert migration_plan(state, "local")["unsatisfied"] == []


def test_preview_lists_a_merge_the_target_would_demand() -> None:
    pulled = entry(pull_request={"number": 1}, version_exemption={"reason": "x"})
    state = {"schema_version": 4, "status": "PLANNING", "policy_version": 1, "worktrees": [pulled]}
    assert any("merge" in item for item in migration_plan(state, "merged")["unsatisfied"])
    assert migration_plan(state, "pr")["unsatisfied"] == []


def test_preview_lists_contradictions_with_existing_evidence() -> None:
    merged = {"state": "merged"}
    release = {"release": {"version": "1.0.0"}}
    busy = entry(pull_request={"number": 1}, merge=merged, version_line=release)
    state = {"schema_version": 4, "status": "PLANNING", "policy_version": 1, "worktrees": [busy], "tasks": []}
    text = " | ".join(migration_plan(state, "local")["contradictions"])
    assert "pull_request" in text and "merge record" in text and "release record" in text
    assert migration_plan(state, "release")["contradictions"] == []
    assert any("research does not use" in item for item in migration_plan(state, "research")["contradictions"])


def test_preview_keeps_a_legacy_local_only_entry_and_says_so() -> None:
    legacy = entry(pr_exemption={"reason": "local_only"})
    state = {"schema_version": 4, "status": "PLANNING", "policy_version": 1, "worktrees": [legacy]}
    plan = migration_plan(state, "pr")
    assert any("local_only" in item and "stays valid" in item for item in plan["notes"])


def test_preview_through_the_cli_prints_json(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(project, "--to", "2", "--target", "local")
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is False and printed["delivery_target"] == "local"


# --- apply ------------------------------------------------------------------------------------


def test_apply_writes_both_fields_together_in_one_revision(project: Path) -> None:
    before = state_of(project)
    result = apply(project, "pr")
    after = state_of(project)
    assert result["applied"] is True
    assert after["policy_version"] == 2 and after["delivery_target"] == "pr"
    assert after["revision"] == before["revision"] + 1
    assert delivery_rules(after).policy == 2 and delivery_rules(after).target == "pr"
    for untouched in ("status", "tasks", "worktrees", "title", "created"):
        assert after.get(untouched) == before.get(untouched)
    assert lib.validate_project(project).errors == []


def test_apply_records_the_users_words_in_the_decision_history(project: Path) -> None:
    apply(project, "local")
    spec = (project / "spec.md").read_text(encoding="utf-8")
    assert WORDS in spec and "kind: migrate-policy" in spec
    assert "delivery_target: local" in spec


@pytest.mark.parametrize("target", lib.DELIVERY_TARGETS)
def test_apply_accepts_each_target(project: Path, target: str) -> None:
    apply(project, target)
    assert state_of(project)["delivery_target"] == target


def test_a_policy_one_project_migrates_too(project: Path) -> None:
    write_state(project, policy_version=1)
    assert migrate_policy(project, target="pr")["from_policy"] == 1
    apply(project, "pr")
    assert state_of(project)["policy_version"] == 2


def test_apply_keeps_records_that_the_new_rules_no_longer_require(project: Path) -> None:
    write_state(project, policy_version=1, worktrees=[])
    apply(project, "local")
    assert state_of(project)["worktrees"] == []


def test_apply_through_the_cli(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    revision = state_of(project)["revision"]
    code = run_cli(project, "--to", "2", "--target", "merged", "--apply", "--expected-revision", str(revision),
                   "--confirm", WORDS)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert state_of(project)["delivery_target"] == "merged"


def test_the_migrated_project_cannot_be_migrated_again(project: Path) -> None:
    apply(project, "pr")
    assert "already at policy 2" in refusal(project, target="local")


# --- refusals ---------------------------------------------------------------------------------


def test_refuses_without_the_users_words(project: Path) -> None:
    assert "confirm" in refusal(project, confirm=None)
    assert "confirm" in refusal(project, confirm="   ")


def test_refuses_apply_without_an_expected_revision(project: Path) -> None:
    assert "expected-revision" in refusal(project, expected_revision=None)


def test_refuses_a_stale_revision(project: Path) -> None:
    before = files(project)
    with pytest.raises(Exception, match="expected_revision"):
        apply(project, expected_revision=state_of(project)["revision"] + 5)
    assert files(project) == before


@pytest.mark.parametrize("target", ["", "nope", None])
def test_refuses_an_unknown_target(project: Path, target: object) -> None:
    assert "research, local, pr, merged, release" in refusal(project, target=target)
    with pytest.raises(WorkspaceError, match="research, local, pr, merged, release"):
        migrate_policy(project, target=target)  # type: ignore[arg-type]


def test_refuses_a_project_still_aligning(project: Path) -> None:
    write_state(project, status="ALIGNING")
    assert "ALIGNING" in refusal(project)


@pytest.mark.parametrize("status", ["ALIGNING", "DONE", "CANCELLED"])
def test_the_plan_refuses_a_status_that_cannot_change_policy(status: str) -> None:
    with pytest.raises(WorkspaceError, match=status):
        migration_plan({"schema_version": 4, "status": status, "policy_version": 0}, "pr")


@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED"])
def test_refuses_a_running_or_blocked_task(project: Path, status: str) -> None:
    state = state_of(project)
    state["tasks"][0]["status"] = status
    write_state(project, tasks=state["tasks"])
    assert "T01" in refusal(project) and status in refusal(project)


def test_refuses_an_open_attempt(project: Path) -> None:
    state = state_of(project)
    execution = copy.deepcopy(state.get("execution", {}))
    execution["attempts"] = {"a1": {"task": "T01"}}
    write_state(project, execution=execution)
    assert "attempt" in refusal(project)


def test_refuses_a_schema_older_than_four(project: Path) -> None:
    write_state(project, schema_version=3)
    assert "schema_version" in refusal(project)


def test_refuses_an_already_policy_two_project(project: Path) -> None:
    write_state(project, policy_version=2, delivery_target="pr")
    assert "already at policy 2" in refusal(project)


def test_refuses_research_while_a_task_still_writes_to_the_target(project: Path) -> None:
    state = state_of(project)
    state["tasks"][0]["outputs"] = [{"root": "target", "path": "x.txt", "required": False}]
    write_state(project, tasks=state["tasks"])
    assert "T01" in refusal(project, target="research")
    apply(project, "local")  # the same plan is fine for any other target


def test_the_guarded_fields_still_refuse_a_plain_update(project: Path) -> None:
    from workspace_session import update_project_data

    with pytest.raises(WorkspaceError, match="policy_version"):
        update_project_data(project, {"policy_version": 2}, expected_revision=state_of(project)["revision"])


# --- the command line -------------------------------------------------------------------------


def run_cli(project: Path, *arguments: str) -> int:
    saved = sys.argv
    sys.argv = ["research-project", "migrate-policy", str(project), *arguments]
    try:
        return manage_workspace.main()
    finally:
        sys.argv = saved


def test_the_command_is_registered_beside_migrate_and_is_distinct_from_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = manage_workspace._build_parser()
    help_text = parser.format_help()
    assert "migrate-policy" in help_text and "migrate " in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["migrate-policy", ".", "--to", "1", "--target", "pr"])
    with pytest.raises(SystemExit):
        parser.parse_args(["migrate-policy", ".", "--to", "2", "--target", "somewhere"])


def test_a_refusal_through_the_cli_exits_one(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_state(project, policy_version=2, delivery_target="pr")
    assert run_cli(project, "--to", "2", "--target", "local") == 1
    assert "already at policy 2" in capsys.readouterr().err


def test_apply_through_the_cli_without_words_exits_one(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(project, "--to", "2", "--target", "pr", "--apply", "--expected-revision", "3")
    assert code == 1
    assert "confirm" in capsys.readouterr().err
    assert "policy_version" not in state_of(project)


# --- the plan on its own: refusals the state loader would otherwise pre-empt --------------------


def plan_state(**fields: Any) -> dict[str, Any]:
    return {"schema_version": 4, "status": "PLANNING", "policy_version": 0, **fields}


def test_the_plan_refuses_a_schema_older_than_four() -> None:
    with pytest.raises(WorkspaceError, match="schema_version 4, but this project has 3"):
        migration_plan(plan_state(schema_version=3), "pr")


@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED"])
def test_the_plan_names_each_running_or_blocked_task(status: str) -> None:
    tasks = [{"id": "T01", "status": status}, {"id": "T02", "status": "TODO"}, {"id": "T03", "status": "DONE"}]
    with pytest.raises(WorkspaceError, match=rf"T01 \({status}\)") as caught:
        migration_plan(plan_state(tasks=tasks), "pr")
    assert "T02" not in str(caught.value)


def test_the_plan_skips_removed_and_non_repository_entries() -> None:
    entries = [entry(status="removed"), entry(path="/n", kind="none")]
    plan = migration_plan(plan_state(worktrees=entries), "release")
    assert plan["unsatisfied"] == [] and plan["contradictions"] == [] and plan["notes"] == []


def test_the_plan_refuses_a_coordinator_run() -> None:
    with pytest.raises(WorkspaceError, match="attempt is open or a coordinator run"):
        migration_plan(plan_state(execution={"attempts": {}, "coordinator_run": "run-1"}), "pr")


# --- retries, flags and records the round-7 review found --------------------------------------------


def test_a_repeated_apply_replays_its_journal_instead_of_failing(project: Path) -> None:
    first = apply(project, "pr")
    revision = state_of(project)["revision"]
    again = migrate_policy(
        project, target="pr", apply=True, expected_revision=revision - 1, confirm=WORDS
    )
    assert again["applied"] is True and again["delivery_target"] == first["delivery_target"]
    assert state_of(project)["revision"] == revision


def test_the_operation_id_names_the_target(project: Path) -> None:
    apply(project, "pr")
    journals = [path.name for path in (project / "operations").iterdir()] if (project / "operations").is_dir() else []
    assert not journals or any("migrate-policy-r" in name and name.endswith("-pr.json") for name in journals)


def test_preview_refuses_the_apply_only_flags(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="--apply"):
        migrate_policy(project, target="pr", confirm=WORDS)
    with pytest.raises(WorkspaceError, match="--apply"):
        migrate_policy(project, target="pr", expected_revision=3)


def test_a_removed_worktree_is_no_contradiction_for_research() -> None:
    gone = entry(status="removed")
    plan = migration_plan(plan_state(worktrees=[gone]), "research")
    assert plan["contradictions"] == []


def test_the_preview_reports_what_the_closure_check_would() -> None:
    from workspace_delivery import closure_findings

    state = plan_state(worktrees=[entry()])
    hypothetical = {**state, "policy_version": 2, "delivery_target": "release"}
    assert migration_plan(state, "release")["unsatisfied"] == closure_findings(hypothetical)


def test_a_filesystem_failure_is_reported_not_a_traceback(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import workspace_workflows

    def broken(*arguments: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(workspace_workflows, "_migrate_policy", broken)
    with pytest.raises(WorkspaceError, match="migrate-policy filesystem failure: disk full"):
        migrate_policy(project, target="pr")
