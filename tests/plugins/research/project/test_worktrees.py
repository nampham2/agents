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
    tokens = {name: snapshot(project / f"{name}.md")[1] for name in ("handoff",) if (project / f"{name}.md").exists()}
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
        refused(project, "must be record or close", run, {"operation": "prune", "path": str(linked),
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
        refused(recorded, "no active repository worktree", close, linked, "keep")

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

    def test_close_refusals(self, recorded: Path, tmp_path: Path, linked: Path) -> None:
        refused(recorded, "decision must be", close, linked, "drop")
        refused(recorded, "no active repository worktree", close, tmp_path / "absent", "keep")
        with patch.object(workspace_context, "_git", side_effect=OSError("gone")):
            refused(recorded, "could not be consulted", close, linked, "keep")
        plain = tmp_path / "plain"
        plain.mkdir()
        project = make_project(tmp_path / "second", plain)
        record(project, plain, kind="none")
        refused(project, "no active repository worktree", close, plain, "keep")

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
            payload = request(project, identity, reflection="Done.", continuation={"next": "none"})
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
        plain = tmp_path / "plain"
        plain.mkdir()
        exempt = make_project(tmp_path / "second", plain)
        record(exempt, plain, kind="none")
        assert worktree_closure_findings(load_state(exempt)) == ([], [])
