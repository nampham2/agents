"""Delivery records of a worktree: the MR, the alpha line, the release, the merge, and their gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import workspace_lib
from workspace_delivery import closure_findings, delivery_errors, spelling, start_findings
from workspace_lib import WorkspaceError, is_delivery_gated
from workspace_operations import task_operation
from workspace_session import update_project_data
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import done, planned
from tests.plugins.research.project.test_workflow_automation import task
from tests.plugins.research.project.test_worktrees import (
    REFLECTION,
    add_worktree,
    close,
    entry,
    errors_for,
    git,
    load_state,
    make_project,
    make_repository,
    record,
    run,
)

URL = "https://github.com/o/r/pull/7"
FILES = [{"path": "pyproject.toml", "style": "pep440"}, {"path": "plugin.json", "style": "semver"}]


def write_versions(root: Path, base: str, alpha: int | None) -> None:
    """Write the two version files; `alpha` None writes the plain release version."""
    python = base if alpha is None else spelling(base, alpha, "pep440")
    manifest = base if alpha is None else spelling(base, alpha, "semver")
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{python}"\n', encoding="utf-8")
    (root / "plugin.json").write_text(json.dumps({"version": manifest}), encoding="utf-8")


def op(project: Path, linked: Path, operation: str, **fields: Any) -> dict[str, Any]:
    request = {
        "operation": operation,
        "path": str(linked),
        "confirmation": {"source": "user reply", "response": "approve"},
        **fields,
    }
    return run(project, request)


def refuse(project: Path, linked: Path, fragment: str, operation: str, **fields: Any) -> None:
    before = load_state(project)
    with pytest.raises(WorkspaceError, match=fragment):
        op(project, linked, operation, **fields)
    assert load_state(project) == before


def entry_of(project: Path) -> dict[str, Any]:
    return load_state(project)["worktrees"][0]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


@pytest.fixture
def recorded(tmp_path: Path, repository: Path, linked: Path) -> Path:
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    project = make_project(tmp_path, repository)
    record(project, linked)
    return project


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "DELIVERY_GATES_FROM", "2000-01-01T00:00:00+00:00")


def alpha(project: Path, linked: Path, number: int, **fields: Any) -> dict[str, Any]:
    write_versions(linked, "0.24.0", number)
    request: dict[str, Any] = {"base": "0.24.0", "alpha": number, "reason": f"alpha {number}"}
    if number == 1:
        request["files"] = FILES
    return op(project, linked, "alpha", **{**request, **fields})


class TestSpelling:
    def test_both_spellings_and_the_boundary(self) -> None:
        assert spelling("0.24.0", 3, "pep440") == "0.24.0a3"
        assert spelling("0.24.0", 3, "semver") == "0.24.0-alpha.3"


class TestPullRequest:
    def test_a_github_pull_request_is_recorded(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        item = entry_of(recorded)["pull_request"]
        assert (item["host"], item["url"], item["number"]) == ("github", URL, 7)

    def test_a_gitlab_merge_request_on_an_ssh_remote(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        git(repository, "remote", "add", "origin", "git@gitlab.example.com:group/repo.git")
        project = make_project(tmp_path, repository)
        record(project, linked)
        url = "https://gitlab.example.com/group/repo/-/merge_requests/12"
        op(project, linked, "pull_request", host="gitlab", url=url, number=12)
        assert entry_of(project)["pull_request"]["host"] == "gitlab"

    def test_a_remote_with_a_scheme_is_read(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        git(repository, "remote", "add", "origin", "ssh://git@github.com/o/r.git")
        project = make_project(tmp_path, repository)
        record(project, linked)
        op(project, linked, "pull_request", host="github", url=URL, number=7)

    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            ({"host": "bitbucket", "url": URL, "number": 7}, "host must be github or gitlab"),
            ({"host": "github", "url": "https://github.com/o/r/pull/8", "number": 7}, "must end with /pull/7"),
            ({"host": "github", "url": "https://gitlab.com/o/r/pull/7", "number": 7}, "host of the origin remote"),
            ({"host": "github", "url": "http://github.com/o/r/pull/7", "number": 7}, "https URL"),
            ({"host": "github", "url": URL, "number": 0}, "number must be a whole number"),
            ({"host": "github", "url": URL, "number": True}, "number must be a whole number"),
            ({"host": "gitlab", "url": URL, "number": 7}, "must end with /-/merge_requests/7"),
            ({"exempt": "other"}, "exempt must be no_remote"),
            ({"exempt": "no_remote"}, "has a remote"),
        ],
    )
    def test_refusals(self, recorded: Path, linked: Path, fields: dict[str, Any], fragment: str) -> None:
        refuse(recorded, linked, fragment, "pull_request", **fields)

    def test_no_origin_remote(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        record(project, linked)
        refuse(project, linked, "no `origin` remote", "pull_request", host="github", url=URL, number=7)

    def test_an_unreadable_remote_host(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        git(repository, "remote", "add", "origin", "file:///srv/repo.git")
        project = make_project(tmp_path, repository)
        record(project, linked)
        refuse(project, linked, "cannot read a host", "pull_request", host="github", url=URL, number=7)

    def test_a_second_record_is_refused(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        refuse(recorded, linked, "already has", "pull_request", host="github", url=URL, number=7)

    def test_an_exemption_needs_a_repository_with_no_remote(
        self, tmp_path: Path, repository: Path, linked: Path
    ) -> None:
        project = make_project(tmp_path, repository)
        record(project, linked)
        op(project, linked, "pull_request", exempt="no_remote")
        assert entry_of(project)["pr_exemption"]["reason"] == "no_remote"
        assert entry_of(project)["pr_exemption"]["response"] == "approve"

    def test_git_unavailable_for_the_exemption(self, recorded: Path, linked: Path, monkeypatch: Any) -> None:
        import workspace_delivery

        monkeypatch.setattr(workspace_delivery, "_git", lambda *args: None)
        refuse(recorded, linked, "Git could not be consulted", "pull_request", exempt="no_remote")

    def test_the_operation_needs_an_active_repository_worktree(self, recorded: Path, tmp_path: Path) -> None:
        refuse(recorded, tmp_path / "nowhere", "no active repository worktree", "pull_request", exempt="no_remote")


class TestAlphaLine:
    def test_the_first_alpha_and_a_bump(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        alpha(recorded, linked, 2, reason="before the plugin refresh")
        line = entry_of(recorded)["version_line"]
        assert (line["base"], line["alpha"], line["release"]) == ("0.24.0", 2, None)
        assert [item["alpha"] for item in line["history"]] == [1, 2]
        assert line["files"] == FILES

    def test_the_number_may_skip_but_never_fall_or_repeat(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        alpha(recorded, linked, 5)
        write_versions(linked, "0.24.0", 4)
        refuse(recorded, linked, "only goes up", "alpha", base="0.24.0", alpha=4, reason="lower")
        write_versions(linked, "0.24.0", 5)
        refuse(recorded, linked, "only goes up", "alpha", base="0.24.0", alpha=5, reason="again")

    def test_the_base_is_fixed(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        write_versions(linked, "0.25.0", 2)
        refuse(recorded, linked, "base stays 0.24.0", "alpha", base="0.25.0", alpha=2, reason="new base")

    def test_the_files_are_fixed_and_a_repeat_may_omit_them(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        write_versions(linked, "0.24.0", 2)
        refuse(
            recorded,
            linked,
            "cannot change",
            "alpha",
            base="0.24.0",
            alpha=2,
            reason="r",
            files=[{"path": "pyproject.toml", "style": "pep440"}],
        )
        alpha(recorded, linked, 2, files=FILES)

    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            ({"base": "0.24", "alpha": 1, "reason": "r", "files": FILES}, "plain X.Y.Z"),
            ({"base": "0.24.0", "alpha": 0, "reason": "r", "files": FILES}, "alpha must be a whole number"),
            ({"base": "0.24.0", "alpha": 1, "reason": "r"}, "files are required"),
            ({"base": "0.24.0", "alpha": 1, "reason": "r", "files": []}, "at least one version file"),
            ({"base": "0.24.0", "alpha": 1, "reason": "r", "files": [{"path": "a", "style": "x"}]}, "pep440 or semver"),
            (
                {"base": "0.24.0", "alpha": 1, "reason": "r", "files": [{"path": "/etc/hosts", "style": "pep440"}]},
                "relative to the worktree",
            ),
            ({"base": "0.24.0", "alpha": 1, "reason": "r", "files": [{"path": "../x", "style": "pep440"}]}, "no '..'"),
            (
                {"base": "0.24.0", "alpha": 1, "reason": "r", "files": [{"path": "absent.toml", "style": "pep440"}]},
                "missing in the worktree",
            ),
            ({"base": "0.24.0", "alpha": 2, "reason": "r", "files": FILES}, "does not contain 0.24.0a2"),
        ],
    )
    def test_first_alpha_refusals(self, recorded: Path, linked: Path, fields: dict[str, Any], fragment: str) -> None:
        write_versions(linked, "0.24.0", 1)
        refuse(recorded, linked, fragment, "alpha", **fields)

    def test_a_longer_number_does_not_satisfy_a_shorter_one(self, recorded: Path, linked: Path) -> None:
        write_versions(linked, "0.24.0", 10)
        refuse(recorded, linked, "does not contain 0.24.0a1", "alpha", base="0.24.0", alpha=1, reason="r", files=FILES)

    def test_an_exemption_excludes_the_line(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "alpha", exempt=True)
        assert entry_of(recorded)["version_exemption"]["response"] == "approve"
        refuse(recorded, linked, "no alpha line applies", "alpha", base="0.24.0", alpha=1, reason="r", files=FILES)
        refuse(recorded, linked, "already has", "alpha", exempt=True)

    def test_an_exemption_after_a_line_is_refused(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        refuse(recorded, linked, "already has", "alpha", exempt=True)

    def test_a_bump_after_the_release_clears_it_and_keeps_the_history(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        write_versions(linked, "0.24.0", None)
        op(recorded, linked, "release", version="0.24.0")
        assert entry_of(recorded)["version_line"]["release"]["version"] == "0.24.0"
        alpha(recorded, linked, 2, reason="fix after review")
        line = entry_of(recorded)["version_line"]
        assert line["release"] is None and [item["alpha"] for item in line["history"]] == [1, 2]

    def test_no_bump_after_the_merge(self, recorded: Path, linked: Path) -> None:
        merged(recorded, linked)
        write_versions(linked, "0.24.0", 9)
        refuse(recorded, linked, "line is closed", "alpha", base="0.24.0", alpha=9, reason="r")


def merged(project: Path, linked: Path) -> None:
    op(project, linked, "pull_request", host="github", url=URL, number=7)
    alpha(project, linked, 1)
    write_versions(linked, "0.24.0", None)
    op(project, linked, "release", version="0.24.0")
    op(project, linked, "merge", merge_state="merged", method="squash", evidence="commit abc123")


class TestRelease:
    def test_a_plain_release_is_recorded(self, recorded: Path, linked: Path) -> None:
        alpha(recorded, linked, 1)
        write_versions(linked, "0.24.0", None)
        op(recorded, linked, "release", version="0.24.0")
        assert entry_of(recorded)["version_line"]["release"]["version"] == "0.24.0"

    def test_refusals(self, recorded: Path, linked: Path) -> None:
        refuse(recorded, linked, "no version_line", "release", version="0.24.0")
        alpha(recorded, linked, 1)
        refuse(recorded, linked, "must equal the base", "release", version="0.24.1")
        refuse(recorded, linked, "still carries an alpha", "release", version="0.24.0")
        (linked / "pyproject.toml").write_text('version = "0.23.9"\n', encoding="utf-8")
        (linked / "plugin.json").write_text('{"version": "0.24.0"}', encoding="utf-8")
        refuse(recorded, linked, "pyproject.toml does not contain 0.24.0", "release", version="0.24.0")


class TestMerge:
    def test_merged_and_declined(self, recorded: Path, linked: Path) -> None:
        merged(recorded, linked)
        item = entry_of(recorded)["merge"]
        assert (item["state"], item["method"], item["evidence"]) == ("merged", "squash", "commit abc123")
        refuse(recorded, linked, "final", "merge", merge_state="declined")

    def test_declined_then_merged(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        op(recorded, linked, "merge", merge_state="declined")
        assert entry_of(recorded)["merge"] == {
            **entry_of(recorded)["merge"],
            "state": "declined",
            "method": None,
            "evidence": None,
        }
        op(recorded, linked, "alpha", exempt=True)
        op(recorded, linked, "merge", merge_state="merged", method="rebase", evidence="https://github.com/o/r/pull/7")
        assert entry_of(recorded)["merge"]["method"] == "rebase"

    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            ({"merge_state": "later"}, "merged or declined"),
            ({"merge_state": "merged", "evidence": "x"}, "method squash, merge or rebase"),
            ({"merge_state": "merged", "method": "squash"}, "evidence must be non-empty"),
        ],
    )
    def test_refusals(self, recorded: Path, linked: Path, fields: dict[str, Any], fragment: str) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        refuse(recorded, linked, fragment, "merge", **fields)

    def test_a_merge_needs_a_pull_request(self, recorded: Path, linked: Path) -> None:
        refuse(recorded, linked, "no pull_request", "merge", merge_state="declined")

    def test_a_merge_needs_the_release_when_a_line_exists(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        alpha(recorded, linked, 1)
        refuse(
            recorded,
            linked,
            "release version before the merge",
            "merge",
            merge_state="merged",
            method="merge",
            evidence="x",
        )


class TestRemovalFollowsTheMerge:
    def test_remove_is_refused_until_the_merge_is_recorded(
        self, recorded: Path, repository: Path, linked: Path
    ) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        git(repository, "worktree", "remove", str(linked))
        before = load_state(recorded)
        with pytest.raises(WorkspaceError, match="not merged"):
            close(recorded, linked, "remove")
        assert load_state(recorded) == before

    def test_remove_follows_a_recorded_merge(self, recorded: Path, repository: Path, linked: Path) -> None:
        merged(recorded, linked)
        git(repository, "worktree", "remove", "--force", str(linked))
        close(recorded, linked, "remove")
        assert entry_of(recorded)["status"] == "removed"

    def test_a_declined_merge_cannot_be_removed_but_can_be_kept(self, recorded: Path, linked: Path) -> None:
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        op(recorded, linked, "merge", merge_state="declined")
        with pytest.raises(WorkspaceError, match="not merged"):
            close(recorded, linked, "remove")
        close(recorded, linked, "keep")
        assert entry_of(recorded)["status"] == "kept"


LINE = {
    "files": FILES,
    "base": "0.24.0",
    "alpha": 2,
    "release": None,
    "history": [
        {"alpha": 1, "recorded_at": "2026-01-01T10:00:00+00:00", "reason": "first"},
        {"alpha": 2, "recorded_at": "2026-01-01T11:00:00+00:00", "reason": "second"},
    ],
}
PR = {"host": "github", "url": URL, "number": 7, "recorded_at": "2026-01-01T10:00:00+00:00"}
MERGE = {
    "state": "merged",
    "method": "squash",
    "evidence": "abc",
    "source": "user",
    "response": "merge",
    "recorded_at": "2026-01-02T10:00:00+00:00",
}


class TestShape:
    def errors(self, **fields: Any) -> list[str]:
        return delivery_errors({**entry(), **fields}, "worktree #1")

    def test_valid_records_have_no_errors(self) -> None:
        assert self.errors() == []
        release = {"version": "0.24.0", "recorded_at": "2026-01-02T10:00:00+00:00"}
        assert self.errors(pull_request=PR, version_line={**LINE, "release": release}, merge=MERGE) == []
        exempt = {"source": "user", "response": "no remote"}
        assert self.errors(pr_exemption={"reason": "no_remote", **exempt}, version_exemption=exempt) == []

    @pytest.mark.parametrize(
        ("field", "value", "fragment"),
        [
            ("pull_request", "x", "must be an object"),
            ("pull_request", {"host": "github"}, "missing number"),
            ("pull_request", {**PR, "host": "bitbucket"}, "needs host github or gitlab"),
            ("pull_request", {**PR, "number": True}, "needs host github or gitlab"),
            ("pull_request", {**PR, "number": 0}, "needs host github or gitlab"),
            ("pull_request", {**PR, "url": ""}, "needs host github or gitlab"),
            ("pull_request", {**PR, "recorded_at": "yesterday"}, "needs host github or gitlab"),
            ("pr_exemption", {"reason": "other", "source": "s", "response": "r"}, "quoted source and response"),
            ("pr_exemption", {"reason": "no_remote", "source": "", "response": "r"}, "quoted source and response"),
            ("version_exemption", {"source": "s"}, "missing response"),
            ("version_exemption", {"source": "s", "response": ""}, "quoted source and response"),
            ("version_line", "x", "must be an object"),
            ("version_line", {**LINE, "base": "0.24"}, "base must be a plain"),
            ("version_line", {**LINE, "alpha": 0}, "alpha must be a whole number"),
            ("version_line", {**LINE, "files": []}, "files must list objects"),
            ("version_line", {**LINE, "files": [{"path": "a", "style": "x"}]}, "files must list objects"),
            ("version_line", {**LINE, "history": []}, "history must be a non-empty list"),
            ("version_line", {**LINE, "history": [{"alpha": 2}]}, "history item 1 needs"),
            ("version_line", {**LINE, "history": [{**LINE["history"][0], "alpha": True}]}, "history item 1 needs"),
            ("version_line", {**LINE, "alpha": 3}, "must increase and end at the current alpha"),
            ("version_line", {**LINE, "history": [LINE["history"][1], LINE["history"][0]]}, "must increase"),
            (
                "version_line",
                {**LINE, "release": {"version": "0.25.0", "recorded_at": "2026-01-02T10:00:00+00:00"}},
                "release needs version equal to base",
            ),
            ("version_line", {**LINE, "release": "x"}, "release needs version equal to base"),
            ("merge", "x", "must be an object"),
            ("merge", {**MERGE, "state": "later"}, "needs state merged or declined"),
            ("merge", {**MERGE, "response": ""}, "needs state merged or declined"),
            ("merge", {**MERGE, "recorded_at": "x"}, "needs state merged or declined"),
            ("merge", MERGE, "needs a pull_request"),
        ],
    )
    def test_malformed_fields(self, field: str, value: Any, fragment: str) -> None:
        assert any(fragment in problem for problem in self.errors(**{field: value}))

    def test_exclusions_and_merged_requirements(self) -> None:
        exempt = {"source": "user", "response": "none"}
        assert any(
            "exclude each other" in p
            for p in self.errors(pull_request=PR, pr_exemption={"reason": "no_remote", **exempt})
        )
        assert any("exclude each other" in p for p in self.errors(version_line=LINE, version_exemption=exempt))
        incomplete = {**MERGE, "method": "other", "evidence": ""}
        assert any("method and evidence" in p for p in self.errors(pull_request=PR, merge=incomplete))
        assert any("needs a release" in p for p in self.errors(pull_request=PR, version_line=LINE, merge=MERGE))
        declined = {**MERGE, "state": "declined", "method": None, "evidence": None}
        assert self.errors(pull_request=PR, version_line=LINE, merge=declined) == []

    def test_state_validation_reports_the_shape_and_unknown_fields(self, tmp_path: Path) -> None:
        errors = errors_for(tmp_path, [entry(pull_request={"host": "github"}, surprise=1)])
        assert any("unexpected fields: surprise" in e for e in errors)
        assert any("pull_request: missing number" in e for e in errors)
        assert not any("pull_request" in e for e in errors_for(tmp_path, [entry(pull_request=PR)]))


class TestGates:
    def test_the_gate_is_off_before_the_cutoff_and_for_unjudgeable_states(self, monkeypatch: Any) -> None:
        state = {"created": "2026-06-01T10:00:00+00:00"}
        monkeypatch.setattr(workspace_lib, "DELIVERY_GATES_FROM", "2099-01-01T00:00:00+00:00")
        assert is_delivery_gated(state) is False
        monkeypatch.setattr(workspace_lib, "DELIVERY_GATES_FROM", "2026-01-01T00:00:00+00:00")
        assert is_delivery_gated(state) is True
        assert is_delivery_gated({"created": "2025-06-01T10:00:00+00:00"}) is False
        assert is_delivery_gated({"created": "not a time"}) is False

    def test_a_project_before_the_cutoff_is_not_gated(self, recorded: Path) -> None:
        state = load_state(recorded)
        assert start_findings(state) == [] and closure_findings(state) == []

    def write_plan(self, project: Path, *, first_writes: bool = True) -> None:
        planned(project)
        first = task("T01")
        if first_writes:
            first["outputs"] = [{"root": "target", "path": "x.txt", "required": False}]
            first["effect"] = {"kind": "local_write", "description": "Edit x"}
        second = task("T02", depends_on=["T01"])
        second["outputs"] = [{"root": "target", "path": "y.txt", "required": False}]
        second["effect"] = {"kind": "local_write", "description": "Edit y"}
        update_project_data(project, {"tasks": [first, second]}, expected_revision=1)

    def planned_project(self, tmp_path: Path, repository: Path, linked: Path, *, first_writes: bool = True) -> Path:
        git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
        project = make_project(tmp_path, repository)
        self.write_plan(project, first_writes=first_writes)
        record(project, linked)
        return project

    def test_a_task_that_writes_needs_the_mr_and_the_alpha_line(
        self, tmp_path: Path, repository: Path, linked: Path, gated: None
    ) -> None:
        project = self.planned_project(tmp_path, repository, linked)

        def revision() -> int:
            return load_state(project)["revision"]

        with pytest.raises(WorkspaceError, match=r"no merge request.*no alpha line"):
            task_operation(project, "start", "T01", expected_revision=revision())
        op(project, linked, "pull_request", host="github", url=URL, number=7)
        with pytest.raises(WorkspaceError, match="no alpha line"):
            task_operation(project, "start", "T01", expected_revision=revision())
        alpha(project, linked, 1)
        started = task_operation(project, "start", "T01", expected_revision=revision())
        assert started["status"] == "EXECUTING"

    def test_a_task_with_no_target_output_starts_without_them(
        self, tmp_path: Path, repository: Path, linked: Path, gated: None
    ) -> None:
        project = self.planned_project(tmp_path, repository, linked, first_writes=False)
        task_operation(project, "start", "T01", expected_revision=load_state(project)["revision"])

    def test_start_next_is_held_to_the_same_rule(
        self, tmp_path: Path, repository: Path, linked: Path, gated: None
    ) -> None:
        project = self.planned_project(tmp_path, repository, linked, first_writes=False)
        task_operation(project, "start", "T01", expected_revision=load_state(project)["revision"])
        result = workflow(project, "verify", {"id": "check", "checks": [{"task": "T01", "argv": ["true"]}]})
        record_id = result["attempts"][0]["result"]["record_id"]
        before = load_state(project)
        with pytest.raises(WorkspaceError, match="task T02 writes to the target"):
            task_operation(
                project,
                "finish",
                "T01",
                expected_revision=before["revision"],
                evidence_record_ids=[record_id],
                start_next="T02",
            )
        assert load_state(project) == before

    def test_an_exempt_worktree_starts_and_closes_without_a_merge(
        self, tmp_path: Path, repository: Path, linked: Path, gated: None
    ) -> None:
        project = make_project(tmp_path, repository)
        self.write_plan(project)
        record(project, linked)
        op(project, linked, "pull_request", exempt="no_remote")
        op(project, linked, "alpha", exempt=True)
        assert start_findings(load_state(project)) == []
        assert closure_findings(load_state(project)) == []

    def test_a_non_repository_target_and_removed_worktrees_are_skipped(self, tmp_path: Path, gated: None) -> None:
        state, _ = errors_state(tmp_path)
        assert start_findings(state) == [] and closure_findings(state) == []

    def test_closure_names_each_missing_record(self, recorded: Path, linked: Path, gated: None) -> None:
        findings = closure_findings(load_state(recorded))
        assert [f for f in findings if "no pull_request or pr_exemption" in f]
        assert [f for f in findings if "no version_line or version_exemption" in f]
        op(recorded, linked, "pull_request", host="github", url=URL, number=7)
        assert any("no merge decision" in f for f in closure_findings(load_state(recorded)))
        op(recorded, linked, "merge", merge_state="declined")
        op(recorded, linked, "alpha", exempt=True)
        assert closure_findings(load_state(recorded)) == []

    def test_finalize_is_refused_until_the_records_exist(
        self, tmp_path: Path, repository: Path, linked: Path, gated: None
    ) -> None:
        from tests.plugins.research.project.test_lifecycle_commands import request

        project = make_project(tmp_path, repository)
        done(project)
        record(project, linked)
        close(project, linked, "keep")
        payload = request(project, "final-1", reflection=REFLECTION, continuation={"next": "none"})
        with pytest.raises(WorkspaceError, match="no pull_request or pr_exemption"):
            workflow(project, "finalize", payload)
        assert load_state(project)["status"] != "DONE"


def errors_state(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    from tests.plugins.research.project.test_worktrees import state_with

    removed = entry(
        status="removed",
        closure={
            "decision": "remove",
            "dirty": [],
            "observed_at": "2026-01-02T10:00:00+00:00",
            "source": "s",
            "response": "r",
        },
    )
    none = entry(kind="none", repository=None, branch=None, base_commit=None, path="/t", role="additional")
    return state_with(tmp_path, [removed, none])
