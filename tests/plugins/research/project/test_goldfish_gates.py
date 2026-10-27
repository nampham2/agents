"""The four Goldfish gates: each refuses without a current round, passes with one, and cannot be walked around.

Every gate is shown refusing first. The gates apply to events at or after `GOLDFISH_FROM`, which ships
as a far-future placeholder, so each test moves it into the past (the `gated` fixture) and one test
leaves it where it ships.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
import workspace_goldfish as goldfish
import workspace_lib
from workspace_goldfish import (
    confirm_gate_errors,
    delivery_gate_errors,
    design_gate_errors,
    goldfish_event_warnings,
    goldfish_findings,
)
from workspace_journal import snapshot
from workspace_lib import WorkspaceError, planning_path
from workspace_operations import task_operation
from workspace_session import update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_delivery import FILES, URL, fetched, op, write_versions
from tests.plugins.research.project.test_goldfish import (
    CODE_TASK,
    CONFIRMATION,
    close,
    edit_planning,
    finding,
    gold,
    open_round,
    report,
    rounds,
)
from tests.plugins.research.project.test_workflow_automation import fill_spec
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    git,
    load_state,
    make_project,
    make_repository,
    record,
)

PAST = "2000-01-01T00:00:00+00:00"
IDS = itertools.count(1)
BRANCH = "npham/2026-01-01-001-x"


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", PAST)


@pytest.fixture
def ungated_alignment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the older alignment gate and task stamps out of the way of tests that start tasks."""
    monkeypatch.setattr(workspace_lib, "GATES_ENFORCED_FROM", "2099-01-01T00:00:00+00:00")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = make_project(tmp_path, make_repository(tmp_path))
    fill_spec(path)
    (path / "planning.md").write_text("# R1\n\nStatus: draft\n\nThe plan.\n", encoding="utf-8")
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text("# Design\n\n## 1. Purpose\n\nGoal.\n", encoding="utf-8")
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    return path


def accept(project: Path, target: str) -> dict[str, Any]:
    stages = ("comprehension", "combined") if target in ("spec",) else ("combined",)
    opened = open_round(project, target, stages)
    return close(project, opened, reports=report(stages))


def confirm(project: Path, kind: str, review_id: str = "R1") -> dict[str, Any]:
    spec = snapshot(project / "spec.md")[1]
    plan = snapshot(planning_path(project))[1]
    fields = {
        "requirements": {"proposal_sha256": spec},
        "planning": {"proposal_sha256": plan},
        "alignment": {"requirements_sha256": spec, "planning_sha256": plan},
    }[kind]
    request = {
        "id": f"c-{next(IDS)}", "expected_revision": load_state(project)["revision"], "kind": kind,
        "review_id": review_id, "response": "yes", "source": "user reply", "scope": "all",
        "tokens": {"spec": spec, "planning": plan}, **fields,
    }
    return workflow(project, "confirm", request)


def refused(project: Path, fragment: str, call: Any, *args: Any) -> None:
    before = (project / "project.json").read_bytes(), (project / "spec.md").read_bytes()
    with pytest.raises(WorkspaceError, match=fragment):
        call(project, *args)
    assert ((project / "project.json").read_bytes(), (project / "spec.md").read_bytes()) == before


# --- spec and planning: the confirm action -----------------------------------------------------


@pytest.mark.parametrize("kind", ["requirements", "planning", "alignment"])
def test_a_confirmation_needs_a_round(gated: None, project: Path, kind: str) -> None:
    refused(project, "goldfish gate '(spec|planning)': no round covers", confirm, kind)


def test_a_confirmation_passes_with_an_accepted_round(gated: None, project: Path) -> None:
    accept(project, "spec")
    assert confirm(project, "requirements")["complete"] is True
    accept(project, "planning")
    assert confirm(project, "planning", "R1")["complete"] is True


def test_an_alignment_needs_both_rounds(gated: None, project: Path) -> None:
    accept(project, "spec")
    refused(project, "goldfish gate 'planning'", confirm, "alignment")
    accept(project, "planning")
    assert confirm(project, "alignment")["complete"] is True


def test_a_round_goes_stale_when_the_text_changes(gated: None, project: Path) -> None:
    accept(project, "planning")
    edit_planning(project)
    refused(project, r"covers version [0-9a-f]{12}, but the planning document is now", confirm, "planning")


def test_a_decision_in_the_spec_does_not_make_a_round_stale(gated: None, project: Path) -> None:
    accept(project, "spec")
    revision = load_state(project)["revision"]
    spec = snapshot(project / "spec.md")[1]
    workflow(project, "round", {
        "id": "decide-1", "expected_revision": revision, "tokens": {"spec": spec},
        "decisions": [{"id": "an-answer", "body": "The user chose the first option."}],
    })
    assert confirm(project, "requirements")["complete"] is True


def test_a_later_open_round_closes_the_gate_even_after_an_accepted_one(gated: None, project: Path) -> None:
    accept(project, "planning")
    open_round(project, "planning", ("combined",))
    refused(project, r"the latest round \(02, open\) is not accepted", confirm, "planning")


def test_a_waiver_passes_the_gate(gated: None, project: Path) -> None:
    refused(project, "goldfish gate 'spec'", confirm, "requirements")
    gold(project, operation="waive", target="spec", reason="no subagent in this host", confirmation=CONFIRMATION)
    assert confirm(project, "requirements")["complete"] is True


def test_the_gates_are_off_before_the_instant(project: Path) -> None:
    assert workspace_lib.GOLDFISH_FROM == workspace_lib.GOLDFISH_PLACEHOLDER
    assert workspace_lib.is_goldfish_gated() is False
    assert confirm(project, "requirements")["complete"] is True
    assert confirm_gate_errors(project, ("requirements",), "text", "text") == []


def test_the_shipped_instant_is_a_real_one_in_the_past() -> None:
    """The suite holds the instant in the future (conftest); the source must ship a real, readable instant."""
    import re
    from datetime import datetime

    source = (Path(workspace_lib.__file__)).read_text(encoding="utf-8")
    match = re.search(r'^GOLDFISH_FROM = "([^"]+)"$', source, re.MULTILINE)
    assert match is not None
    shipped = datetime.fromisoformat(match.group(1))
    assert match.group(1) != workspace_lib.GOLDFISH_PLACEHOLDER
    assert shipped.tzinfo is not None and shipped <= datetime.now().astimezone()
    assert shipped >= datetime.fromisoformat("2026-10-27T00:00:00+00:00"), "the 0.31.0 release is after this date"


def test_the_instant_is_read_when_the_gate_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", "2999-01-01T00:00:00+00:00")
    assert workspace_lib.is_goldfish_gated() is False
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", PAST)
    assert workspace_lib.is_goldfish_gated() is True


# --- the design: the first code task ------------------------------------------------------------


def state_of(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": list(tasks)}


def code(task_id: str = "T01", status: str = "TODO", **fields: Any) -> dict[str, Any]:
    return {**CODE_TASK, "id": task_id, "status": status, **fields}


def prose(task_id: str = "T01", status: str = "TODO") -> dict[str, Any]:
    return {**CODE_TASK, "id": task_id, "status": status, "outputs": [{"root": "target", "path": "n.md"}]}


@pytest.mark.parametrize(
    ("previous", "candidate", "fires"),
    [
        (state_of(code()), state_of(code(status="RUNNING")), True),
        (state_of(code()), state_of(code(status="DONE")), True),
        (state_of(code(status="SKIPPED"), code("T02")), state_of(code(status="SKIPPED"), code("T02", "RUNNING")), True),
        (state_of(code(status="BLOCKED")), state_of(code(status="RUNNING")), True),
        (state_of(prose(status="RUNNING")), state_of(code(status="RUNNING")), True),
        (state_of(code("T01"), code("T02")), state_of(code("T01"), code("T02", "RUNNING")), True),
        (state_of(code(status="RUNNING")), state_of(code(status="RUNNING")), False),
        (state_of(code(status="DONE")), state_of(code(status="DONE"), code("T02", "RUNNING")), False),
        (
            state_of(code(status="BLOCKED", started_at="2026-10-27T10:00:00+00:00")),
            state_of(code(status="RUNNING")), False,
        ),
        (
            state_of(code(status="BLOCKED", evidence=[{"root": "workspace", "path": "evidence.md", "anchor": "x"}])),
            state_of(code(status="RUNNING")), False,
        ),
        (state_of(code()), state_of(code(status="BLOCKED")), False),
        (state_of(code()), state_of(code(status="SKIPPED")), False),
        (state_of(prose()), state_of(prose(status="RUNNING")), False),
        (state_of(), state_of(), False),
    ],
)
def test_the_design_gate_fires_on_the_first_code_task_to_run(
    gated: None, project: Path, previous: dict[str, Any], candidate: dict[str, Any], fires: bool
) -> None:
    errors = design_gate_errors(project, previous, candidate)
    assert bool(errors) is fires, errors
    if fires:
        assert errors[0].startswith("goldfish gate 'design': no round covers artifacts/architecture.md")


def test_the_design_gate_passes_with_a_current_round(gated: None, project: Path) -> None:
    previous, candidate = state_of(code()), state_of(code(status="RUNNING"))
    assert design_gate_errors(project, previous, candidate)
    accept(project, "design")
    assert design_gate_errors(project, previous, candidate) == []
    (project / "artifacts" / "architecture.md").write_text("# Design\n\n## 1. Purpose\n\nA new goal.\n")
    assert "artifacts/architecture.md is now" in design_gate_errors(project, previous, candidate)[0]


def test_a_design_that_is_missing_is_refused(gated: None, project: Path) -> None:
    (project / "artifacts" / "architecture.md").unlink()
    errors = design_gate_errors(project, state_of(code()), state_of(code(status="RUNNING")))
    assert errors == [
        "goldfish gate 'design': artifacts/architecture.md is missing; write the design and run a round first"
    ]


def test_the_design_gate_is_off_before_the_instant(project: Path) -> None:
    assert design_gate_errors(project, state_of(code()), state_of(code(status="RUNNING"))) == []


def to_planning(project: Path) -> None:
    update_project_data(
        project, {"status": "PLANNING", "tasks": [{**CODE_TASK, "name": "Code"}]},
        expected_revision=load_state(project)["revision"],
    )


def test_a_task_start_is_refused_until_the_design_has_a_round(
    gated: None, ungated_alignment: None, project: Path
) -> None:
    to_planning(project)
    revision = load_state(project)["revision"]
    with pytest.raises(WorkspaceError, match="goldfish gate 'design'"):
        task_operation(project, "start", "T01", expected_revision=revision)
    assert load_state(project)["tasks"][0]["status"] == "TODO"
    accept(project, "design")
    task_operation(project, "start", "T01", expected_revision=load_state(project)["revision"])
    assert load_state(project)["tasks"][0]["status"] == "RUNNING"


def test_a_plain_update_cannot_start_the_first_code_task(gated: None, ungated_alignment: None, project: Path) -> None:
    to_planning(project)
    revision = load_state(project)["revision"]
    with pytest.raises(WorkspaceError, match="goldfish gate 'design'"):
        update_project_data(project, {"tasks": [{"id": "T01", "status": "RUNNING"}]}, expected_revision=revision)


def test_a_refused_operation_writes_no_documents(gated: None, ungated_alignment: None, project: Path) -> None:
    to_planning(project)
    before = {name: (project / name).read_bytes() for name in ("spec.md", "project.json")}
    assert not (project / "handoff.md").exists()
    request = {
        "id": "reconcile-1", "expected_revision": load_state(project)["revision"],
        "patch": {"status": "EXECUTING", "tasks": [{"id": "T01", "status": "RUNNING"}]},
        "decision": "Start the first code task.", "continuation": {"next": "Run the first code task."},
        "tokens": {"spec": snapshot(project / "spec.md")[1]},
    }
    with pytest.raises(WorkspaceError, match="goldfish gate 'design'"):
        workflow(project, "reconcile", request)
    assert {name: (project / name).read_bytes() for name in before} == before
    assert not (project / "handoff.md").exists()


# --- the diff: release, merge and closure -------------------------------------------------------


@pytest.fixture
def delivered(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A project with a code task, a remote, and a worktree whose branch is pushed (as far as Git can tell)."""
    repository = make_repository(tmp_path)
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    fetched(repository)
    linked = add_worktree(repository)
    path = make_project(tmp_path, repository)
    record(path, linked)
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    return path, linked, repository


def commit_all(linked: Path, message: str = "work") -> str:
    git(linked, "add", "-A")
    git(linked, "commit", "-q", "-m", message)
    return git(linked, "rev-parse", "HEAD")


def push(linked: Path) -> None:
    """What `git push -u` leaves behind: a remote-tracking ref at HEAD, and the upstream setting."""
    git(linked, "update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
    git(linked, "branch", f"--set-upstream-to=origin/{BRANCH}")


def diff_round(project: Path, linked: Path, **fields: Any) -> dict[str, Any]:
    opened = open_round(project, "diff", ("combined",), worktree=str(linked), **fields)
    return close(project, opened, reports=report(("combined",)))


def refuse_release(project: Path, linked: Path, fragment: str) -> None:
    with pytest.raises(WorkspaceError, match=fragment):
        op(project, linked, "release", version="0.24.0", files=FILES)
    assert "version_line" not in load_state(project)["worktrees"][0]


def prepare_release(project: Path, linked: Path) -> str:
    write_versions(linked, "0.24.0", None)
    head = commit_all(linked, "release 0.24.0")
    push(linked)
    return head


def test_a_release_needs_a_diff_round(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    prepare_release(project, linked)
    refuse_release(project, linked, "goldfish gate 'diff': no round covers the diff version")
    diff_round(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)
    assert load_state(project)["worktrees"][0]["version_line"]["release"]["version"] == "0.24.0"


def test_a_commit_after_the_round_makes_it_stale(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prepare_release(project, linked)
    diff_round(project, linked)
    (linked / "more.txt").write_text("x\n", encoding="utf-8")
    commit_all(linked, "after the review")
    push(linked)
    refuse_release(project, linked, "covers version [0-9a-f]{12}, but the diff is now")


def test_a_release_needs_a_clean_tree_on_the_recorded_branch(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prepare_release(project, linked)
    diff_round(project, linked)
    (linked / "scratch.txt").write_text("x\n", encoding="utf-8")
    refuse_release(project, linked, r"uncommitted changes \(\?\? scratch.txt\)")
    (linked / "scratch.txt").unlink()
    git(linked, "checkout", "-q", "--detach")
    refuse_release(project, linked, r"is on branch '\(detached\)'")


def test_a_pushed_release_needs_its_upstream_at_head(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    write_versions(linked, "0.24.0", None)
    commit_all(linked, "release 0.24.0")
    diff_round(project, linked)
    refuse_release(project, linked, f"branch {BRANCH} has no upstream; push it with -u first")
    git(linked, "update-ref", "refs/remotes/origin/other", "HEAD~1")
    git(linked, "branch", "--set-upstream-to=origin/other")
    refuse_release(project, linked, "does not track a branch named")
    git(linked, "update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD~1")
    git(linked, "branch", f"--set-upstream-to=origin/{BRANCH}")
    refuse_release(project, linked, "differs from HEAD; push first")
    push(linked)
    op(project, linked, "release", version="0.24.0", files=FILES)


def test_a_second_release_needs_its_own_round(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prepare_release(project, linked)
    diff_round(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)
    write_versions(linked, "0.24.0", 2)
    commit_all(linked, "alpha again")
    push(linked)
    op(project, linked, "alpha", base="0.24.0", alpha=2, reason="fix after review")
    write_versions(linked, "0.24.0", None)
    commit_all(linked, "release again")
    push(linked)
    with pytest.raises(WorkspaceError, match="goldfish gate 'diff'"):
        op(project, linked, "release", version="0.24.0")


def test_a_waiver_replaces_the_round_but_not_the_checks(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prepare_release(project, linked)
    refuse_release(project, linked, "goldfish gate 'diff'")
    gold(
        project, operation="waive", target="diff", worktree=str(linked), reason="no subagent",
        confirmation=CONFIRMATION,
    )
    (linked / "scratch.txt").write_text("x\n", encoding="utf-8")
    refuse_release(project, linked, "uncommitted changes")
    (linked / "scratch.txt").unlink()
    op(project, linked, "release", version="0.24.0", files=FILES)


def test_a_project_without_a_code_task_has_no_diff_gate(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prose_task = {"id": "T01", "outputs": [{"root": "target", "path": "notes.md", "required": True}]}
    update_project_data(project, {"tasks": [prose_task]}, expected_revision=load_state(project)["revision"])
    prepare_release(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)


def test_the_diff_gate_is_off_before_the_instant(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    prepare_release(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)


def merge_claim(project: Path, linked: Path, **fields: Any) -> dict[str, Any]:
    return op(project, linked, "merge", merge_state="merged", method="squash", evidence="commit abc", **fields)


def released(project: Path, linked: Path) -> str:
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    head = prepare_release(project, linked)
    diff_round(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)
    return head


def test_a_merge_record_names_the_head_that_was_merged(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    head = released(project, linked)
    with pytest.raises(WorkspaceError, match="needs merged_head"):
        merge_claim(project, linked)
    for claim, fragment in (("abc", "full lowercase hex"), ("f" * 40, "is not in repository")):
        with pytest.raises(WorkspaceError, match=fragment):
            merge_claim(project, linked, merged_head=claim)
    git(linked, "checkout", "-q", "--detach")  # the host merge moved the checkout; the claim is still checked
    merge_claim(project, linked, merged_head=head)
    assert load_state(project)["worktrees"][0]["merge"]["state"] == "merged"


def test_a_merged_head_must_be_the_reviewed_one(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    released(project, linked)
    (linked / "host-edit.txt").write_text("x\n", encoding="utf-8")
    other = commit_all(linked, "added on the host")
    with pytest.raises(WorkspaceError, match=r"covers version [0-9a-f]{12}, but the diff is now"):
        merge_claim(project, linked, merged_head=other)
    assert "merge" not in load_state(project)["worktrees"][0]


def test_a_host_merge_before_any_round_is_reviewed_by_commit(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    head = prepare_release(project, linked)
    git(linked, "checkout", "-q", "--detach")
    with pytest.raises(WorkspaceError, match="goldfish gate 'diff'"):
        merge_claim(project, linked, merged_head=head)
    pinned = open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)
    close(project, pinned, reports=report(("combined",)))
    merge_claim(project, linked, merged_head=head)


def test_a_declined_merge_is_not_gated(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    op(project, linked, "merge", merge_state="declined")
    assert load_state(project)["worktrees"][0]["merge"]["state"] == "declined"


def no_remote_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = make_repository(tmp_path)
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    record(project, linked)
    update_project_data(project, {"tasks": [CODE_TASK]}, expected_revision=load_state(project)["revision"])
    op(project, linked, "pull_request", exempt="no_remote")
    return project, linked, repository


def close_worktree(project: Path, linked: Path, decision: str) -> dict[str, Any]:
    return op(project, linked, "close", decision=decision)


@pytest.mark.parametrize("decision", ["keep", "accept_dirty", "remove"])
def test_closing_a_no_remote_worktree_needs_a_round(gated: None, tmp_path: Path, decision: str) -> None:
    project, linked, repository = no_remote_worktree(tmp_path)
    if decision == "accept_dirty":
        (linked / "scratch.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="goldfish gate 'diff': no round covers"):
        close_worktree(project, linked, decision)
    assert load_state(project)["worktrees"][0]["status"] == "active"
    if decision == "accept_dirty":
        (linked / "scratch.txt").unlink()
        git(linked, "checkout", "-q", "--detach")  # the round covers the branch, wherever the checkout is
    diff_round_for_branch(project, linked, repository)
    if decision == "accept_dirty":
        (linked / "scratch.txt").write_text("x\n", encoding="utf-8")
    if decision == "remove":
        git(repository, "worktree", "remove", "--force", str(linked))
    close_worktree(project, linked, decision)
    assert load_state(project)["worktrees"][0]["status"] == ("removed" if decision == "remove" else "kept")


def diff_round_for_branch(project: Path, linked: Path, repository: Path) -> None:
    """A round pinned to the branch head, as a reviewer of a checkout that has moved would open it."""
    head = git(repository, "rev-parse", f"refs/heads/{BRANCH}")
    pinned = open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)
    close(project, pinned, reports=report(("combined",)))


def test_a_second_closure_of_a_kept_worktree_is_not_gated_again(gated: None, tmp_path: Path) -> None:
    project, linked, repository = no_remote_worktree(tmp_path)
    diff_round_for_branch(project, linked, repository)
    close_worktree(project, linked, "keep")
    (linked / "more.txt").write_text("x\n", encoding="utf-8")
    commit_all(linked, "after the closure")
    close_worktree(project, linked, "keep")


def test_closing_a_worktree_with_a_pull_request_is_not_gated(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    close_worktree(project, linked, "keep")
    assert load_state(project)["worktrees"][0]["status"] == "kept"


def test_a_closure_names_a_branch_that_exists(gated: None, tmp_path: Path) -> None:
    project, _, _ = no_remote_worktree(tmp_path)
    entry = {**load_state(project)["worktrees"][0], "branch": "no-such-branch"}
    errors = delivery_gate_errors(project, load_state(project), entry, "closure", {})
    assert errors == [
        f"goldfish gate 'diff': branch no-such-branch no longer exists in {entry['repository']} and no round has passed"
    ]
    assert delivery_gate_errors(project, load_state(project), {**entry, "repository": None}, "closure", {}) == []


# --- reporting ---------------------------------------------------------------------------------


def test_the_findings_name_an_open_round_and_a_pending_re_audit(project: Path) -> None:
    assert goldfish_findings(project) == []
    opened = open_round(project, "planning", ("combined",))
    assert goldfish_findings(project) == ["Goldfish round 01 (planning) is open; close it or open a new round"]
    edit_planning(project)
    close(project, opened, [finding("F1", "Major", "FIX")], reports=report(("combined",)))
    assert goldfish_findings(project) == ["Goldfish round 01 (planning) needs its re-audit; open a round"]


def test_the_findings_report_a_spec_or_plan_that_changed_after_its_round(project: Path) -> None:
    accept(project, "spec")
    accept(project, "planning")
    accept(project, "design")  # a design that changes with every code task is not reported
    assert goldfish_findings(project) == []
    edit_planning(project)
    (project / "artifacts" / "architecture.md").write_text("# Design\n\nchanged\n", encoding="utf-8")
    assert goldfish_findings(project) == [
        "Goldfish round 02 covers an earlier version of the planning document; "
        "the next confirmation needs a delta round"
    ]
    (project / "planning.md").unlink()
    assert goldfish_findings(project) == []


def test_a_round_that_changed_the_spec_is_reported(project: Path) -> None:
    accept(project, "spec")
    path = project / "spec.md"
    text = path.read_text(encoding="utf-8")
    assert "Content for In scope." in text
    path.write_text(text.replace("Content for In scope.", "Different content for In scope."), encoding="utf-8")
    assert goldfish_findings(project) == [
        "Goldfish round 01 covers an earlier version of spec.md; the next confirmation needs a delta round"
    ]


def test_a_round_result_carries_the_findings(project: Path) -> None:
    open_round(project, "planning", ("combined",))
    revision = load_state(project)["revision"]
    result = workflow(project, "round", {
        "id": "round-note", "expected_revision": revision,
        "decisions": [{"id": "a-note", "body": "A decision."}], "tokens": {"spec": snapshot(project / "spec.md")[1]},
    })
    assert result["goldfish_findings"] == ["Goldfish round 01 (planning) is open; close it or open a new round"]


ENTRY = "<!-- research-entry: {i} -->\n<!-- research-body-sha256: x -->\n<a id=\"{i}\"></a>\n### {stamp} — {i}\n\n"


def confirmation_entry(kind: str, sha: str, stamp: str) -> str:
    marker = f"<!-- research-proposal: kind={kind} review_id=R1 content_sha256={sha} -->\n"
    return "\n" + ENTRY.format(i=f"c-{kind}", stamp=stamp) + marker


def test_a_confirmation_after_the_instant_with_no_round_is_reported(gated: None, project: Path) -> None:
    sha = "a" * 64
    path = project / "spec.md"
    for kind, stamp in (("requirements", "2026-11-01T10:00:00+00:00"), ("planning", "1999-01-01T00:00:00+00:00")):
        path.write_text(path.read_text(encoding="utf-8") + confirmation_entry(kind, sha, stamp), encoding="utf-8")
    state = load_state(project)
    assert goldfish_event_warnings(project, state) == [
        "the spec confirmation R1 was recorded at 2026-11-01T10:00:00+00:00 with no Goldfish round for that version"
    ]
    # With a round that covers that digest the confirmation is accounted for.
    digest = workspace_lib.proposal_digest("requirements", path.read_text(encoding="utf-8"))
    path.write_text(path.read_text(encoding="utf-8").replace(sha, digest, 1), encoding="utf-8")
    accept(project, "spec")
    assert goldfish_event_warnings(project, state) == []


def test_a_marker_without_a_heading_or_a_readable_time_is_not_reported(gated: None, project: Path) -> None:
    path = project / "spec.md"
    marker = f"\n<!-- research-proposal: kind=requirements review_id=R1 content_sha256={'b' * 64} -->\n"
    path.write_text(path.read_text(encoding="utf-8") + marker, encoding="utf-8")
    assert goldfish_event_warnings(project, load_state(project)) == []
    broken = confirmation_entry("planning", "c" * 64, "2026-13-99T99:99:99Z")
    path.write_text(path.read_text(encoding="utf-8") + broken, encoding="utf-8")
    assert goldfish_event_warnings(project, load_state(project)) == []
    naive = confirmation_entry("planning", "c" * 64, "2026-11-01T10:00:00")
    path.write_text(path.read_text(encoding="utf-8") + naive, encoding="utf-8")
    assert goldfish_event_warnings(project, load_state(project)) == []


def test_a_code_task_started_after_the_instant_with_no_design_round_is_reported(gated: None, project: Path) -> None:
    started = code("T01", "RUNNING", started_at="2026-11-01T10:00:00+00:00")
    early = code("T02", "RUNNING", started_at="1999-01-01T00:00:00+00:00")
    state = {"tasks": [started, early, prose("T03", "RUNNING")]}
    assert goldfish_event_warnings(project, state) == [
        "code task T01 started at 2026-11-01T10:00:00+00:00 with no design round"
    ]
    accept(project, "design")
    assert goldfish_event_warnings(project, state) == []


def test_release_merge_and_closure_events_with_no_round_are_reported(gated: None, project: Path) -> None:
    later = "2026-11-01T10:00:00+00:00"
    entry = {
        "path": "/w", "version_line": {"release": {"version": "1.0.0", "recorded_at": later}},
        "merge": {"state": "merged", "recorded_at": later}, "pull_request": {"number": 1},
        "closure": {"observed_at": later},
    }
    state = {"tasks": [code()], "worktrees": [entry]}
    assert goldfish_event_warnings(project, state) == [
        f"the release of /w at {later} has no Goldfish diff round",
        f"the merge of /w at {later} has no Goldfish diff round",
    ]
    unmerged = {"path": "/w", "pull_request": {"number": 1}, "closure": {"observed_at": later}}
    assert goldfish_event_warnings(project, {"tasks": [code()], "worktrees": [unmerged]}) == [
        f"worktree /w was closed at {later} with no merge recorded"
    ]
    declined = {**unmerged, "merge": {"state": "declined", "recorded_at": later}}
    assert goldfish_event_warnings(project, {"tasks": [code()], "worktrees": [declined]}) == []
    early = {**unmerged, "closure": {"observed_at": "1999-01-01T00:00:00+00:00"}}
    assert goldfish_event_warnings(project, {"tasks": [code()], "worktrees": [early]}) == []
    assert goldfish_event_warnings(project, {"tasks": [prose()], "worktrees": [entry]}) == []


def test_the_events_are_not_reported_before_the_instant(project: Path) -> None:
    state = {"tasks": [code("T01", "RUNNING", started_at="2026-11-01T10:00:00+00:00")]}
    assert goldfish_event_warnings(project, state) == []


def test_validation_carries_the_warnings_and_never_an_error(gated: None, project: Path) -> None:
    open_round(project, "planning", ("combined",))
    report_ = workspace_lib.validate_project(project)
    assert report_.valid, report_.errors
    assert "Goldfish round 01 (planning) is open; close it or open a new round" in report_.warnings
    assert goldfish.goldfish_warnings(project, load_state(project)) == [
        "Goldfish round 01 (planning) is open; close it or open a new round"
    ]
    revision = json.loads((project / "project.json").read_text(encoding="utf-8"))["revision"]
    assert revision == load_state(project)["revision"]
    assert len(rounds(project)) == 1


# --- what the reviews of the diff found -------------------------------------------------------------


def test_the_instant_comparison_includes_the_boundary() -> None:
    at = "2026-01-01T00:00:00+00:00"
    assert workspace_lib.at_or_after(at, at) is True
    assert workspace_lib.at_or_after("2025-12-31T23:59:59+00:00", at) is False
    assert workspace_lib.at_or_after("2026-01-01T00:00:00", at) is False  # naive
    assert workspace_lib.at_or_after("not a time", at) is False
    assert workspace_lib.at_or_after(None, at) is False
    assert workspace_lib.at_or_after("2026-01-01T00:00:00Z", at) is True


def test_a_closure_whose_branch_is_gone_stands_on_the_last_passed_round(gated: None, tmp_path: Path) -> None:
    project, linked, repository = no_remote_worktree(tmp_path)
    git(repository, "worktree", "remove", "--force", str(linked))
    git(repository, "branch", "-D", BRANCH)
    with pytest.raises(WorkspaceError, match=r"no longer exists in .* and no round has passed"):
        close_worktree(project, linked, "remove")
    assert load_state(project)["worktrees"][0]["status"] == "active"
    gold(
        project, operation="waive", target="diff", worktree=str(linked), commit=git(repository, "rev-parse", "main"),
        reason="the branch was merged locally and deleted", confirmation=CONFIRMATION,
    )
    close_worktree(project, linked, "remove")
    assert load_state(project)["worktrees"][0]["status"] == "removed"


def test_a_kept_worktree_that_gained_commits_needs_a_new_round_to_be_removed(gated: None, tmp_path: Path) -> None:
    project, linked, repository = no_remote_worktree(tmp_path)
    diff_round_for_branch(project, linked, repository)
    close_worktree(project, linked, "keep")
    (linked / "more.txt").write_text("x\n", encoding="utf-8")
    commit_all(linked, "work after the closure")
    git(repository, "worktree", "remove", "--force", str(linked))
    with pytest.raises(WorkspaceError, match=r"covers version [0-9a-f]{12}, but the diff is now"):
        close_worktree(project, linked, "remove")
    assert load_state(project)["worktrees"][0]["status"] == "kept"


def test_a_merged_head_must_be_the_head_of_the_branch(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    reviewed = released(project, linked)
    (linked / "later.txt").write_text("x\n", encoding="utf-8")
    commit_all(linked, "committed after the release")
    with pytest.raises(WorkspaceError, match=f"merged_head {reviewed[:12]} is not the head of branch {BRANCH}"):
        merge_claim(project, linked, merged_head=reviewed)
    assert "merge" not in load_state(project)["worktrees"][0]


def test_hand_edited_records_do_not_crash_the_reporting_or_the_gates(gated: None, project: Path) -> None:
    junk = {"tasks": None, "worktrees": ["x", 5, {"path": "/w", "merge": "x", "version_line": "x", "closure": 5}]}
    assert goldfish_event_warnings(project, junk) == []
    assert goldfish_event_warnings(project, {"tasks": "x", "worktrees": "x"}) == []
    assert design_gate_errors(project, {"tasks": None}, {"tasks": "x"}) == []
    assert delivery_gate_errors(project, {"tasks": ["x"]}, {"repository": "/r", "path": "/w"}, "release", {}) == []
    state = load_state(project)
    state["worktrees"] = "x"
    (project / "project.json").write_text(json.dumps(state), encoding="utf-8")
    assert isinstance(workspace_lib.validate_project(project).errors, list)  # reports; does not raise


def test_resume_and_readiness_carry_a_goldfish_warning(gated: None, project: Path) -> None:
    open_round(project, "planning", ("combined",))
    warning = "Goldfish round 01 (planning) is open; close it or open a new round"
    assert warning in json.dumps(workflow(project, "readiness", {}))
    assert warning in json.dumps(workflow(project, "resume", {}))
