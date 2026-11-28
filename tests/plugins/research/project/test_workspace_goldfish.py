"""c1: a merge after fix commits. The diff gate accepts a merged head ahead of the reviewed head when the
request lists every commit in between, and the tool checks the list by ancestry."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import workspace_goldfish as goldfish
import workspace_lib
from workspace_delivery import delivery_errors
from workspace_goldfish import delivery_gate_errors
from workspace_lib import WorkspaceError
from workspace_session import update_project_data
from workspace_workflows import merge_requirements

from tests.plugins.research.project.test_delivery import MERGE, URL, entry_of, fetched, op, refuse
from tests.plugins.research.project.test_delivery_scenarios import entry
from tests.plugins.research.project.test_goldfish import (
    CODE_TASK,
    CONFIRMATION,
    DESIGN_BODY,
    PLANNING_BODY,
    close,
    finding,
    gold,
    open_round,
    report,
)
from tests.plugins.research.project.test_goldfish_gates import BRANCH, PAST, commit_all, merge_claim, released
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    git,
    load_state,
    make_project,
    make_repository,
    record,
)

GATE = "goldfish gate 'diff':"
HINT = "If later commits only fix its findings, list them in fix_commits."


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", PAST)


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


def fix(linked: Path, name: str) -> str:
    (linked / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
    return commit_all(linked, f"fix {name}")


def reviewed_then_fixed(delivered: tuple[Path, Path, Path], count: int = 2) -> tuple[Path, Path, str, list[str]]:
    """A passed diff round at the release head, then `count` fix commits on the branch."""
    project, linked, _ = delivered
    reviewed = released(project, linked)
    return project, linked, reviewed, [fix(linked, f"f{number}") for number in range(count)]


def refused_merge(project: Path, linked: Path, fragment: str, **fields: Any) -> None:
    with pytest.raises(WorkspaceError, match=fragment):
        merge_claim(project, linked, **fields)
    assert "merge" not in load_state(project)["worktrees"][0]


def side_commit(linked: Path, parent: str, name: str) -> str:
    """A commit on `parent` that the branch does not contain; the checkout returns to the branch."""
    git(linked, "checkout", "-q", "--detach", parent)
    made = fix(linked, name)
    git(linked, "checkout", "-q", BRANCH)
    return made


def test_a_merge_after_fix_commits_is_recorded_with_the_list(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _, fixes = reviewed_then_fixed(delivered)
    merge_claim(project, linked, merged_head=fixes[-1], fix_commits=fixes)
    merge = load_state(project)["worktrees"][0]["merge"]
    assert (merge["state"], merge["merged_head"], merge["fix_commits"]) == ("merged", fixes[-1], fixes)


def test_an_accepted_residual_round_also_serves(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    blocker = finding("F1", "Blocker", "FIX", artifact="spec.md")
    first = open_round(project, "diff", ("combined",), worktree=str(linked))
    close(project, first, [blocker], reports=report(("combined",)))
    again = open_round(project, "diff", ("combined",), worktree=str(linked))
    closed = close(project, again, [blocker], reports=report(("combined",)), confirmation=CONFIRMATION)
    assert closed["outcome"] == "accepted-residual"
    later = fix(linked, "later")
    merge_claim(project, linked, merged_head=later, fix_commits=[later])


def test_the_stale_round_error_hints_at_fix_commits(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered)
    refused_merge(project, linked, rf"covers version {reviewed[:12]}, but the diff is now {fixes[-1][:12]}.*{HINT}$",
                  merged_head=fixes[-1])


def test_no_hint_when_the_round_is_not_an_ancestor(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    base = git(linked, "rev-parse", "HEAD")
    elsewhere = side_commit(linked, base, "elsewhere")
    close(project, open_round(project, "diff", ("combined",), worktree=str(linked), commit=elsewhere),
          reports=report(("combined",)))
    head = fix(linked, "mine")
    with pytest.raises(WorkspaceError) as caught:
        merge_claim(project, linked, merged_head=head)
    assert "covers version" in str(caught.value)
    assert HINT not in str(caught.value)


def test_the_readiness_listing_shows_the_hint(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, _, _, fixes = reviewed_then_fixed(delivered)
    [item] = merge_requirements(project, load_state(project))
    assert item["diff_round"]["errors"][-1].endswith(HINT)
    assert fixes[-1] == item["merged_head"]["branch_head"]


def test_a_waiver_is_refused(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    gold(project, operation="waive", target="diff", worktree=str(linked), reason="no subagent",
         confirmation=CONFIRMATION)
    later = fix(linked, "later")
    refused_merge(project, linked, rf"{GATE} fix_commits needs the latest round \(01\) to be accepted or "
                  r"accepted-residual, not waived", merged_head=later, fix_commits=[later])


def test_a_round_that_is_not_accepted_is_refused(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    open_round(project, "diff", ("combined",), worktree=str(linked))
    later = fix(linked, "later")
    refused_merge(project, linked, r"to be accepted or accepted-residual, not open", merged_head=later,
                  fix_commits=[later])


def test_no_round_at_all_keeps_the_existing_message(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    later = fix(linked, "later")
    refused_merge(project, linked, rf"{GATE} no round covers the diff version", merged_head=later,
                  fix_commits=[later])


def test_a_list_with_the_reviewed_head_as_merged_head_is_refused(
    gated: None, delivered: tuple[Path, Path, Path]
) -> None:
    project, linked, _ = delivered
    reviewed = released(project, linked)
    refused_merge(project, linked, rf"{GATE} fix_commits needs a merged_head ahead of the reviewed head "
                  rf"{reviewed[:12]}$", merged_head=reviewed, fix_commits=[reviewed])
    merge_claim(project, linked, merged_head=reviewed)  # without the list the existing path stands


def test_a_merged_head_that_does_not_build_on_the_reviewed_head_is_refused(
    gated: None, delivered: tuple[Path, Path, Path]
) -> None:
    project, linked, _ = delivered
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    elsewhere = side_commit(linked, git(linked, "rev-parse", "HEAD"), "elsewhere")
    close(project, open_round(project, "diff", ("combined",), worktree=str(linked), commit=elsewhere),
          reports=report(("combined",)))
    head = fix(linked, "mine")
    refused_merge(project, linked, rf"{GATE} merged_head {head[:12]} does not descend from the reviewed head "
                  rf"{elsewhere[:12]} of round 01", merged_head=head, fix_commits=[head])


def test_an_empty_list_is_refused_by_the_gate(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, _, _, fixes = reviewed_then_fixed(delivered)
    state, worktree = load_state(project), load_state(project)["worktrees"][0]
    request = {"merged_head": fixes[-1], "fix_commits": []}
    assert delivery_gate_errors(project, state, worktree, "merge", request) == [
        f"{GATE} fix_commits is empty; list the commits made after the reviewed head or merge without it"
    ]


def test_a_commit_outside_the_reviewed_line_is_refused(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered)
    base = git(linked, "rev-parse", f"{reviewed}~1")
    elsewhere = side_commit(linked, base, "elsewhere")
    refused_merge(project, linked, rf"{GATE} commit {elsewhere[:12]} is not a descendant of the reviewed head "
                  rf"{reviewed[:12]} of round 01$", merged_head=fixes[-1], fix_commits=[elsewhere, *fixes])


def test_the_reviewed_head_itself_is_not_a_fix_commit(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered)
    refused_merge(project, linked, rf"commit {reviewed[:12]} is not a descendant of the reviewed head",
                  merged_head=fixes[-1], fix_commits=[reviewed, *fixes])


def test_a_commit_that_the_merged_head_does_not_contain_is_refused(
    gated: None, delivered: tuple[Path, Path, Path]
) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered)
    aside = side_commit(linked, reviewed, "aside")
    refused_merge(project, linked, rf"{GATE} commit {aside[:12]} is not an ancestor of merged_head "
                  rf"{fixes[-1][:12]}$", merged_head=fixes[-1], fix_commits=[aside, *fixes])


def test_a_commit_missing_from_the_repository_is_refused(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _, fixes = reviewed_then_fixed(delivered)
    refused_merge(project, linked, rf"{GATE} commit {'f' * 40} is not in repository", merged_head=fixes[-1],
                  fix_commits=["f" * 40, *fixes])


def test_an_unlisted_commit_between_the_heads_is_refused(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered, count=3)
    refused_merge(project, linked, rf"{GATE} commit {fixes[0][:12]} lies between {reviewed[:12]} and merged_head "
                  rf"{fixes[-1][:12]} and is not listed in fix_commits \(2 unlisted\)$",
                  merged_head=fixes[-1], fix_commits=[fixes[-1]])


def test_the_listing_order_does_not_matter(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _, fixes = reviewed_then_fixed(delivered, count=3)
    merge_claim(project, linked, merged_head=fixes[-1], fix_commits=list(reversed(fixes)))


def test_a_merge_commit_from_main_is_unlisted(gated: None, delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    reviewed = released(project, linked)
    fixed = fix(linked, "fixed")
    other = side_commit(linked, reviewed, "main-work")
    git(linked, "merge", "-q", "--no-ff", "-m", "bring main in", other)
    merged = git(linked, "rev-parse", "HEAD")
    refused_merge(project, linked, rf"commit {merged[:12]} lies between", merged_head=merged,
                  fix_commits=[fixed, other])


def test_git_that_cannot_list_the_range_is_an_error(
    gated: None, delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, linked, reviewed, fixes = reviewed_then_fixed(delivered)
    real = goldfish._query

    def refusing(root: Path, *arguments: str) -> "str | None":
        return None if arguments[0] == "rev-list" else real(root, *arguments)

    monkeypatch.setattr(goldfish, "_query", refusing)
    refused_merge(project, linked, rf"{GATE} git could not list the commits between {reviewed[:12]} and "
                  rf"merged_head {fixes[-1][:12]}$", merged_head=fixes[-1], fix_commits=fixes)


def test_a_local_target_stores_the_list_without_checking_it(
    gated: None, delivered: tuple[Path, Path, Path]
) -> None:
    project, _, _, fixes = reviewed_then_fixed(delivered)
    worktree = load_state(project)["worktrees"][0]
    state: dict[str, Any] = {**load_state(project), "delivery_target": "local", "policy_version": 3}
    request = {"merged_head": fixes[-1], "fix_commits": ["0" * 40]}
    assert delivery_gate_errors(project, state, worktree, "merge", request) == []


# --- the shape check and the validator ---------------------------------------------------------------


@pytest.fixture
def local(tmp_path: Path) -> tuple[Path, Path]:
    repository = make_repository(tmp_path)
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    record(project, linked)
    op(project, linked, "pull_request", exempt="local_only")
    return project, linked


HEAD = "a" * 40
SECOND = "b" * 64


def merged(project: Path, linked: Path, **fields: Any) -> dict[str, Any]:
    return op(project, linked, "merge", merge_state="merged", method="merge", evidence="abc", **fields)


def test_the_list_is_stored_when_nothing_is_gated(local: tuple[Path, Path]) -> None:
    project, linked = local
    merged(project, linked, merged_head=HEAD, fix_commits=[SECOND, HEAD])
    assert entry_of(project)["merge"]["fix_commits"] == [SECOND, HEAD]
    assert delivery_errors(entry_of(project), "w") == []


def test_a_merge_without_a_list_stores_none(local: tuple[Path, Path]) -> None:
    project, linked = local
    merged(project, linked, merged_head=HEAD, fix_commits=None)
    assert "fix_commits" not in entry_of(project)["merge"]


SHAPE = re.escape("fix_commits must be a non-empty list of full lowercase hex commits (40 or 64 digits)")


@pytest.mark.parametrize(
    "value", ["a" * 40, [], [HEAD, "abc"], [HEAD, "A" * 40], [HEAD, 5], [HEAD + "\n"], {"a": 1}, [HEAD, None]]
)
def test_a_malformed_list_is_refused_before_storing(local: tuple[Path, Path], value: Any) -> None:
    project, linked = local
    refuse(project, linked, SHAPE, "merge", merge_state="merged", method="merge", evidence="abc",
           merged_head=HEAD, fix_commits=value)


def test_a_list_needs_a_merged_head(local: tuple[Path, Path]) -> None:
    project, linked = local
    refuse(project, linked, "fix_commits needs merged_head, the head that was merged", "merge",
           merge_state="merged", method="merge", evidence="abc", fix_commits=[HEAD])


def test_a_declined_record_takes_no_list(local: tuple[Path, Path]) -> None:
    project, linked = local
    refuse(project, linked, "fix_commits belongs to a merged record, not a declined one", "merge",
           merge_state="declined", fix_commits=[HEAD])


GOOD = {**MERGE, "merged_head": HEAD}
PROBLEM = (
    "w.merge: fix_commits is a non-empty list of full lowercase hex commits "
    "and needs a merged record with merged_head"
)


def validated(merge: dict[str, Any]) -> list[str]:
    return delivery_errors({**entry(), "pr_exemption": {"reason": "local_only", "source": "u", "response": "r"},
                            "merge": merge}, "w")


def test_the_validator_accepts_a_good_list() -> None:
    assert validated({**GOOD, "fix_commits": [HEAD, SECOND]}) == []


@pytest.mark.parametrize(
    "merge",
    [
        {**GOOD, "fix_commits": []},
        {**GOOD, "fix_commits": "a" * 40},
        {**GOOD, "fix_commits": [HEAD, "abc"]},
        {**GOOD, "fix_commits": [HEAD, HEAD.upper()]},
        {**GOOD, "fix_commits": [HEAD + "\n"]},
        {**GOOD, "fix_commits": [HEAD, 3]},
        {**MERGE, "fix_commits": [HEAD]},
        {**MERGE, "state": "declined", "method": None, "evidence": None, "merged_head": HEAD, "fix_commits": [HEAD]},
    ],
)
def test_the_validator_reports_one_message_for_a_bad_list(merge: dict[str, Any]) -> None:
    assert [p for p in validated(merge) if "fix_commits" in p] == [PROBLEM]


@pytest.fixture
def lone(tmp_path: Path) -> Path:
    return make_project(tmp_path, make_repository(tmp_path))


def test_open_names_one_report_path_per_stage(lone: Path) -> None:
    opened = open_round(lone, "spec")
    assert opened["report_paths"] == {
        "comprehension": "reviews/goldfish_01_comprehension.md",
        "combined": "reviews/goldfish_01_combined.md",
    }
    assert not (lone / "reviews" / "goldfish_01_combined.md").exists()


def test_the_report_paths_follow_the_round_number_and_the_opened_stages(lone: Path) -> None:
    close(lone, open_round(lone, "spec"), [], report())
    second = open_round(lone, "spec", ("combined",))
    assert second["report_paths"] == {"combined": "reviews/goldfish_02_combined.md"}


def test_close_does_not_check_that_the_report_files_exist(lone: Path) -> None:
    opened = open_round(lone, "spec")
    assert close(lone, opened)["outcome"] == "accepted"
    assert sorted(path.name for path in (lone / "reviews").iterdir()) == ["goldfish_01.md"]


def test_a_report_file_does_not_count_as_a_round(lone: Path) -> None:
    opened = open_round(lone, "spec")
    (lone / "reviews" / "goldfish_01_combined.md").write_text("report", encoding="utf-8")
    assert [item.number for item in goldfish.round_documents(lone)] == [1]
    close(lone, opened)
    assert open_round(lone, "spec", ("combined",))["round"] == 2


def test_a_minor_finding_is_stored_as_a_nit(lone: Path) -> None:
    opened = open_round(lone, "spec")
    close(lone, opened, [finding("N1", "Minor", "DECLINE")])
    text = (lone / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")
    assert "- N1 (critic, Nit, section 2): A gap. Triage: DECLINE." in text
    assert "Minor" not in text


def test_a_minor_finding_may_be_declined_like_a_nit(lone: Path) -> None:
    assert close(lone, open_round(lone, "spec"), [finding("N1", "Minor", "DECLINE")])["outcome"] == "accepted"


def test_the_caller_dict_keeps_its_severity(lone: Path) -> None:
    item = finding("N1", "Minor", "DECLINE")
    assert goldfish._findings([item], lone)[0]["severity"] == "Nit"
    assert item["severity"] == "Minor"


def test_minor_is_the_only_alias(lone: Path) -> None:
    opened = open_round(lone, "spec")
    with pytest.raises(WorkspaceError, match="finding N1 severity must be one of Blocker, Major, Nit"):
        close(lone, opened, [finding("N1", "minor", "DECLINE")])


# --- c9: the head advisory on a pinned round ------------------------------------------------------


def pinned_round(delivered: tuple[Path, Path, Path]) -> tuple[Path, Path, str, dict[str, Any]]:
    project, linked, _ = delivered
    head = git(linked, "rev-parse", "HEAD")
    opened = open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)
    return project, linked, head, opened


def advisory(linked: Path, branch: str, head: str) -> str:
    return (
        f"worktree {linked.resolve()} is on branch {branch}, but records {BRANCH}; "
        f"the reviewed commit is {head[:12]}, so check that no reviewer moved HEAD"
    )


def test_a_pinned_open_on_the_recorded_branch_has_no_advisory(delivered: tuple[Path, Path, Path]) -> None:
    assert pinned_round(delivered)[3]["warnings"] == []


def test_a_pinned_open_on_a_detached_worktree_advises(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    git(linked, "checkout", "-q", "--detach")
    opened = open_round(project, "diff", ("combined",), worktree=str(linked), commit=git(linked, "rev-parse", "HEAD"))
    assert opened["warnings"] == [f"worktree {linked.resolve()} is detached"]


def test_a_pinned_open_on_another_branch_advises(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    head = git(linked, "rev-parse", "HEAD")
    git(linked, "checkout", "-q", "-b", "other")
    opened = open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)
    assert opened["warnings"] == [advisory(linked, "other", head)]
    assert opened["reviewed"] == head


def test_a_pinned_close_advises_when_the_head_moved(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, head, opened = pinned_round(delivered)
    git(linked, "checkout", "-q", "-b", "other")
    closed = close(project, opened, reports=report(("combined",)))
    assert closed["warnings"] == [advisory(linked, "other", head)]
    assert closed["outcome"] == "accepted"
    assert closed["resolved"] == head


def test_a_pinned_close_on_a_detached_worktree_advises(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _, opened = pinned_round(delivered)
    git(linked, "checkout", "-q", "--detach")
    assert close(project, opened, reports=report(("combined",)))["warnings"] == [
        f"worktree {linked.resolve()} is detached"
    ]


def test_a_pinned_close_on_the_recorded_branch_has_no_advisory(delivered: tuple[Path, Path, Path]) -> None:
    project, _, _, opened = pinned_round(delivered)
    assert close(project, opened, reports=report(("combined",)))["warnings"] == []


def test_a_missing_directory_gives_no_advisory(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, head, opened = pinned_round(delivered)
    shutil.rmtree(linked)
    assert close(project, opened, reports=report(("combined",)))["warnings"] == []
    again = open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)
    assert again["warnings"] == []


@pytest.mark.parametrize("observation", [{"available": False}, {"is_repository": False}])
def test_git_that_cannot_answer_gives_no_advisory(
    monkeypatch: pytest.MonkeyPatch, delivered: tuple[Path, Path, Path], observation: dict[str, bool]
) -> None:
    project, linked, head, opened = pinned_round(delivered)
    seen = {"exists": True, "available": True, "is_repository": True, "branch": ""} | observation
    monkeypatch.setattr(goldfish, "worktree_observation", lambda path: seen)
    assert close(project, opened, reports=report(("combined",)))["warnings"] == []
    assert open_round(project, "diff", ("combined",), worktree=str(linked), commit=head)["warnings"] == []


def test_a_pinned_close_survives_a_worktree_that_left_the_record(
    monkeypatch: pytest.MonkeyPatch, delivered: tuple[Path, Path, Path]
) -> None:
    project, linked, _, opened = pinned_round(delivered)
    git(linked, "checkout", "-q", "--detach")

    def gone(state: dict[str, Any], path: object) -> dict[str, Any]:
        raise WorkspaceError("worktree is not a recorded repository worktree")

    monkeypatch.setattr(goldfish, "worktree_entry", gone)
    assert close(project, opened, reports=report(("combined",)))["warnings"] == []


def test_an_unpinned_diff_round_is_still_refused_off_its_branch(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    opened = open_round(project, "diff", ("combined",), worktree=str(linked))
    assert opened["warnings"] == []
    git(linked, "checkout", "-q", "--detach")
    with pytest.raises(WorkspaceError, match=r"is on branch '\(detached\)', but records"):
        close(project, opened, reports=report(("combined",)))
    with pytest.raises(WorkspaceError, match=r"is on branch '\(detached\)', but records"):
        open_round(project, "diff", ("combined",), worktree=str(linked))


def test_a_document_round_carries_an_empty_warnings_list(lone: Path) -> None:
    opened = open_round(lone, "spec")
    assert opened["warnings"] == []
    assert close(lone, opened)["warnings"] == []


def test_the_persona_rules_forbid_moving_head() -> None:
    text = (Path(goldfish.__file__).parents[1] / "references" / "goldfish-personas.md").read_text(encoding="utf-8")
    assert "Run no `git checkout` or `git switch`: they move HEAD." in text


# --- c2: the round-count advisory ----------------------------------------------------------------


@pytest.fixture
def full(tmp_path: Path) -> tuple[Path, Path]:
    """A project with a code task, a recorded worktree and a planning and a design document."""
    repository = make_repository(tmp_path)
    linked = add_worktree(repository)
    path = make_project(tmp_path, repository)
    record(path, linked)
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text(DESIGN_BODY, encoding="utf-8")
    return path, Path(load_state(path)["worktrees"][0]["path"])


def advice(target: str, number: int, limit: int, where: str = "") -> str:
    return (
        f"round {number} on {target}{where}: from round {limit} "
        "ask the user whether to stop at accepted-residual"
    )


def open_rounds(project: Path, count: int, target: str = "spec", **fields: Any) -> list[dict[str, Any]]:
    return [open_round(project, target, ("combined",), **fields) for _ in range(count)]


@pytest.mark.parametrize("target", ["spec", "planning"])
def test_a_document_target_is_advised_from_its_fifth_round(full: tuple[Path, Path], target: str) -> None:
    opened = open_rounds(full[0], 5, target)
    assert [item["warnings"] for item in opened[:4]] == [[], [], [], []]
    assert opened[4]["warnings"] == [advice(target, 5, 5)]


def test_the_advice_continues_and_never_refuses(lone: Path) -> None:
    opened = open_rounds(lone, 7)
    assert [item["round"] for item in opened] == [1, 2, 3, 4, 5, 6, 7]
    assert opened[5]["warnings"] == [advice("spec", 6, 5)]
    assert opened[6]["warnings"] == [advice("spec", 7, 5)]


def test_the_count_is_per_target(full: tuple[Path, Path]) -> None:
    project, _ = full
    open_rounds(project, 4, "spec")
    assert open_round(project, "planning", ("combined",))["warnings"] == []
    assert open_round(project, "spec", ("combined",))["warnings"] == [advice("spec", 5, 5)]


def test_the_design_target_is_advised_from_its_fifth_round(full: tuple[Path, Path]) -> None:
    opened = open_rounds(full[0], 5, "design")
    assert opened[3]["warnings"] == []
    assert opened[4]["warnings"] == [advice("design", 5, 5)]


def test_a_diff_target_is_advised_from_its_eighth_round(full: tuple[Path, Path]) -> None:
    project, worktree = full
    opened = open_rounds(project, 8, "diff", worktree=str(worktree))
    assert [item["warnings"] for item in opened[:7]] == [[]] * 7
    where = f", worktree {worktree.resolve()}"
    assert opened[7]["warnings"] == [advice("diff", 8, 8, where)]


def test_a_diff_count_is_not_a_document_count(full: tuple[Path, Path]) -> None:
    project, worktree = full
    open_rounds(project, 7, "diff", worktree=str(worktree))
    assert open_round(project, "design", ("combined",))["warnings"] == []


def test_every_kind_of_round_counts(full: tuple[Path, Path]) -> None:
    project, _ = full
    first = open_round(project, "planning", ("combined",))
    (project / "planning.md").write_text(PLANNING_BODY + "Changed plan.\n", encoding="utf-8")
    close(project, first, [finding("F1", "Blocker", "FIX")], report(("combined",)))
    reaudit = open_round(project, "planning", ("combined",))
    assert reaudit["reaudit_of"] == 1
    close(project, reaudit, [], report(("combined",)))
    (project / "planning.md").write_text(PLANNING_BODY + "Changed again.\n", encoding="utf-8")
    delta = open_round(project, "planning", ("combined",))
    assert delta["delta_of"] == 2
    close(project, delta, [], report(("combined",)))
    gold(project, operation="waive", target="planning", reason="no subagent", confirmation=CONFIRMATION)
    fifth = open_round(project, "planning", ("combined",))
    assert fifth["round"] == 5 and fifth["warnings"] == [advice("planning", 5, 5)]


def test_the_close_result_carries_no_round_advice(lone: Path) -> None:
    opened = open_rounds(lone, 5)
    assert close(lone, opened[4], reports=report(("combined",)))["warnings"] == []
