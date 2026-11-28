"""c7: `workflow readiness` lists what the merge checks; the occupancy check skips this project's own siblings."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib
import workspace_occupancy
import workspace_workflows
from workspace_lib import WorkspaceError
from workspace_occupancy import _holds_project, _listed_worktrees
from workspace_session import update_project_data
from workspace_workflows import merge_requirements, workflow

from tests.plugins.research.project.test_delivery import FILES, URL, fetched, op
from tests.plugins.research.project.test_goldfish import CODE_TASK
from tests.plugins.research.project.test_goldfish_gates import BRANCH, PAST, diff_round, prepare_release
from tests.plugins.research.project.test_occupancy import OWN_BRANCH, Scene
from tests.plugins.research.project.test_occupancy import write_versions as write_pair
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    git,
    load_state,
    make_project,
    make_repository,
    record,
)


def retarget(project: Path, target: str) -> None:
    """Set the delivery target in the state file, as the delivery scenarios do, to pick the rules under test."""
    path = project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.update(policy_version=2, delivery_target=target)
    path.write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """A gated project with a code task and a pushed-looking worktree that has a pull request."""
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", PAST)
    repository = make_repository(tmp_path)
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    fetched(repository)
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    record(project, linked)
    update_project_data(project, {"tasks": [CODE_TASK]}, expected_revision=load_state(project)["revision"])
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    return project, linked, repository


def requirements(project: Path) -> list[dict[str, Any]]:
    return workflow(project, "readiness", {})["merge_requirements"]


# --- readiness: merge_requirements ----------------------------------------------------------------


def test_readiness_lists_the_release_record_the_full_hash_gate_and_the_diff_round(
    delivered: tuple[Path, Path, Path],
) -> None:
    project, linked, _ = delivered
    retarget(project, "release")
    head = git(linked, "rev-parse", "HEAD")
    [before] = requirements(project)
    assert before["worktree"] == str(linked)
    assert before["release"] == {"required": True, "recorded": False}
    assert before["merged_head"] == {"format": "40 or 64 lowercase hex", "branch_head": head}
    assert before["diff_round"]["owed"] is True
    assert len(before["diff_round"]["errors"]) == 1
    assert before["diff_round"]["errors"][0].startswith("goldfish gate 'diff': no round covers the diff version")

    released_head = prepare_release(project, linked)
    diff_round(project, linked)
    op(project, linked, "release", version="0.24.0", files=FILES)
    [after] = requirements(project)
    assert after["release"] == {"required": True, "recorded": True}
    assert after["merged_head"]["branch_head"] == released_head
    assert after["diff_round"] == {"owed": True, "errors": []}


def test_a_version_exemption_means_no_release_is_required(delivered: tuple[Path, Path, Path]) -> None:
    project, linked, _ = delivered
    retarget(project, "release")
    op(project, linked, "alpha", exempt=True, reason="docs-only project; no version change")
    [item] = requirements(project)
    assert item["release"] == {"required": False, "recorded": False}


def test_a_pr_target_owes_no_release_and_no_merge_round(delivered: tuple[Path, Path, Path]) -> None:
    project, _, _ = delivered
    retarget(project, "pr")
    [item] = requirements(project)
    assert item["release"] == {"required": False, "recorded": False}
    assert item["diff_round"] == {"owed": False, "errors": []}


def test_an_entry_without_a_merge_basis_is_not_listed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    record(project, linked)
    assert requirements(project) == []


def test_a_branch_with_no_readable_head_gets_its_own_error_and_no_gate_call(
    delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, repository = delivered
    git(repository, "update-ref", "-d", f"refs/heads/{BRANCH}")

    def forbidden(*_: object) -> list[str]:
        raise AssertionError("the gate must not be called without a head")

    monkeypatch.setattr(workspace_workflows, "delivery_gate_errors", forbidden)
    [item] = requirements(project)
    assert item["merged_head"]["branch_head"] is None
    assert item["diff_round"]["errors"] == [f"branch {BRANCH} has no readable head in {repository}"]


def test_a_head_that_is_not_full_hex_counts_as_unreadable(
    delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _ = delivered
    monkeypatch.setattr(workspace_workflows, "_query", lambda *_: "not-a-hash")
    [item] = requirements(project)
    assert item["merged_head"]["branch_head"] is None
    assert "has no readable head" in item["diff_round"]["errors"][0]


def test_a_head_with_no_diff_round_owed_lists_no_error(
    delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, repository = delivered
    git(repository, "update-ref", "-d", f"refs/heads/{BRANCH}")
    real = workspace_workflows.delivery_rules

    def quiet(state: dict[str, Any]) -> Any:
        return dataclasses.replace(real(state), diff_events=frozenset())

    monkeypatch.setattr(workspace_workflows, "delivery_rules", quiet)
    [item] = requirements(project)
    assert item["diff_round"] == {"owed": False, "errors": []}


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        (WorkspaceError("Git could not be consulted (boom)"), "Git could not be consulted (boom)"),
        (OSError("reviews unreadable"), "reviews unreadable"),
    ],
)
def test_readiness_reports_a_failing_round_listing_instead_of_raising(
    delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, failure: Exception, text: str
) -> None:
    project, _, _ = delivered

    def broken(*_: object) -> list[str]:
        raise failure

    monkeypatch.setattr(workspace_workflows, "delivery_gate_errors", broken)
    [item] = requirements(project)
    assert item["diff_round"]["errors"] == [f"the diff round could not be checked: {text}"]


def test_git_that_cannot_run_is_reported_with_no_head(
    delivered: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, _ = delivered

    def broken(*_: object) -> str:
        raise WorkspaceError("Git could not be consulted (gone)")

    monkeypatch.setattr(workspace_workflows, "_query", broken)
    [item] = requirements(project)
    assert item["merged_head"]["branch_head"] is None
    assert item["diff_round"]["errors"] == ["the diff round could not be checked: Git could not be consulted (gone)"]


@pytest.mark.parametrize(
    "items",
    [
        "not a list",
        ["not an object"],
        [{"status": "kept", "kind": "git", "path": "/p", "repository": "/r", "branch": "b", "pull_request": {}}],
        [{"status": "removed", "kind": "git", "path": "/p", "repository": "/r", "branch": "b", "pull_request": {}}],
        [{"status": "active", "kind": "none", "path": "/p", "repository": "/r", "branch": "b", "pull_request": {}}],
        [{"status": "active", "kind": "git", "path": "/p", "repository": "/r", "pull_request": {}}],
        [{"status": "active", "kind": "git", "path": 7, "repository": "/r", "branch": "b", "pull_request": {}}],
        [{"status": "active", "kind": "git", "path": "/p", "repository": "/r", "branch": "b"}],
    ],
)
def test_malformed_or_ineligible_entries_are_not_listed(tmp_path: Path, items: object) -> None:
    assert merge_requirements(tmp_path, {"worktrees": items}) == []
    assert merge_requirements(tmp_path, {}) == []


# --- occupancy: siblings of the same project -------------------------------------------------------


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


def refusal(scene: Scene, base: str = "0.30.0") -> str:
    with pytest.raises(WorkspaceError) as error:
        scene.check(base)
    return str(error.value)


def test_the_project_id_is_a_token_that_digits_do_not_extend() -> None:
    assert _holds_project("npham/2026-11-26-001-x", "2026-11-26-001")
    assert _holds_project("2026-11-26-001", "2026-11-26-001")
    assert not _holds_project("npham/2026-11-26-0011-x", "2026-11-26-001")
    assert not _holds_project("npham/12026-11-26-001", "2026-11-26-001")
    assert not _holds_project("", "2026-11-26-001")


def test_the_porcelain_listing_is_read_one_block_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listing = "worktree /a\nHEAD 1\nbranch refs/heads/feat/x\n\nstray block\n\nworktree /b\nHEAD 2\ndetached"
    monkeypatch.setattr(workspace_occupancy, "_query", lambda *_: listing)
    assert _listed_worktrees(tmp_path) == [(Path("/a"), "feat/x"), (Path("/b"), "")]
    monkeypatch.setattr(workspace_occupancy, "_query", lambda *_: None)
    assert _listed_worktrees(tmp_path) == []


def test_a_sibling_origin_branch_of_this_project_is_not_an_occupier(scene: Scene) -> None:
    scene.remote_branch(f"npham/{scene.project}-runtime", "0.30.0a1", "0.30.0-alpha.1")
    assert scene.check("0.30.0") == []


def test_a_sibling_local_branch_of_this_project_is_not_an_occupier(scene: Scene) -> None:
    scene.local_branch(f"npham/{scene.project}-second", "0.30.0a1", "0.30.0-alpha.1")
    assert scene.check("0.30.0") == []


def test_a_sibling_branch_without_the_project_id_still_refuses_and_is_named(scene: Scene) -> None:
    scene.remote_branch("npham/other-runtime", "0.30.0a1", "0.30.0-alpha.1")
    assert "remote: origin/npham/other-runtime carries 0.30.0" in refusal(scene)


def test_a_longer_number_does_not_pass_for_the_project_id(scene: Scene) -> None:
    scene.local_branch(f"npham/{scene.project}1-x", "0.30.0a1", "0.30.0-alpha.1")
    assert f"local: npham/{scene.project}1-x carries 0.30.0" in refusal(scene)


def test_a_same_project_worktree_on_a_sibling_branch_is_not_an_occupier(scene: Scene) -> None:
    other = add_worktree(scene.repository, "scratch", f"npham/{scene.project}-b")
    write_pair(other, "0.30.0a3", "0.30.0-alpha.3")
    assert scene.check("0.30.0") == []


def test_a_detached_same_project_worktree_is_skipped_by_its_path(scene: Scene) -> None:
    detached = scene.repository.parent / f"{scene.repository.name}.worktrees" / scene.project
    git(scene.repository, "worktree", "add", "-q", "--detach", str(detached))
    write_pair(detached, "0.30.0a3", "0.30.0-alpha.3")
    assert [(path.resolve(), branch) for path, branch in _listed_worktrees(scene.own)][-1] == (detached.resolve(), "")
    assert scene.check("0.30.0") == []


def test_a_detached_worktree_of_another_name_still_occupies_its_base(scene: Scene) -> None:
    detached = scene.repository.parent / f"{scene.repository.name}.worktrees" / "elsewhere"
    git(scene.repository, "worktree", "add", "-q", "--detach", str(detached))
    write_pair(detached, "0.30.0a3", "0.30.0-alpha.3")
    assert f"local: worktree {detached.resolve()} carries 0.30.0" in refusal(scene)


def test_a_worktree_on_another_branch_without_the_id_still_occupies_its_base(scene: Scene) -> None:
    other = add_worktree(scene.repository, "scratch", "npham/unrelated")
    write_pair(other, "0.30.0a3", "0.30.0-alpha.3")
    assert f"local: worktree {other.resolve()} carries 0.30.0" in refusal(scene)


def test_the_recorded_own_branch_and_worktree_stay_excluded(scene: Scene) -> None:
    write_pair(scene.own, "0.30.0a1", "0.30.0-alpha.1")
    scene.remote_branch(OWN_BRANCH, "0.30.0a1", "0.30.0-alpha.1")
    assert scene.check("0.30.0") == []
