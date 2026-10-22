"""The occupied-base check: four sources, the exclusions, the refusals and the notes."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import workspace_occupancy
from workspace_lib import WorkspaceError, allocate_project
from workspace_occupancy import check_base, versions_in

from tests.plugins.research.project.test_worktrees import add_worktree, git, make_repository

FILES = [{"path": "pyproject.toml", "style": "pep440"}, {"path": "plugin.json", "style": "semver"}]
OWN_BRANCH = "feat/own"


def write_versions(directory: Path, python: str, manifest: str) -> None:
    (directory / "pyproject.toml").write_text(f'[project]\nversion = "{python}"\n', encoding="utf-8")
    (directory / "plugin.json").write_text(json.dumps({"version": manifest}), encoding="utf-8")


def commit_versions(repository: Path, python: str, manifest: str) -> str:
    write_versions(repository, python, manifest)
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", f"version {python}")
    return git(repository, "rev-parse", "HEAD")


class Scene:
    """A repository on main at 0.29.0 with a fetched origin, this project's worktree, and a workspace."""

    def __init__(self, root: Path) -> None:
        self.repository = make_repository(root)
        commit_versions(self.repository, "0.29.0", "0.29.0")
        git(self.repository, "remote", "add", "origin", "https://github.com/o/r.git")
        git(self.repository, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.own = add_worktree(self.repository, "2026-02-02-001", OWN_BRANCH)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.project = allocate_project(self.workspace, title="Own project", working_directory=self.own).name
        self.entry = {"path": str(self.own), "branch": OWN_BRANCH, "repository": str(self.repository)}

    def check(self, base: str = "0.30.0", files: list[dict[str, str]] = FILES) -> list[str]:
        return check_base(
            self.own, self.entry, files, base, workspace_root=self.workspace, project=self.project,
        )

    def main_lock(self, text: str) -> None:
        """Release a `uv.lock` on main: commit it and move origin/main to it."""
        (self.repository / "uv.lock").write_text(text, encoding="utf-8")
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-q", "-m", "lock")
        git(self.repository, "update-ref", "refs/remotes/origin/main", "HEAD")

    def remote_lock_branch(self, name: str, text: str) -> None:
        """A branch that exists only on origin and carries a `uv.lock`."""
        git(self.repository, "checkout", "-q", "-b", "scratch", "main")
        (self.repository / "uv.lock").write_text(text, encoding="utf-8")
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-q", "-m", "lock")
        git(self.repository, "update-ref", f"refs/remotes/origin/{name}", git(self.repository, "rev-parse", "HEAD"))
        git(self.repository, "checkout", "-q", "main")
        git(self.repository, "branch", "-q", "-D", "scratch")

    def remote_branch(self, name: str, python: str, manifest: str) -> None:
        """A branch that exists only on origin: its commit is reachable from a remote-tracking ref alone."""
        git(self.repository, "checkout", "-q", "-b", "scratch", "main")
        sha = commit_versions(self.repository, python, manifest)
        git(self.repository, "update-ref", f"refs/remotes/origin/{name}", sha)
        git(self.repository, "checkout", "-q", "main")
        git(self.repository, "branch", "-q", "-D", "scratch")

    def local_branch(self, name: str, python: str, manifest: str) -> None:
        git(self.repository, "checkout", "-q", "-b", name, "main")
        commit_versions(self.repository, python, manifest)
        git(self.repository, "checkout", "-q", "main")

    def other_project(
        self, name: str, status: str, base: str, *, repository: Path | None = None, merged: bool = False
    ) -> None:
        other = allocate_project(self.workspace, title=name, working_directory=self.repository).name
        path = self.workspace / other / "project.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        item: dict[str, Any] = {
            "repository": str(repository or self.repository), "version_line": {"base": base},
        }
        if merged:
            item["merge"] = {"state": "merged"}
        state.update(status=status, worktrees=[item])
        path.write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


def refused(scene: Scene, base: str = "0.30.0") -> str:
    with pytest.raises(WorkspaceError) as refusal:
        scene.check(base)
    return str(refusal.value)


def test_a_version_file_states_its_plain_version_in_either_spelling() -> None:
    assert versions_in('[project]\nversion = "0.30.0a3"\n', "pep440") == ["0.30.0"]
    assert versions_in('__version__ = "1.2.3"\n', "pep440") == ["1.2.3"]
    assert versions_in('{"version": "0.30.0-alpha.3"}', "semver") == ["0.30.0"]
    assert versions_in('{"version": "0.30.0"}', "semver") == ["0.30.0"]
    assert versions_in('requires-python = ">=3.11"\ndependencies = ["x>=0.30.0"]\n', "pep440") == []
    assert versions_in('{"name": "x"}', "semver") == []


def test_a_free_base_passes_with_nothing_to_note(scene: Scene) -> None:
    assert scene.check("0.30.0") == []


def test_a_base_at_or_below_the_released_version_is_occupied(scene: Scene) -> None:
    assert "main: origin/main releases 0.29.0" in refused(scene, "0.29.0")
    assert "main: origin/main releases 0.29.0" in refused(scene, "0.28.5")
    assert scene.check("0.29.1") == []


def test_a_version_tag_counts_as_released(scene: Scene) -> None:
    git(scene.repository, "tag", "v0.31.0")
    git(scene.repository, "tag", "not-a-version")
    message = refused(scene, "0.30.0")
    assert "main: tag v0.31.0 releases 0.31.0" in message
    assert scene.check("0.31.1") == []


def test_another_origin_branch_occupies_its_base_in_either_alpha_spelling(scene: Scene) -> None:
    scene.remote_branch("feat/other", "0.30.0a2", "0.30.0-alpha.2")
    message = refused(scene, "0.30.0")
    assert "remote: origin/feat/other carries 0.30.0" in message
    assert "The next free base is 0.30.1" in message
    assert scene.check("0.31.0") == []


def test_this_projects_own_remote_branch_is_excluded(scene: Scene) -> None:
    scene.remote_branch(OWN_BRANCH, "0.30.0a1", "0.30.0-alpha.1")
    assert scene.check("0.30.0") == []


def test_a_local_branch_that_origin_does_not_have_occupies_its_base(scene: Scene) -> None:
    scene.local_branch("feat/unpushed", "0.30.0a1", "0.30.0-alpha.1")
    assert "local: feat/unpushed carries 0.30.0" in refused(scene, "0.30.0")


def test_a_local_branch_repeating_an_origin_branch_is_counted_once(scene: Scene) -> None:
    scene.remote_branch("feat/other", "0.30.0a2", "0.30.0-alpha.2")
    sha = git(scene.repository, "rev-parse", "refs/remotes/origin/feat/other")
    git(scene.repository, "branch", "feat/other", sha)
    message = refused(scene, "0.30.0")
    assert message.count("carries 0.30.0") == 1
    assert "remote: origin/feat/other" in message


def test_another_worktree_occupies_its_base_and_this_one_does_not(scene: Scene) -> None:
    write_versions(scene.own, "0.30.0a1", "0.30.0-alpha.1")
    assert scene.check("0.30.0") == []
    other = add_worktree(scene.repository, "2026-02-02-002", "feat/second")
    write_versions(other, "0.30.0a5", "0.30.0-alpha.5")
    assert f"local: worktree {other.resolve()} carries 0.30.0" in refused(scene, "0.30.0")


def test_a_worktree_without_the_version_files_is_skipped(scene: Scene) -> None:
    other = add_worktree(scene.repository, "2026-02-02-002", "feat/second")
    for name in ("pyproject.toml", "plugin.json"):
        (other / name).unlink()
    assert scene.check("0.30.0") == []


def test_a_live_project_on_the_same_repository_occupies_its_base(scene: Scene) -> None:
    scene.other_project("Other work", "EXECUTING", "0.30.0")
    assert "workspace: project 2026-" in refused(scene, "0.30.0")


@pytest.mark.parametrize("status", ["DONE", "CANCELLED"])
def test_a_finished_project_holds_nothing(scene: Scene, status: str) -> None:
    scene.other_project("Finished work", status, "0.30.0")
    assert scene.check("0.30.0") == []


def test_a_merged_version_line_holds_nothing(scene: Scene) -> None:
    scene.other_project("Merged work", "EXECUTING", "0.30.0", merged=True)
    assert scene.check("0.30.0") == []


def test_a_project_on_another_repository_holds_nothing(scene: Scene, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    scene.other_project("Other repository", "EXECUTING", "0.30.0", repository=elsewhere)
    assert scene.check("0.30.0") == []


def test_this_projects_own_record_is_excluded(scene: Scene) -> None:
    path = scene.workspace / scene.project / "project.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    own_line = {"repository": str(scene.repository), "version_line": {"base": "0.30.0"}}
    state.update(status="EXECUTING", worktrees=[own_line])
    path.write_text(json.dumps(state), encoding="utf-8")
    assert scene.check("0.30.0") == []


@pytest.mark.parametrize(
    "worktrees",
    [
        "not a list",
        ["not an object"],
        [{"version_line": "not an object"}],
        [{"version_line": {"base": 7}, "repository": "/x"}],
        [{"version_line": {"base": "0.30.0a1"}, "repository": "/x"}],
        [{"version_line": {"base": "0.30.0"}, "repository": 7}],
    ],
)
def test_malformed_records_of_other_projects_are_ignored(scene: Scene, worktrees: object) -> None:
    other = allocate_project(scene.workspace, title="Odd", working_directory=scene.repository)
    state = json.loads((other / "project.json").read_text(encoding="utf-8"))
    state.update(status="EXECUTING", worktrees=worktrees)
    (other / "project.json").write_text(json.dumps(state), encoding="utf-8")
    broken = scene.workspace / "2026-09-09-009"
    broken.mkdir()
    (broken / "project.json").write_text("[]", encoding="utf-8")
    (scene.workspace / ".hidden").mkdir()
    (scene.workspace / "notes.txt").write_text("not a project", encoding="utf-8")
    assert scene.check("0.30.0") == []


def test_a_missing_workspace_root_has_nothing_to_read(scene: Scene, tmp_path: Path) -> None:
    check_base(
        scene.own, scene.entry, FILES, "0.30.0", workspace_root=tmp_path / "absent", project=scene.project,
    )


def test_every_occupier_is_named_and_the_suggestion_is_above_the_highest_version_seen(scene: Scene) -> None:
    scene.remote_branch("feat/other", "0.30.0a2", "0.30.0-alpha.2")
    scene.local_branch("feat/ahead", "0.32.4a1", "0.32.4-alpha.1")
    scene.other_project("Other work", "EXECUTING", "0.30.0")
    git(scene.repository, "tag", "v0.29.9")
    message = refused(scene, "0.30.0")
    assert "remote: origin/feat/other carries 0.30.0" in message
    assert "workspace: project 2026-" in message
    assert "The next free base is 0.32.5, the patch above the highest version seen (0.32.4)" in message


def test_without_an_origin_the_local_sources_still_decide_and_the_skip_is_noted(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    commit_versions(repository, "0.29.0", "0.29.0")
    own = add_worktree(repository, "2026-02-02-001", OWN_BRANCH)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = {"path": str(own), "branch": OWN_BRANCH, "repository": str(repository)}
    notes = check_base(own, entry, FILES, "0.30.0", workspace_root=workspace, project="2026-02-02-001")
    assert notes == ["no origin remote: the origin/main and remote-branch sources were skipped"]
    other = add_worktree(repository, "2026-02-02-002", "feat/second")
    write_versions(other, "0.30.0a1", "0.30.0-alpha.1")
    with pytest.raises(WorkspaceError, match="local: worktree"):
        check_base(own, entry, FILES, "0.30.0", workspace_root=workspace, project="2026-02-02-001")


def test_an_origin_without_a_main_branch_ref_refuses(scene: Scene) -> None:
    git(scene.repository, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    git(scene.repository, "update-ref", "-d", "refs/remotes/origin/main")
    assert "origin has no main branch ref" in refused(scene)


def test_origin_head_may_name_a_branch_other_than_main(scene: Scene) -> None:
    git(scene.repository, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    git(scene.repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    git(scene.repository, "update-ref", "-d", "refs/remotes/origin/main")
    assert "main: origin/trunk releases 0.29.0" in refused(scene, "0.29.0")


def test_a_directory_that_is_not_a_repository_refuses(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    entry = {"path": str(plain), "branch": "x", "repository": str(plain)}
    with pytest.raises(WorkspaceError, match="Git could not be consulted"):
        check_base(plain, entry, FILES, "0.30.0", workspace_root=tmp_path, project="p")


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("git", 5), FileNotFoundError("git")])
def test_a_git_that_cannot_run_refuses(scene: Scene, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def broken(*_: object) -> str:
        raise failure

    monkeypatch.setattr(workspace_occupancy, "_git", broken)
    assert "Git could not be consulted" in refused(scene)


def test_old_origin_refs_are_noted_and_fresh_ones_are_not(scene: Scene) -> None:
    fetch_head = Path(git(scene.own, "rev-parse", "--git-path", "FETCH_HEAD"))
    fetch_head.write_text("", encoding="utf-8")
    assert scene.check("0.30.0") == []
    old = time.time() - 3 * 24 * 3600
    os.utime(fetch_head, (old, old))
    notes = scene.check("0.30.0")
    assert len(notes) == 1 and "last fetched 72 hours ago" in notes[0]


def test_a_git_path_that_cannot_be_read_gives_no_note(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    real = workspace_occupancy._query

    def query(root: Path, *arguments: str) -> str | None:
        if arguments[:2] in (("rev-parse", "--git-path"), ("rev-parse", "--git-common-dir")):
            return None
        return real(root, *arguments)

    monkeypatch.setattr(workspace_occupancy, "_query", query)
    assert scene.check("0.30.0") == []


LOCKED = [{"path": "pyproject.toml", "style": "pep440"}, {"path": "uv.lock", "style": "pep440"}]
ROOT = 'source = { virtual = "." }'
REGISTRY = 'source = { registry = "https://pypi.org/simple" }'


def package(name: str, version: str | None, source: str, extra: str = "") -> str:
    lines = ["[[package]]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    return "\n".join([*lines, source]) + "\n" + extra


def lock(*blocks: str) -> str:
    return 'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n\n' + "\n".join(blocks)


class TestOwnEntryOfALock:
    """A `uv.lock` states one version of this project and many of its dependencies; only the first counts."""

    def test_a_dependency_is_not_the_project(self) -> None:
        text = lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.3.6", ROOT))
        assert versions_in(text, "pep440") == ["0.3.6"]

    def test_the_root_is_found_before_or_after_its_dependencies(self) -> None:
        text = lock(package("agents", "0.3.6", ROOT), package("regex", "2026.9.10", REGISTRY))
        assert versions_in(text, "pep440") == ["0.3.6"]

    @pytest.mark.parametrize(
        "source", ['source = { editable = "." }', 'source = {editable = "."}', 'source = { virtual = "." }']
    )
    def test_the_root_is_editable_or_virtual_with_any_spacing(self, source: str) -> None:
        text = lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.3.6", source))
        assert versions_in(text, "pep440") == ["0.3.6"]

    def test_a_workspace_member_is_not_the_root(self) -> None:
        member = package("member", "9.9.9", 'source = { editable = "packages/member" }')
        assert versions_in(lock(member, package("agents", "0.3.6", ROOT)), "pep440") == ["0.3.6"]
        assert versions_in(lock(member), "pep440") == []

    def test_a_lock_with_no_root_has_no_version(self) -> None:
        assert versions_in(lock(package("regex", "2026.9.10", REGISTRY)), "pep440") == []

    def test_a_root_with_a_dynamic_version_has_no_version(self) -> None:
        text = lock(package("regex", "2026.9.10", REGISTRY), package("agents", None, ROOT))
        assert versions_in(text, "pep440") == []

    def test_an_alpha_spelling_of_the_root_reads_as_its_base(self) -> None:
        text = lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.3.7a2", ROOT))
        assert versions_in(text, "pep440") == ["0.3.7"]

    def test_windows_line_endings_read_the_same(self) -> None:
        text = lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.3.6", ROOT))
        assert versions_in(text.replace("\n", "\r\n"), "pep440") == ["0.3.6"]

    def test_a_version_line_in_a_sub_table_is_not_the_root_version(self) -> None:
        text = lock(package("agents", "0.3.6", ROOT, '\n[package.metadata]\nversion = "9.9.9"\n'))
        assert versions_in(text, "pep440") == ["0.3.6"]

    def test_main_releasing_a_dependency_does_not_occupy_a_base(self, scene: Scene) -> None:
        scene.main_lock(lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.29.0", ROOT)))
        assert scene.check("0.30.0", LOCKED) == []

    def test_main_releasing_the_project_itself_still_occupies_a_base(self, scene: Scene) -> None:
        scene.main_lock(lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.31.0", ROOT)))
        with pytest.raises(WorkspaceError, match=r"main: origin/main releases 0\.31\.0"):
            scene.check("0.30.0", LOCKED)

    def test_another_origin_branch_holding_a_dependency_at_the_base_does_not_occupy_it(self, scene: Scene) -> None:
        scene.remote_lock_branch(
            "feat/other", lock(package("regex", "0.30.0", REGISTRY), package("agents", "0.29.0", ROOT))
        )
        assert scene.check("0.30.0", LOCKED) == []

    def test_another_origin_branch_holding_the_project_at_the_base_occupies_it(self, scene: Scene) -> None:
        scene.remote_lock_branch(
            "feat/other", lock(package("regex", "2026.9.10", REGISTRY), package("agents", "0.30.0", ROOT))
        )
        with pytest.raises(WorkspaceError, match=r"remote: origin/feat/other carries 0\.30\.0"):
            scene.check("0.30.0", LOCKED)

    def test_another_worktree_holding_a_dependency_at_the_base_does_not_occupy_it(self, scene: Scene) -> None:
        other = add_worktree(scene.repository, "2026-02-03-001", "feat/other-worktree")
        (other / "uv.lock").write_text(
            lock(package("regex", "0.30.0", REGISTRY), package("agents", "0.29.0", ROOT)), encoding="utf-8"
        )
        assert scene.check("0.30.0", LOCKED) == []
