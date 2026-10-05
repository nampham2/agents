"""Recorded worktrees: the schema field, its Git observations, and the guards built on them."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import workspace_context
from workspace_context import git_observation, worktree_observation, worktree_registered
from workspace_lib import allocate_project, validate_v3_state, validate_v4_state

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

REFLECTION = (
    "# Reflection\n\nOutcome: the work was verified end to end and every success criterion was met.\n\n"
    "Limitations: none known. Lessons: nothing new beyond what the evidence and the decisions already record, "
    "and no work is left open for a later session.\n"
)



def git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True, env=GIT_ENV
    )
    return completed.stdout.strip()


def make_repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir(parents=True)
    git(repository, "init", "-q", "-b", "main")
    (repository / "README.md").write_text("hello\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-q", "-m", "initial")
    return repository


def add_worktree(repository: Path, project_id: str = "2026-01-01-001", branch: str = "npham/2026-01-01-001-x") -> Path:
    path = repository.parent / f"{repository.name}.worktrees" / project_id
    git(repository, "worktree", "add", "-q", "-b", branch, str(path))
    return path


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


def load_state(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "repository": "/repo",
        "path": "/repo.worktrees/2026-01-01-001",
        "branch": "npham/2026-01-01-001-x",
        "base_commit": "abc123",
        "kind": "created",
        "role": "target",
        "status": "active",
        "recorded_at": "2026-01-01T10:00:00+00:00",
        "confirmation": {"source": "user reply", "response": "yes"},
        "closure": None,
    }
    base.update(overrides)
    return base


def closure(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision": "keep",
        "dirty": [],
        "observed_at": "2026-01-02T10:00:00+00:00",
        "source": "user reply",
        "response": "keep it",
    }
    base.update(overrides)
    return base


def state_with(
    tmp_path: Path, worktrees: object, *, working_directory: str | None = None, v3: bool = False
) -> tuple[dict[str, Any], Path]:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    project = allocate_project(root, title="Worktrees", working_directory=target)
    state = load_state(project)
    state["worktrees"] = worktrees
    if working_directory is not None:
        state["working_directory"] = working_directory
    if v3:
        state["schema_version"] = 3
        del state["execution"]
    return state, project


def errors_for(tmp_path: Path, worktrees: object, **kwargs: Any) -> list[str]:
    v3 = kwargs.pop("v3", False)
    check_files = kwargs.pop("check_files", False)
    state, project = state_with(tmp_path, worktrees, v3=v3, **kwargs)
    validator = validate_v3_state if v3 else validate_v4_state
    return validator(state, project, check_files=check_files).errors


class TestWorktreeSchema:
    def test_legacy_state_without_the_field_is_valid(self, tmp_path: Path) -> None:
        state, project = state_with(tmp_path, [])
        del state["worktrees"]
        assert validate_v4_state(state, project, check_files=True).errors == []

    @pytest.mark.parametrize("v3", [False, True])
    def test_target_entry_agrees_with_working_directory(self, tmp_path: Path, v3: bool) -> None:
        assert errors_for(tmp_path, [entry()], working_directory="/repo.worktrees/2026-01-01-001", v3=v3) == []
        mismatch = errors_for(tmp_path, [entry()], v3=v3)
        assert any("requires working_directory /repo.worktrees/2026-01-01-001" in e for e in mismatch)

    def test_removed_target_points_back_at_the_repository(self, tmp_path: Path) -> None:
        removed = entry(status="removed", closure=closure(decision="remove", response="remove it"))
        assert errors_for(tmp_path, [removed], working_directory="/repo") == []
        wrong = errors_for(tmp_path, [removed], working_directory="/repo.worktrees/2026-01-01-001")
        assert any("requires working_directory /repo" in e for e in wrong)

    def test_additional_entries_do_not_constrain_the_target(self, tmp_path: Path) -> None:
        extra = entry(role="additional", path="/other.worktrees/2026-01-01-001", repository="/other")
        assert errors_for(tmp_path, [extra]) == []

    def test_kind_none_records_a_non_repository_target(self, tmp_path: Path) -> None:
        none = entry(kind="none", repository=None, branch=None, base_commit=None, path="/target")
        assert errors_for(tmp_path, [none]) == []
        bad = errors_for(tmp_path, [entry(kind="none", path="/target", status="kept", closure=closure())])
        assert any("must be null when kind is none" in e for e in bad)
        assert any("stays active with no closure" in e for e in bad)

    @pytest.mark.parametrize(
        "worktrees,fragment",
        [
            ("nope", "worktrees must be a list"),
            (["nope"], "must be an object"),
            ([entry(extra=1)], "unexpected"),
            ([entry(kind="cloned")], "kind must be one of"),
            ([entry(role="main")], "role must be one of"),
            ([entry(status="gone")], "status must be one of"),
            ([entry(path="relative/path")], "path must be an absolute path"),
            ([entry(repository="relative")], "repository must be an absolute path"),
            ([entry(branch="")], "branch must be a non-empty string"),
            ([entry(base_commit=None)], "base_commit must be a non-empty string"),
            ([entry(recorded_at="yesterday")], "recorded_at must be timezone-aware"),
            ([entry(confirmation="yes")], "confirmation must be an object"),
            ([entry(confirmation={"source": "", "response": "yes"})], "confirmation: source must be"),
            ([entry(confirmation={"source": "x", "response": "yes", "extra": 1})], "confirmation: unexpected"),
            ([entry(status="kept")], "closure must be an object once"),
            ([entry(closure=closure())], "closure must be null while the worktree is active"),
            ([entry(status="kept", closure=closure(decision="drop"))], "decision must be one of"),
            ([entry(status="kept", closure=closure(decision="remove"))], "does not match status"),
            ([entry(status="kept", closure=closure(dirty=["?? new.txt"]))], "requires an empty dirty snapshot"),
            ([entry(status="kept", closure=closure(dirty="M a"))], "dirty must be a list"),
            ([entry(status="kept", closure=closure(observed_at="now"))], "observed_at must be"),
            ([entry(status="kept", closure=closure(response=""))], "closure: response must be"),
            ([entry(status="kept", closure=closure(nonsense=1))], "closure: unexpected"),
        ],
    )
    def test_invalid_entries_produce_findings(self, tmp_path: Path, worktrees: object, fragment: str) -> None:
        errors = errors_for(tmp_path, worktrees, working_directory="/repo.worktrees/2026-01-01-001")
        assert any(fragment in e for e in errors), errors

    def test_duplicate_paths_and_second_targets_are_refused(self, tmp_path: Path) -> None:
        errors = errors_for(
            tmp_path,
            [entry(), entry(role="additional"), entry(repository="/other", path="/other.worktrees/x")],
            working_directory="/repo.worktrees/2026-01-01-001",
        )
        assert any("duplicate worktree path" in e for e in errors)
        assert any("at most one worktree may have role target" in e for e in errors)

    @pytest.mark.parametrize("v3", [False, True])
    def test_a_removed_target_followed_by_a_new_target_is_valid(self, tmp_path: Path, v3: bool) -> None:
        first = entry(path="/repo.worktrees/a", status="removed", closure=closure(decision="remove"))
        second = entry(path="/repo.worktrees/b")
        errors = errors_for(tmp_path, [first, second], working_directory="/repo.worktrees/b", v3=v3)
        assert errors == []

    def test_any_number_of_removed_targets_may_precede_the_current_one(self, tmp_path: Path) -> None:
        removed = [
            entry(path=f"/repo.worktrees/{name}", status="removed", closure=closure(decision="remove"))
            for name in ("a", "b")
        ]
        current = entry(path="/repo.worktrees/c")
        assert errors_for(tmp_path, [*removed, current], working_directory="/repo.worktrees/c") == []

    @pytest.mark.parametrize("earlier_status", ["active", "kept"])
    def test_an_earlier_target_must_be_removed_before_another_is_recorded(
        self, tmp_path: Path, earlier_status: str
    ) -> None:
        earlier = entry(
            path="/repo.worktrees/a", status=earlier_status, closure=closure() if earlier_status == "kept" else None
        )
        errors = errors_for(tmp_path, [earlier, entry(path="/repo.worktrees/b")], working_directory="/repo.worktrees/b")
        assert any(
            "at most one worktree may have role target" in e and "worktree #1 must be removed" in e for e in errors
        ), errors

    def test_only_the_latest_target_decides_the_target_root(self, tmp_path: Path) -> None:
        removed_first = entry(path="/repo.worktrees/a", status="removed", closure=closure(decision="remove"))
        removed_last = entry(path="/repo.worktrees/b", status="removed", closure=closure(decision="remove"))
        assert errors_for(tmp_path, [removed_first, removed_last], working_directory="/repo") == []
        errors = errors_for(tmp_path, [removed_first, removed_last], working_directory="/repo.worktrees/a")
        assert any("worktree #2" in e and "requires working_directory /repo" in e for e in errors), errors

    def test_missing_directory_only_warns_with_check_files(self, tmp_path: Path) -> None:
        state, project = state_with(tmp_path, [entry(role="additional")])
        report = validate_v4_state(state, project, check_files=True)
        assert report.errors == []
        assert any("missing on disk" in w for w in report.warnings)
        removed = entry(role="additional", status="removed", closure=closure(decision="remove"))
        state["worktrees"] = [removed]
        assert not any("missing on disk" in w for w in validate_v4_state(state, project, check_files=True).warnings)


class TestWorktreeObservation:
    def test_missing_directory_and_plain_directory(self, tmp_path: Path) -> None:
        missing = worktree_observation(tmp_path / "absent")
        assert missing["available"] is True and missing["exists"] is False
        plain = worktree_observation(tmp_path)
        assert plain["exists"] is True and plain["is_repository"] is False and plain["repository"] is None

    def test_main_checkout_and_linked_worktree(self, repository: Path, linked: Path) -> None:
        main = worktree_observation(repository)
        assert main["is_repository"] and not main["is_worktree"]
        assert main["repository"] == str(repository.resolve()) == main["toplevel"]
        assert main["branch"] == "main" and main["dirty"] == []
        (linked / "new.txt").write_text("x\n", encoding="utf-8")
        seen = worktree_observation(linked)
        assert seen["is_worktree"] is True
        assert seen["repository"] == str(repository.resolve())
        assert seen["toplevel"] == str(linked.resolve())
        assert seen["branch"] == "npham/2026-01-01-001-x"
        assert seen["head"] == git(repository, "rev-parse", "HEAD")
        assert seen["dirty"] == ["?? new.txt"]
        # A subdirectory is inside the worktree but is not its root; callers compare toplevel.
        (linked / "sub").mkdir()
        assert worktree_observation(linked / "sub")["toplevel"] == str(linked.resolve())

    def test_status_column_keeps_its_leading_space(self, linked: Path) -> None:
        (linked / "README.md").write_text("changed\n", encoding="utf-8")
        assert worktree_observation(linked)["dirty"] == [" M README.md"]
        assert git_observation(linked)["dirty"] == "M README.md"

    def test_git_failures_degrade_to_unavailable(self, tmp_path: Path, linked: Path) -> None:
        with patch.object(workspace_context.subprocess, "run", side_effect=OSError("no git")):
            assert worktree_observation(linked)["available"] is False
            assert worktree_registered(linked, linked) is None
        real = workspace_context._git

        def flaky(target: Path, *arguments: str) -> str | None:
            return None if arguments[0] == "branch" else real(target, *arguments)

        with patch.object(workspace_context, "_git", side_effect=flaky):
            assert worktree_observation(linked)["available"] is False
        assert worktree_registered(tmp_path, linked) is None

    def test_worktree_registration(self, repository: Path, linked: Path, tmp_path: Path) -> None:
        assert worktree_registered(repository, linked) is True
        assert worktree_registered(repository, tmp_path / "elsewhere") is False
        git(repository, "worktree", "remove", "--force", str(linked))
        assert worktree_registered(repository, linked) is False


# --- workflow action -------------------------------------------------------------------------


def make_project(tmp_path: Path, target: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return allocate_project(root, title="Worktree action", working_directory=target)


OPERATION_IDS = itertools.count(1)


def run(project: Path, request: dict[str, Any], **fields: Any) -> dict[str, Any]:
    from workspace_journal import snapshot
    from workspace_workflows import workflow

    revision = load_state(project)["revision"]
    names = [name for name in ("handoff", "spec") if (project / f"{name}.md").exists()]
    tokens = {name: snapshot(project / f"{name}.md")[1] for name in names}
    request = {"id": f"op-{next(OPERATION_IDS)}", "expected_revision": revision, "tokens": tokens, **request, **fields}
    return workflow(project, "worktree", request)


def record(project: Path, path: Path, **fields: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "operation": "record",
        "path": str(path),
        "branch": "npham/2026-01-01-001-x",
        "confirmation": {"source": "user reply", "response": "yes"},
    }
    request.update(fields)
    return run(project, request)


def close(project: Path, path: Path, decision: str, **fields: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "operation": "close",
        "path": str(path),
        "decision": decision,
        "confirmation": {"source": "user reply", "response": decision},
    }
    request.update(fields)
    return run(project, request)


def refused(project: Path, fragment: str, call: Any, *args: Any, **kwargs: Any) -> None:
    from workspace_lib import WorkspaceError

    before = load_state(project)
    with pytest.raises(WorkspaceError, match=fragment):
        call(project, *args, **kwargs)
    assert load_state(project) == before


class TestRecordWorktree:
    def test_created_target_is_recorded_and_repointed(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        result = record(project, linked, repository=str(repository), continuation={"next": "write"})
        state = load_state(project)
        assert state["working_directory"] == str(linked.resolve())
        assert state["revision"] == 1
        entry = state["worktrees"][0]
        assert entry["kind"] == "created" and entry["role"] == "target" and entry["status"] == "active"
        assert entry["repository"] == str(repository.resolve())
        assert entry["base_commit"] == git(repository, "rev-parse", "HEAD")
        assert entry["confirmation"] == {"source": "user reply", "response": "yes"}
        assert result["worktree"] == entry and result["observed"]["is_worktree"] is True
        assert (project / "handoff.md").exists()
        # Recording again from the worktree itself is allowed (a bootstrap that repointed by hand),
        # but the same path cannot be recorded twice.
        refused(project, "already recorded", record, linked)

    def test_record_from_the_worktree_itself(self, tmp_path: Path, linked: Path) -> None:
        project = make_project(tmp_path, linked)
        record(project, linked)
        assert load_state(project)["working_directory"] == str(linked.resolve())

    def test_record_refusals(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        (linked / "sub").mkdir()
        refused(project, "branch mismatch", record, linked, branch="other")
        refused(project, "repository mismatch", record, linked, repository=str(tmp_path))
        refused(project, "not the root of a Git working tree", record, linked / "sub")
        refused(project, "not the root of a Git working tree", record, tmp_path / "absent")
        refused(project, "main checkout, not a linked worktree", record, repository)
        from_worktree = make_project(tmp_path / "p2", linked)
        refused(from_worktree, "cannot be the target root", record, linked, role="additional")
        refused(project, "kind must be", record, linked, kind="cloned")
        refused(project, "kind must be", record, linked, role="main")
        refused(project, "must be record, close, pull_request", run, {"operation": "prune", "path": str(linked),
                                                             "confirmation": {"source": "s", "response": "r"}})
        refused(project, "must be absolute", record, Path("relative"))
        other = make_repository(tmp_path / "elsewhere")
        stray = add_worktree(other)
        refused(project, "must be recorded from its repository", record, stray)
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            refused(project, "could not be consulted", record, linked)
        seen = worktree_observation(linked)
        with patch("workspace_workflows.worktree_observation", return_value={**seen, "head": ""}):
            refused(project, "no commit to record", record, linked)

    def test_additional_repository(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        other = make_repository(tmp_path / "elsewhere")
        stray = add_worktree(other)
        record(project, stray, role="additional")
        state = load_state(project)
        assert state["working_directory"] == str(repository)
        assert state["worktrees"][0]["role"] == "additional"
        record(project, linked)
        assert load_state(project)["working_directory"] == str(linked.resolve())

    def test_existing_worktree_target(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, linked)
        record(project, linked, kind="existing")
        assert load_state(project)["worktrees"][0]["kind"] == "existing"
        other = make_project(tmp_path / "second", repository)
        refused(other, "must be recorded from its repository", record, linked, kind="existing")

    def test_non_repository_target(self, tmp_path: Path, repository: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        project = make_project(tmp_path, plain)
        refused(project, "is not a repository", record, plain, kind="none", role="additional")
        record(project, plain, kind="none")
        state = load_state(project)
        assert state["working_directory"] == str(plain)
        assert state["worktrees"][0] == {
            **state["worktrees"][0], "kind": "none", "repository": None, "branch": None, "base_commit": None,
        }
        refused(project, "already recorded", record, plain, kind="none")
        repo_project = make_project(tmp_path / "second", repository)
        refused(repo_project, "is not a repository", record, repository, kind="none")

    def test_update_cannot_declare_worktrees(self, tmp_path: Path, repository: Path) -> None:
        from workspace_lib import WorkspaceError
        from workspace_session import update_project_data

        project = make_project(tmp_path, repository)
        with pytest.raises(WorkspaceError, match="unsupported update fields: working_directory, worktrees"):
            update_project_data(project, {"worktrees": [], "working_directory": "/tmp"}, expected_revision=0)


class TestCloseWorktree:
    @pytest.fixture
    def recorded(self, tmp_path: Path, repository: Path, linked: Path) -> Path:
        project = make_project(tmp_path, repository)
        record(project, linked)
        return project

    def test_keep_clean(self, recorded: Path, linked: Path) -> None:
        result = close(recorded, linked, "keep")
        entry = load_state(recorded)["worktrees"][0]
        assert entry["status"] == "kept" and entry["closure"]["decision"] == "keep"
        assert entry["closure"]["dirty"] == [] and entry["closure"]["response"] == "keep"
        assert result["observed"]["dirty"] == []

    def test_keep_dirty_needs_acceptance(self, recorded: Path, linked: Path) -> None:
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        refused(recorded, r"uncommitted changes.*\n\?\? wip.txt", close, linked, "keep")
        close(recorded, linked, "accept_dirty")
        entry = load_state(recorded)["worktrees"][0]
        assert entry["status"] == "kept" and entry["closure"]["dirty"] == ["?? wip.txt"]

    def test_remove_requires_the_agent_to_have_removed_it(self, recorded: Path, repository: Path, linked: Path) -> None:
        refused(recorded, "still present or registered", close, linked, "remove")
        git(repository, "worktree", "remove", "--force", str(linked))
        close(recorded, linked, "remove")
        state = load_state(recorded)
        assert state["worktrees"][0]["status"] == "removed"
        assert state["working_directory"] == str(repository.resolve())

    def test_missing_worktree_cannot_be_kept(self, recorded: Path, repository: Path, linked: Path) -> None:
        git(repository, "worktree", "remove", "--force", str(linked))
        refused(recorded, "worktree is missing", close, linked, "keep")

    def test_a_kept_worktree_can_later_be_recorded_as_removed(
        self, recorded: Path, repository: Path, linked: Path
    ) -> None:
        close(recorded, linked, "keep")
        refused(recorded, "still present or registered", close, linked, "remove")
        assert load_state(recorded)["worktrees"][0]["status"] == "kept", "a refused removal changes nothing"
        git(repository, "worktree", "remove", "--force", str(linked))
        result = close(recorded, linked, "remove", confirmation={"source": "user", "response": "removed it"})
        state = load_state(recorded)
        assert result["worktree"]["status"] == "removed"
        assert state["worktrees"][0]["closure"]["decision"] == "remove"
        assert state["worktrees"][0]["closure"]["response"] == "removed it"
        assert state["working_directory"] == str(repository.resolve())
        from workspace_lib import validate_project

        assert validate_project(recorded).valid
        refused(recorded, "no active or kept repository worktree", close, linked, "remove")

    def test_a_keep_can_be_superseded_and_the_first_decision_is_not_lost(self, recorded: Path, linked: Path) -> None:
        close(recorded, linked, "keep", confirmation={"source": "reply 1", "response": "keep it clean"})
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        result = close(
            recorded, linked, "accept_dirty", confirmation={"source": "reply 2", "response": "wip is fine"}
        )
        closure = load_state(recorded)["worktrees"][0]["closure"]
        assert closure["decision"] == "accept_dirty" and closure["dirty"] == ["?? wip.txt"]
        assert result["superseded"]["decision"] == "keep" and result["superseded"]["response"] == "keep it clean"
        history = (recorded / "spec.md").read_text(encoding="utf-8")
        assert "closure decision superseded" in history
        assert "'keep it clean'" in history and "'wip is fine'" in history

    def test_the_first_closure_leaves_no_supersession_entry(self, recorded: Path, linked: Path) -> None:
        result = close(recorded, linked, "keep")
        assert "superseded" not in result
        assert "superseded" not in (recorded / "spec.md").read_text(encoding="utf-8")

    def test_a_removal_after_a_keep_also_keeps_the_earlier_consent(
        self, recorded: Path, repository: Path, linked: Path
    ) -> None:
        close(recorded, linked, "keep", confirmation={"source": "reply 1", "response": "keep it"})
        git(repository, "worktree", "remove", "--force", str(linked))
        close(recorded, linked, "remove", confirmation={"source": "reply 2", "response": "gone"})
        history = (recorded / "spec.md").read_text(encoding="utf-8")
        assert "closure decision superseded" in history and "'keep it'" in history

    def test_a_removed_worktree_is_final(self, recorded: Path, repository: Path, linked: Path) -> None:
        git(repository, "worktree", "remove", "--force", str(linked))
        close(recorded, linked, "remove")
        for decision in ("keep", "accept_dirty", "remove"):
            refused(recorded, "no active or kept repository worktree", close, linked, decision)

    def test_replaying_a_supersession_adds_no_second_history_entry(self, recorded: Path, linked: Path) -> None:
        from workspace_workflows import workflow

        close(recorded, linked, "keep")
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        from workspace_journal import snapshot

        request = {
            "id": "again-1", "expected_revision": load_state(recorded)["revision"], "operation": "close",
            "tokens": {"spec": snapshot(recorded / "spec.md")[1]},
            "path": str(linked), "decision": "accept_dirty", "confirmation": {"source": "s", "response": "ok"},
        }
        first = workflow(recorded, "worktree", request)
        assert workflow(recorded, "worktree", request) == first
        assert (recorded / "spec.md").read_text(encoding="utf-8").count("closure decision superseded") == 1

    def test_close_refusals(self, recorded: Path, tmp_path: Path, linked: Path) -> None:
        refused(recorded, "decision must be", close, linked, "drop")
        refused(recorded, "no active or kept repository worktree", close, tmp_path / "absent", "keep")
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            refused(recorded, "could not be consulted", close, linked, "keep")
        plain = tmp_path / "plain"
        plain.mkdir()
        project = make_project(tmp_path / "second", plain)
        record(project, plain, kind="none")
        refused(project, "no active or kept repository worktree", close, plain, "keep")

    def test_same_operation_id_replays_and_changed_input_conflicts(self, recorded: Path, linked: Path) -> None:
        from workspace_lib import WorkspaceError
        from workspace_workflows import workflow

        request = {
            "id": "close-1", "expected_revision": 1, "operation": "close", "path": str(linked),
            "decision": "keep", "confirmation": {"source": "s", "response": "keep"},
        }
        first = workflow(recorded, "worktree", request)
        assert workflow(recorded, "worktree", request)["worktree"] == first["worktree"]
        with pytest.raises(WorkspaceError, match="already used with different input"):
            workflow(recorded, "worktree", {**request, "decision": "accept_dirty"})

    def test_cli_entry(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from tests.plugins.research.project.test_workflow_automation import invoke

        project = make_project(tmp_path, repository)
        payload = json.dumps({
            "id": "cli-1", "expected_revision": 0, "operation": "record", "path": str(linked),
            "branch": "npham/2026-01-01-001-x", "confirmation": {"source": "s", "response": "yes"},
        })
        code, out, _ = invoke(["workflow", str(project), "worktree", "-"], stdin=payload)
        assert code == 0, out
        assert json.loads(out)["worktree"]["path"] == str(linked.resolve())


# --- guards ----------------------------------------------------------------------------------


class TestGuards:
    def test_task_start_reports_worktree_status(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from workspace_operations import task_operation

        from tests.plugins.research.project.test_lifecycle_commands import planned

        project = make_project(tmp_path, repository)
        planned(project)
        started = task_operation(project, "start", "T01", expected_revision=1)
        assert started["worktree"]["is_repository"] is True and started["worktree"]["recorded"] is None
        assert any("no recorded worktree" in w and "references/worktrees.md" in w for w in started["warnings"])
        record(project, linked)
        restarted = task_operation(project, "start", "T01", expected_revision=3)
        assert restarted["warnings"] == []
        assert restarted["worktree"]["recorded"]["path"] == str(linked.resolve())
        assert restarted["roots"]["target"] == str(linked.resolve())

    def test_status_variants(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from workspace_context import worktree_status

        plain = tmp_path / "plain"
        plain.mkdir()
        project = make_project(tmp_path, plain)
        state = load_state(project)
        assert worktree_status(state)["warnings"] == []
        record(project, plain, kind="none")
        assert worktree_status(load_state(project))["recorded"]["kind"] == "none"
        in_worktree = make_project(tmp_path / "second", linked)
        status = worktree_status(load_state(in_worktree))
        assert status["is_worktree"] is True
        assert "Git linked worktree with no recorded worktree" in status["warnings"][0]
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            unknown = worktree_status(load_state(in_worktree))
        assert unknown["is_repository"] is None and "worktree state is unknown" in unknown["warnings"][0]
        other = make_repository(tmp_path / "elsewhere")
        stray = add_worktree(other)
        record(in_worktree, stray, role="additional")
        additional = worktree_status(load_state(in_worktree))["additional"]
        assert [item["path"] for item in additional] == [str(stray.resolve())]

    def test_resume_bundle_and_checkpoint_carry_worktrees(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from workspace_context import checkpoint_data
        from workspace_workflows import workflow

        project = make_project(tmp_path, repository)
        record(project, linked, continuation={"next": "write"})
        metadata = checkpoint_data(project)
        assert metadata is not None
        assert metadata["worktrees"] == [
            {"path": str(linked.resolve()), "branch": "npham/2026-01-01-001-x", "status": "active", "dirty": 0}
        ]
        bundle = workflow(project, "resume", {})
        assert bundle["worktree"]["recorded"]["path"] == str(linked.resolve())
        assert bundle["freshness"]["target"] == "unchanged"
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        close(project, linked, "accept_dirty", continuation={"next": "close"})
        refreshed = checkpoint_data(project)
        assert refreshed is not None and refreshed["worktrees"][0] == {
            **refreshed["worktrees"][0], "status": "kept", "dirty": 1,
        }
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            from workspace_context import worktree_summaries

            assert worktree_summaries(load_state(project))[0]["dirty"] is None

    def test_readiness_and_finalize_guard_closure(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from workspace_lib import WorkspaceError
        from workspace_workflows import workflow

        from tests.plugins.research.project.test_lifecycle_commands import done, request

        project = make_project(tmp_path, repository)
        done(project)
        record(project, linked)

        def finalize(identity: str) -> dict[str, Any]:
            payload = request(project, identity, reflection=REFLECTION, continuation={"next": "none"})
            return workflow(project, "finalize", payload)

        groups = workflow(project, "readiness", {})["groups"]
        assert any("no closure decision" in item["finding"] for item in groups["worktree"])
        with pytest.raises(WorkspaceError, match="closure blocked by recorded worktrees"):
            finalize("final-1")
        assert load_state(project)["status"] != "DONE"
        close(project, linked, "keep")
        (linked / "late.txt").write_text("x\n", encoding="utf-8")
        groups = workflow(project, "readiness", {})["groups"]
        assert any("kept as clean but now has uncommitted changes" in item["finding"] for item in groups["worktree"])
        with pytest.raises(WorkspaceError, match="now has uncommitted changes"):
            finalize("final-2")
        (linked / "late.txt").unlink()
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            groups = workflow(project, "readiness", {})["groups"]
        assert any("could not be observed" in item["finding"] for item in groups["worktree"])
        result = finalize("final-3")
        assert result["worktree_warnings"] == [] and load_state(project)["status"] == "DONE"

    def test_missing_kept_worktree_only_warns(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        from workspace_context import worktree_closure_findings

        project = make_project(tmp_path, repository)
        record(project, linked)
        close(project, linked, "keep")
        git(repository, "worktree", "remove", "--force", str(linked))
        errors, warnings = worktree_closure_findings(load_state(project))
        assert errors == [] and "missing on disk" in warnings[0]
        assert "close, decision remove" in warnings[0], "the advice must name a transition that now exists"
        plain = tmp_path / "plain"
        plain.mkdir()
        exempt = make_project(tmp_path / "second", plain)
        record(exempt, plain, kind="none")
        assert worktree_closure_findings(load_state(exempt)) == ([], [])


class TestFinalizeReflection:
    @pytest.fixture
    def finished(self, tmp_path: Path, repository: Path) -> Path:
        from tests.plugins.research.project.test_lifecycle_commands import done

        project = make_project(tmp_path, repository)
        done(project)
        return project

    def finalize(self, project: Path, identity: str, reflection: str) -> dict[str, Any]:
        from workspace_workflows import workflow

        from tests.plugins.research.project.test_lifecycle_commands import request

        payload = request(project, identity, reflection=reflection, continuation={"next": "none"})
        return workflow(project, "finalize", payload)

    @pytest.mark.parametrize(
        "reflection",
        [
            "aae983d8" * 8,  # a bare 64-character hash was once saved as a reflection
            "# Reflection\n\nToo short.",
            "No heading here, " + "but it is long enough to pass the length rule. " * 6,
            "```\n# only inside a fence\n```\n" + "padding " * 40,
        ],
    )
    def test_a_token_a_stub_or_a_headingless_text_is_refused_and_nothing_is_committed(
        self, finished: Path, reflection: str
    ) -> None:
        from workspace_lib import WorkspaceError

        before = (load_state(finished)["revision"], (finished / "reflection.md").exists())
        with pytest.raises(WorkspaceError, match="post-mortem of at least 200 characters"):
            self.finalize(finished, "bad", reflection)
        assert (load_state(finished)["revision"], (finished / "reflection.md").exists()) == before
        assert load_state(finished)["status"] != "DONE"

    def test_a_real_reflection_closes_and_staged_lessons_are_surfaced_not_blocking(self, finished: Path) -> None:
        (finished / "memory-staging.md").write_text(
            "# Staged lessons\n\n## A lesson nobody triaged\n\nSomething surprising.\n", encoding="utf-8"
        )
        result = self.finalize(finished, "good", REFLECTION)
        assert load_state(finished)["status"] == "DONE"
        assert result["memory_staging_warnings"], "an untriaged staged lesson must be reported at finalize"

    def test_a_clean_staging_file_reports_nothing(self, finished: Path) -> None:
        assert self.finalize(finished, "clean", REFLECTION)["memory_staging_warnings"] == []


class TestRetargetAfterRemoval:
    """The first maintenance reopen after a worktree was removed had no legal way to record the next one."""

    @pytest.fixture
    def removed(self, tmp_path: Path, repository: Path, linked: Path) -> Path:
        project = make_project(tmp_path, repository)
        record(project, linked)
        git(repository, "worktree", "remove", "--force", str(linked))
        close(project, linked, "remove")
        assert load_state(project)["working_directory"] == str(repository.resolve())
        return project

    def test_a_new_target_can_be_recorded_after_the_first_was_removed(self, removed: Path, repository: Path) -> None:
        second = add_worktree(repository, "second", "npham/second")
        record(removed, second, branch="npham/second")
        state = load_state(removed)
        assert state["working_directory"] == str(second.resolve())
        assert [w["status"] for w in state["worktrees"]] == ["removed", "active"]
        assert [w["role"] for w in state["worktrees"]] == ["target", "target"]
        from workspace_lib import validate_project

        assert not [e for e in validate_project(removed).errors if "worktree" in e], "the history must validate"

    def test_the_new_target_can_itself_be_closed_and_the_root_goes_back_to_the_repository(
        self, removed: Path, repository: Path
    ) -> None:
        second = add_worktree(repository, "second", "npham/second")
        record(removed, second, branch="npham/second")
        git(repository, "worktree", "remove", "--force", str(second))
        close(removed, second, "remove")
        state = load_state(removed)
        assert state["working_directory"] == str(repository.resolve())
        assert [w["status"] for w in state["worktrees"]] == ["removed", "removed"]

    def test_a_removed_path_cannot_be_reused_and_the_message_says_what_to_do(
        self, removed: Path, repository: Path
    ) -> None:
        again = add_worktree(repository, branch="npham/again")  # the same path as the removed worktree
        refused(removed, "a new worktree needs a new path", record, again, branch="npham/again")

    @pytest.mark.parametrize("earlier", ["active", "kept"])
    def test_a_second_target_is_refused_while_the_first_still_stands(
        self, tmp_path: Path, repository: Path, linked: Path, earlier: str
    ) -> None:
        project = make_project(tmp_path, repository)
        record(project, linked)
        if earlier == "kept":
            close(project, linked, "keep")
        second = add_worktree(repository, "second", "npham/second")
        refused(project, "must be recorded from its repository", record, second, branch="npham/second")


CLOSING_ENTRIES = ["close", "update", "finalize"]


class TestEveryClosingEntryPointAgrees:
    """`finalize` refused an active worktree while `close` and `update` committed DONE over it.

    The rule now lives in close-mode validation, which every way of closing runs, so the same
    project is open or closed whichever command asks. `readiness` and `--close` report the finding
    a commit would refuse.
    """

    @pytest.fixture
    def ready(self, tmp_path: Path, repository: Path, linked: Path) -> Path:
        from tests.plugins.research.project.test_lifecycle_commands import done

        project = make_project(tmp_path, repository)
        done(project)
        record(project, linked)
        (project / "reflection.md").write_text(REFLECTION, encoding="utf-8")
        return project

    def attempt(self, project: Path, entry: str) -> Any:
        from workspace_operations import close_project
        from workspace_session import update_project_data
        from workspace_workflows import workflow

        from tests.plugins.research.project.test_lifecycle_commands import request

        revision = load_state(project)["revision"]
        if entry == "close":
            return close_project(project, expected_revision=revision)
        if entry == "update":
            return update_project_data(project, {"status": "DONE"}, expected_revision=revision)
        payload = request(project, f"final-{revision}", reflection=REFLECTION, continuation={"next": "none"})
        return workflow(project, "finalize", payload)

    @pytest.mark.parametrize("entry", CLOSING_ENTRIES)
    def test_an_active_worktree_blocks_every_way_of_closing(self, ready: Path, entry: str) -> None:
        refused(ready, "no closure decision", self.attempt, entry)
        assert load_state(ready)["status"] != "DONE"

    @pytest.mark.parametrize("entry", CLOSING_ENTRIES)
    def test_a_kept_clean_worktree_that_turned_dirty_blocks_every_way_of_closing(
        self, ready: Path, linked: Path, entry: str
    ) -> None:
        close(ready, linked, "keep")
        (linked / "late.txt").write_text("x\n", encoding="utf-8")
        refused(ready, "kept as clean but now has uncommitted changes", self.attempt, entry)

    @pytest.mark.parametrize("entry", CLOSING_ENTRIES)
    def test_a_decided_worktree_lets_every_way_of_closing_through(self, ready: Path, linked: Path, entry: str) -> None:
        close(ready, linked, "keep")
        self.attempt(ready, entry)
        assert load_state(ready)["status"] == "DONE"

    def test_readiness_and_close_validation_report_what_a_commit_refuses(self, ready: Path) -> None:
        from workspace_lib import validate_project
        from workspace_workflows import workflow

        groups = workflow(ready, "readiness", {})["groups"]
        findings = [item["finding"] for item in groups["worktree"]]
        assert len([finding for finding in findings if "no closure decision" in finding]) == 1
        assert any("no closure decision" in error for error in validate_project(ready, close=True).errors)
        assert not any("closure decision" in error for error in validate_project(ready).errors)

    def test_a_project_that_is_already_done_is_not_rejudged_by_later_commits(
        self, ready: Path, linked: Path
    ) -> None:
        from workspace_lib import validate_project
        from workspace_session import update_project_data

        close(ready, linked, "keep")
        self.attempt(ready, "close")
        (linked / "after.txt").write_text("x\n", encoding="utf-8")
        update_project_data(ready, {"title": "Renamed after closure"}, expected_revision=load_state(ready)["revision"])
        assert load_state(ready)["title"] == "Renamed after closure"
        assert any("now has uncommitted changes" in error for error in validate_project(ready, close=True).errors)

    @pytest.mark.parametrize("v3", [False, True])
    def test_both_validators_apply_the_rule_and_a_caller_can_opt_out(self, tmp_path: Path, v3: bool) -> None:
        state, project = state_with(tmp_path, [entry(role="additional")], v3=v3)
        validator = validate_v3_state if v3 else validate_v4_state
        assert any("no closure decision" in error for error in validator(state, project, close=True).errors)
        opted_out = validator(state, project, close=True, worktree_closure=False).errors
        assert not any("closure decision" in error for error in opted_out)
        assert not any("closure decision" in error for error in validator(state, project, close=False).errors)


class TestAcceptedDirtyIsScopedToWhatTheUserSaw:
    """Both observed F4 symptoms: a kept entry could not be re-decided, and one accepted snapshot covered anything."""

    @pytest.fixture
    def recorded(self, tmp_path: Path, repository: Path, linked: Path) -> Path:
        project = make_project(tmp_path, repository)
        record(project, linked)
        return project

    def findings(self, project: Path) -> list[str]:
        from workspace_context import worktree_closure_findings

        return worktree_closure_findings(load_state(project))[0]

    def test_readiness_advice_to_accept_dirty_can_be_followed_after_a_keep(
        self, recorded: Path, linked: Path
    ) -> None:
        close(recorded, linked, "keep")
        (linked / "late.txt").write_text("x\n", encoding="utf-8")
        assert any("record accept_dirty" in finding for finding in self.findings(recorded))
        close(recorded, linked, "accept_dirty")
        assert self.findings(recorded) == []

    def test_a_file_added_after_the_acceptance_is_a_finding(self, recorded: Path, linked: Path) -> None:
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        close(recorded, linked, "accept_dirty")
        assert self.findings(recorded) == []
        (linked / "late.txt").write_text("x\n", encoding="utf-8")
        [finding] = self.findings(recorded)
        assert "beyond the accepted dirty snapshot" in finding and "late.txt" in finding and "wip.txt" not in finding
        close(recorded, linked, "accept_dirty")
        assert self.findings(recorded) == []

    def test_committing_or_staging_accepted_paths_is_not_new_work(self, recorded: Path, linked: Path) -> None:
        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        close(recorded, linked, "accept_dirty")
        git(linked, "add", "wip.txt")
        assert self.findings(recorded) == [], "staging changes the status columns, not the path"
        git(linked, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "wip")
        assert self.findings(recorded) == []

    def test_the_closure_commands_refuse_while_a_path_is_beyond_the_snapshot(
        self, recorded: Path, linked: Path
    ) -> None:
        from workspace_lib import validate_project

        (linked / "wip.txt").write_text("x\n", encoding="utf-8")
        close(recorded, linked, "accept_dirty")
        (linked / "late.txt").write_text("x\n", encoding="utf-8")
        assert any("beyond the accepted" in error for error in validate_project(recorded, close=True).errors)
