"""The gate profile: the `gates` field and its writers, discovery, approval, runs, scans and the finish gate.

Every claim the design (artifacts/architecture.md, sections 3, 4 and 6) makes about existing code is pinned here
by a test that was written before the code it builds on. The repositories are real scratch Git repositories.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_gates as gates
import workspace_lib as lib
import workspace_workflows as workflows
from workspace_journal import READ_INPUTS
from workspace_lib import WorkspaceError, allocate_project, validate_v3_state, validate_v4_state
from workspace_session import _load_state, update_project_data
from workspace_workflows import workflow

from tests.conftest import SKILL_SCRIPTS
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_workflow_automation import fill_spec

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
USER = {"source": "user reply 2026-11-23", "response": "Yes, run those"}
CODE_OUTPUTS = [{"root": "target", "path": "app.py", "required": True}]
APPROVAL = {"source": "user reply 2026-11-23", "response": "Yes", "approved_at": "2026-11-23T10:00:00+00:00"}
BASE = "a" * 40
LINT = {"id": "lint", "kind": "lint", "argv": ["ruff", "check", "."]}
TYPES = {"id": "types", "kind": "types", "argv": ["ty", "check", "."]}
TESTS = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-m", "pytest", "-q"]}


def git(cwd: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True, env=GIT_ENV)
    return done.stdout.strip()


def make_repo(root: Path, files: "dict[str, str] | None" = None) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for name, text in (
        files
        or {"README.md": "hello\n", "src/app.py": "def f():\n    return 1\n", "app.py": "x = 1\n", "NOTES.md": "n\n"}
    ).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def raw_state(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def write_state(project: Path, state: dict[str, Any]) -> None:
    (project / "project.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def gates_object(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"enabled": True, "commands": [], "approval": None, "waiver": None}
    base.update(changes)
    return base


def worktree_entry(repo: Path, **changes: Any) -> dict[str, Any]:
    entry = {
        "repository": str(repo),
        "path": str(repo),
        "branch": "main",
        "base_commit": "abc123",
        "kind": "existing",
        "role": "target",
        "status": "active",
        "recorded_at": "2026-01-01T10:00:00+00:00",
        "confirmation": {"source": "user reply", "response": "yes"},
        "closure": None,
    }
    entry.update(changes)
    return entry


def task_plan(task_id: str, outputs: "list[dict[str, Any]] | None" = None) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": f"Do {task_id}",
        "success_criteria": "ok",
        "verification": "run",
        "depends_on": [],
        "effect": {"kind": "local_write", "description": "Edit"},
        "outputs": CODE_OUTPUTS if outputs is None else outputs,
    }


def make_project(tmp_path: Path, repo: "Path | None" = None, *, gates_field: "dict[str, Any] | None" = None) -> Path:
    """A PLANNING project whose target is `repo`, with a code task T01 and a prose task T02."""
    repo = repo or make_repo(tmp_path)
    project = allocate_project(tmp_path / "ws", title="Gated", working_directory=repo, create_root=True)
    fill_spec(project)
    update_project_data(
        project,
        {
            "status": "PLANNING",
            "tasks": [task_plan("T01"), task_plan("T02", [{"root": "target", "path": "NOTES.md", "required": True}])],
        },
        expected_revision=0,
    )
    state = raw_state(project)
    state["worktrees"] = [worktree_entry(repo)]
    if gates_field is not None:
        state["gates"] = gates_field
    write_state(project, state)
    return project


def gates_request(project: Path, identity: str, operation: str, **fields: Any) -> dict[str, Any]:
    return request(project, identity, operation=operation, **fields)


# --- the schema and who may write the field ---------------------------------------------------------------


class TestGatesField:
    def test_a_valid_gates_object_is_accepted_and_the_field_is_optional(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, gates_field=gates_object())
        state = raw_state(project)
        assert validate_v4_state(state, project, check_files=False).errors == []
        del state["gates"]
        assert validate_v4_state(state, project, check_files=False).errors == []
        state["gates"] = gates_object(
            base_commit=BASE,
            sources=[{"path": "AGENTS.md", "sha256": "b" * 64}],
            commands=[LINT, TYPES, TESTS],
            approval=APPROVAL,
        )
        assert validate_v4_state(state, project, check_files=False).errors == []

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("yes", "gates must be an object"),
            ({"enabled": True, "commands": [], "approval": None}, "gates: missing"),
            (gates_object(enabled="yes"), "gates.enabled must be a boolean"),
            (gates_object(extra=1), "gates: unexpected"),
            (gates_object(commands="lint"), "gates.commands must be a list"),
            (gates_object(commands=[{"id": "lint", "kind": "style", "argv": ["x"]}]), "kind must be one of"),
            (gates_object(commands=[{"id": "Bad Id", "kind": "lint", "argv": ["x"]}]), "id must be"),
            (gates_object(commands=[LINT, LINT]), "duplicate command id"),
            (gates_object(commands=[{"id": "lint", "kind": "lint", "argv": []}]), "argv must be a non-empty list"),
            (gates_object(commands=[{"id": "lint", "kind": "lint", "argv": [""]}]), "argv must be a non-empty list"),
            (gates_object(commands=["lint"]), "must be an object"),
            (gates_object(approval="user"), "gates.approval must be null or an object"),
            (gates_object(approval={"source": "s"}), "gates.approval: missing"),
            (gates_object(approval=APPROVAL), "base_commit"),
            (gates_object(approval=APPROVAL, base_commit="xyz", sources=[]), "base_commit must be 40"),
            (gates_object(approval=APPROVAL, base_commit=BASE), "sources"),
            (gates_object(approval=APPROVAL, base_commit=BASE, sources="x"), "sources must be a list"),
            (gates_object(approval=APPROVAL, base_commit=BASE, sources=[{"path": "a"}]), "sources[0]: missing"),
            (gates_object(approval=APPROVAL, base_commit=BASE, sources=["x"]), "sources[0] must be an object"),
            (
                gates_object(approval=APPROVAL, base_commit=BASE, sources=[{"path": "a", "sha256": "zz"}]),
                "sources[0]: sha256 must be 64",
            ),
            (gates_object(waiver="no"), "gates.waiver must be null or an object"),
            (gates_object(waiver={"reason": "r"}), "gates.waiver: missing"),
            (
                gates_object(waiver={"reason": "r", "source": "s", "response": "x", "granted_at": "t", "kinds": []}),
                "kinds must be a non-empty list",
            ),
            (
                gates_object(waiver={"reason": "r", "source": "s", "response": "x", "granted_at": "t", "kinds": ["x"]}),
                "kinds must hold",
            ),
        ],
    )
    def test_a_malformed_gates_object_is_a_finding_never_an_exception(
        self, tmp_path: Path, value: Any, message: str
    ) -> None:
        project = make_project(tmp_path)
        state = raw_state(project)
        state["gates"] = value
        errors = validate_v4_state(state, project, check_files=False).errors
        assert any(message in error for error in errors), errors

    def test_a_schema_v3_project_cannot_hold_the_field(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        state = raw_state(project)
        state["schema_version"] = 3
        del state["execution"]
        state["gates"] = gates_object()
        errors = validate_v3_state(state, project, check_files=False).errors
        assert any("gates" in error for error in errors), errors

    def test_update_cannot_write_gates(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        with pytest.raises(WorkspaceError, match="unsupported update fields: gates"):
            update_project_data(project, {"gates": gates_object()}, expected_revision=_load_state(project)["revision"])
        assert "gates" not in raw_state(project)

    def test_a_candidate_that_changes_gates_is_refused_unless_the_gates_action_wrote_it(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, gates_field=gates_object())
        current = raw_state(project)
        candidate = copy.deepcopy(current)
        candidate["gates"]["enabled"] = False
        for action in (None, "update", "reconcile", "settings", "confirm"):
            report = lib.check_state_candidate(project, candidate, action=action, check_files=False)
            assert "candidate cannot change guarded field: gates" in report.errors, action
        report = lib.check_state_candidate(project, candidate, action="gates", check_files=False)
        assert "candidate cannot change guarded field: gates" not in report.errors

    def test_reconcile_cannot_smuggle_gates_through_its_patch(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, gates_field=gates_object())
        # Refused one layer earlier than the guard: the patch of `reconcile` names a closed set of keys.
        with pytest.raises(WorkspaceError, match=r"unknown field\(s\) \['gates'\]"):
            workflow(
                project,
                "reconcile",
                request(
                    project,
                    "rec-g",
                    patch={"gates": gates_object(enabled=False)},
                    decision="x",
                    continuation={"next": "Go on."},
                ),
            )
        assert raw_state(project)["gates"]["enabled"] is True


class TestRegistration:
    def test_the_actions_are_registered_with_their_inputs(self) -> None:
        assert "gates" in workflows.FIELDS
        assert workflows.FIELDS["gates"] == {"operation", "commands", "confirmation", "reason", "kinds"}
        assert READ_INPUTS["gate-discover"] == (frozenset(), frozenset())
        assert READ_INPUTS["gate-run"] == (frozenset({"task"}), frozenset({"task"}))
        assert {"gate-discover", "gate-run"} <= workflows.READ_ACTIONS

    def test_the_schema_prints_the_keys_each_operation_needs(self) -> None:
        schema = workflows.input_schema("gates")
        assert {"confirmation", "operation"} <= set(schema["required"])
        assert schema["required_by"]["operation=approve"] == sorted(
            {"id", "expected_revision", "confirmation", "operation", "commands"}
        )
        assert schema["required_by"]["operation=waive"] == sorted(
            {"id", "expected_revision", "confirmation", "operation", "kinds", "reason"}
        )
        assert schema["nested"] == {
            "confirmation": {"allowed": ["response", "source"], "required": ["response", "source"]}
        }

    def test_a_missing_confirmation_or_command_list_is_refused_by_name(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, gates_field=gates_object())
        with pytest.raises(WorkspaceError, match=r"missing required field\(s\) \['commands'\]"):
            workflow(project, "gates", gates_request(project, "g1", "approve", confirmation=USER))
        with pytest.raises(WorkspaceError, match=r"confirmation: expected a JSON object"):
            workflow(project, "gates", gates_request(project, "g2", "waive", kinds=["types"], reason="no checker"))

    def test_gates_is_an_internal_update_field_only(self) -> None:
        import workspace_session

        assert "gates" in workspace_session.INTERNAL_UPDATE_FIELDS
        assert "gates" not in workspace_session.UPDATE_FIELDS
        assert lib.GUARDED_WRITERS["gates"] == {"gates"}
        assert "gates" in lib.GUARDED_PROJECT_FIELDS
        assert "gates" in lib.OPTIONAL_PROJECT_FIELDS and "gates" in lib.PROJECT_V4_FIELDS


def test_the_scripts_directory_is_the_working_tree_copy() -> None:
    assert Path(gates.__file__).resolve().parent == SKILL_SCRIPTS.resolve()


# --- the Git recipes: head_tree on a real scratch repository -----------------------------------------------


def tree(repo: Path, base: "str | None" = None) -> str:
    return gates.head_tree(repo, base or git(repo, "rev-parse", "HEAD"))


class TestHeadTree:
    def test_it_is_stable_and_follows_tracked_edits(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        clean = tree(repo, base)
        assert len(clean) == 64
        assert tree(repo, base) == clean
        (repo / "src/app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        edited = tree(repo, base)
        assert edited != clean
        (repo / "src/app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        assert tree(repo, base) == clean

    def test_untracked_files_and_their_content_are_part_of_it(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        clean = tree(repo, base)
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        first = tree(repo, base)
        assert first != clean
        (repo / "new.py").write_text("x = 2\n", encoding="utf-8")
        assert tree(repo, base) not in (clean, first)

    def test_ignored_files_are_not(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {".gitignore": "*.log\n", "a.py": "a = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        clean = tree(repo, base)
        (repo / "noise.log").write_text("x\n", encoding="utf-8")
        assert tree(repo, base) == clean

    def test_a_commit_after_the_base_keeps_the_tree_of_the_same_content(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "src/app.py").write_text("def f():\n    return 3\n", encoding="utf-8")
        before = tree(repo, base)
        git(repo, "commit", "-q", "-am", "work")
        assert tree(repo, base) == before

    def test_it_writes_nothing_to_the_index_the_tree_or_the_object_store(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        # A tracked file with new stat data (same content) is what tempts a plain `git diff` to refresh the index.
        os.utime(repo / "README.md", (1_700_000_000, 1_700_000_000))
        index = repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        objects = sorted(path.name for path in (repo / ".git" / "objects").rglob("*") if path.is_file())
        tree(repo, base)
        assert (index.read_bytes(), index.stat().st_mtime_ns) == before
        assert sorted(path.name for path in (repo / ".git" / "objects").rglob("*") if path.is_file()) == objects
        # A control: a bare `git diff` does refresh this index, so the assertion above can fail.
        subprocess.run(["git", "-C", str(repo), "diff", "--quiet"], check=False, env=GIT_ENV)
        assert (index.read_bytes(), index.stat().st_mtime_ns) != before

    def test_a_same_size_edit_inside_the_index_second_is_still_seen(self, tmp_path: Path) -> None:
        """Git trusts a file whose size and mtime second match its index entry unless the entry is racily clean.

        The snapshot is a fresh `read-tree` of the base with no stat data at all, so no entry can be racily clean and
        a quick edit of the same size is hashed like any other.
        """
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        clean = tree(repo, base)
        target = repo / "app.py"
        before = target.stat()
        target.write_text("x = 2\n", encoding="utf-8")  # same size as "x = 1\n"
        assert target.stat().st_size == before.st_size
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Make the entry racy, as an edit within the second of the last `git add` makes it.
        index = repo / ".git" / "index"
        os.utime(index, ns=(before.st_mtime_ns, before.st_mtime_ns))
        assert tree(repo, base) != clean
        assert [item.path for item in gates.changed_files(repo, base)] == ["app.py"]

    def test_a_repository_without_an_index_file_is_still_read(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("x = 9\n", encoding="utf-8")
        (repo / ".git" / "index").unlink()
        assert [item.path for item in gates.changed_files(repo, base)] == ["app.py"]
        assert len(tree(repo, base)) == 64

    def test_an_unborn_repository_is_refused_with_its_remedy(self, tmp_path: Path) -> None:
        repo = tmp_path / "unborn"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        with pytest.raises(WorkspaceError, match=r"gates-no-commit: .*initial commit"):
            gates.current_head(repo)

    def test_a_directory_that_is_not_a_repository_is_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorkspaceError, match="gates-no-repository"):
            gates.current_head(plain)

    def test_current_head_is_the_commit_id(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        assert gates.current_head(repo) == git(repo, "rev-parse", "HEAD")


# --- discovery, approval and waiver -------------------------------------------------------------------------

AGENTS = """# Agents

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .   # lint
uv run ty check .
uv lock --check
uv run ruff check --fix .
uv run pytest -q --no-cov tests/x.py
cat a | wc -l
```

Run `uv run ruff format --check .` too, and `uv run ruff format .` rewrites files.
"""
CI = """name: ci
jobs:
  test:
    steps:
      - run: mypy src
      - name: many
        run: |
          pip install tox
          flake8 .
"""


def set_worktrees(project: Path, worktrees: "list[dict[str, Any]]", working_directory: "Path | None" = None) -> None:
    state = raw_state(project)
    state["worktrees"] = worktrees
    if working_directory is not None:
        state["working_directory"] = str(working_directory)
    write_state(project, state)


def tree_files(root: Path) -> "dict[str, bytes]":
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture
def documented(tmp_path: Path) -> "tuple[Path, Path]":
    repo = make_repo(
        tmp_path, {"AGENTS.md": AGENTS, ".github/workflows/ci.yml": CI, "src/app.py": "def f():\n    return 1\n"}
    )
    return make_project(tmp_path, repo), repo


class TestDiscover:
    def test_candidates_come_from_the_documented_commands_with_their_sources(
        self, documented: "tuple[Path, Path]"
    ) -> None:
        project, repo = documented
        result = gates.gates_discover(project)
        found = {(item["kind"], tuple(item["argv"]), item["source"]) for item in result["candidates"]}
        assert found == {
            ("tests", ("uv", "run", "pytest", "-q"), "AGENTS.md"),
            ("lint", ("uv", "run", "ruff", "check", "."), "AGENTS.md"),
            ("types", ("uv", "run", "ty", "check", "."), "AGENTS.md"),
            ("format", ("uv", "run", "ruff", "format", "--check", "."), "AGENTS.md"),
            ("types", ("mypy", "src"), ".github/workflows/ci.yml"),
            ("lint", ("flake8", "."), ".github/workflows/ci.yml"),
        }
        ids = [item["id"] for item in result["candidates"]]
        assert len(ids) == len(set(ids)) and {"lint", "types", "tests", "format"} <= set(ids)
        assert result["repository"] == str(repo) and result["head"] == git(repo, "rev-parse", "HEAD")
        assert {item["path"] for item in result["sources"]} == {"AGENTS.md", ".github/workflows/ci.yml"}
        assert all(len(item["sha256"]) == 64 for item in result["sources"])
        assert result["missing_kinds"] == []

    def test_it_lists_the_line_a_candidate_came_from(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        by_argv = {tuple(item["argv"]): item for item in gates.gates_discover(project)["candidates"]}
        assert by_argv[("uv", "run", "pytest", "-q")]["line"] == 5

    def test_it_writes_nothing(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        before = (tree_files(project), tree_files(repo), git(repo, "status", "--porcelain=v1"))
        gates.gates_discover(project)
        assert (tree_files(project), tree_files(repo), git(repo, "status", "--porcelain=v1")) == before

    def test_the_workflow_action_returns_the_same_result(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        assert workflow(project, "gate-discover", {}) == gates.gates_discover(project)

    def test_a_pyproject_table_is_a_fallback_candidate_without_a_source_hash(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"pyproject.toml": "[tool.ruff]\n\n[tool.pytest.ini_options]\n\n[tool.ty]\n"})
        result = gates.gates_discover(make_project(tmp_path, repo))
        assert {(item["kind"], tuple(item["argv"])) for item in result["candidates"]} == {
            ("lint", ("ruff", "check", ".")),
            ("tests", ("pytest", "-q")),
            ("types", ("ty", "check", ".")),
        }
        assert result["sources"] == []

    def test_a_bare_repository_has_no_candidates_and_misses_every_required_kind(self, tmp_path: Path) -> None:
        result = gates.gates_discover(make_project(tmp_path))
        assert result["candidates"] == [] and result["sources"] == []
        assert result["missing_kinds"] == ["lint", "types", "tests"]

    def test_a_project_without_a_repository_reports_none(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        set_worktrees(project, [])
        result = gates.gates_discover(project)
        assert result["repository"] is None and result["head"] is None and result["candidates"] == []


def approve(project: Path, commands: "list[dict[str, Any]]", identity: str = "ga", **extra: Any) -> dict[str, Any]:
    return workflow(
        project, "gates", gates_request(project, identity, "approve", commands=commands, confirmation=USER, **extra)
    )


def waive(project: Path, kinds: "list[str]", identity: str = "gw", reason: str = "no checker") -> dict[str, Any]:
    return workflow(
        project, "gates", gates_request(project, identity, "waive", kinds=kinds, reason=reason, confirmation=USER)
    )


class TestApprove:
    def test_it_records_the_commands_the_base_the_sources_and_the_users_words(
        self, documented: "tuple[Path, Path]"
    ) -> None:
        project, repo = documented
        revision = raw_state(project)["revision"]
        result = approve(project, [LINT, TYPES, TESTS])
        stored = raw_state(project)
        assert stored["revision"] == revision + 1
        assert stored["gates"]["commands"] == [LINT, TYPES, TESTS]
        assert stored["gates"]["base_commit"] == git(repo, "rev-parse", "HEAD")
        assert {item["path"] for item in stored["gates"]["sources"]} == {"AGENTS.md", ".github/workflows/ci.yml"}
        assert stored["gates"]["approval"]["source"] == USER["source"]
        assert stored["gates"]["approval"]["response"] == USER["response"]
        assert stored["gates"]["enabled"] is True and stored["gates"]["waiver"] is None
        assert result["missing_kinds"] == [] and result["commands"] == ["lint", "types", "tests"]
        spec = (project / "spec.md").read_text(encoding="utf-8")
        assert "kind: gates" in spec and USER["response"] in spec
        assert validate_v4_state(stored, project, check_files=False).errors == []

    def test_a_second_approval_keeps_the_base_and_merges_commands_by_id(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        approve(project, [LINT, TESTS])
        base = raw_state(project)["gates"]["base_commit"]
        (repo / "more.py").write_text("x = 1\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "second")
        newer = {**LINT, "argv": ["ruff", "check", "src"]}
        result = approve(project, [newer, TYPES], "gb")
        gates_now = raw_state(project)["gates"]
        assert gates_now["base_commit"] == base != git(repo, "rev-parse", "HEAD")
        assert gates_now["commands"] == [newer, TESTS, TYPES]
        assert result["missing_kinds"] == []

    def test_a_lint_only_set_reports_what_is_still_missing(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        assert approve(project, [LINT])["missing_kinds"] == ["types", "tests"]

    def test_an_absent_gates_field_is_created_enabled(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        assert "gates" not in raw_state(project)
        approve(project, [LINT])
        assert raw_state(project)["gates"]["enabled"] is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["pytest", "--no-cov"],
            ["pytest", "-p", "no:cov"],
            ["pytest", "-pno:cov"],
            ["pytest", "--cov-branch"],
            ["pytest", "--cov-report=json:x.json"],
            ["python", "-m", "pytest", "--no-cov", "-q"],
        ],
    )
    def test_a_tests_command_that_switches_coverage_off_or_owns_its_report_is_refused(
        self, documented: "tuple[Path, Path]", argv: "list[str]"
    ) -> None:
        project, _ = documented
        with pytest.raises(WorkspaceError, match="approval-invalid"):
            approve(project, [{"id": "tests", "kind": "tests", "argv": argv}])
        assert "gates" not in raw_state(project)

    def test_a_cov_directory_is_kept(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        approve(project, [{"id": "tests", "kind": "tests", "argv": ["pytest", "--cov=src", "-q"]}])
        assert raw_state(project)["gates"]["commands"][0]["argv"] == ["pytest", "--cov=src", "-q"]

    def test_a_tests_command_must_be_a_pytest_invocation(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        with pytest.raises(WorkspaceError, match=r"approval-invalid.*pytest"):
            approve(project, [{"id": "tests", "kind": "tests", "argv": ["make", "test"]}])
        approve(project, [{"id": "tests", "kind": "tests", "argv": ["uv", "run", "pytest"]}], "ok1")
        approve(project, [{"id": "t2", "kind": "tests", "argv": ["python", "-m", "pytest"]}], "ok2")
        approve(project, [{"id": "t3", "kind": "tests", "argv": ["/venv/bin/pytest", "-q"]}], "ok3")

    @pytest.mark.parametrize(
        ("commands", "message"),
        [
            ([], "commands must be a non-empty list|approval-invalid"),
            ([{"id": "lint", "kind": "style", "argv": ["x"]}], "kind must be one of"),
            ([LINT, LINT], "duplicate command id"),
            ([{"id": "lint", "kind": "lint", "argv": []}], "argv must be a non-empty list"),
            (["lint"], "expected a JSON object"),
            ([{"id": "lint", "kind": "lint"}], "missing required field"),
            ([{"id": "lint", "kind": "lint", "argv": ["a"], "extra": 1}], "unknown field"),
        ],
    )
    def test_a_malformed_command_list_is_refused_and_nothing_is_written(
        self, documented: "tuple[Path, Path]", commands: Any, message: str
    ) -> None:
        project, _ = documented
        before = tree_files(project)
        with pytest.raises(WorkspaceError, match=message):
            approve(project, commands)
        assert {name: data for name, data in tree_files(project).items() if not name.startswith(".operations")} == {
            name: data for name, data in before.items() if not name.startswith(".operations")
        }

    def test_a_project_with_no_target_repository_cannot_be_gated(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        set_worktrees(project, [])
        with pytest.raises(WorkspaceError, match="gates-no-repository"):
            approve(project, [LINT])
        set_worktrees(project, [worktree_entry(repo, kind="none", repository=None, branch=None, base_commit=None)])
        with pytest.raises(WorkspaceError, match="gates-no-repository"):
            approve(project, [LINT], "gx")

    def test_an_additional_worktree_is_not_the_target(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        set_worktrees(project, [worktree_entry(repo, role="additional")])
        with pytest.raises(WorkspaceError, match="gates-no-repository"):
            approve(project, [LINT])

    def test_an_unborn_repository_is_refused(self, tmp_path: Path) -> None:
        repo = tmp_path / "unborn"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        project = make_project(tmp_path, repo)
        with pytest.raises(WorkspaceError, match="gates-no-commit"):
            approve(project, [LINT])

    def test_the_command_runs_where_the_target_worktree_is_not_where_working_directory_points(
        self, documented: "tuple[Path, Path]"
    ) -> None:
        project, repo = documented
        state = raw_state(project)
        assert state["working_directory"] == str(repo)
        assert gates.target_repository(state) == repo

    def test_it_needs_an_aligned_project(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        project = allocate_project(tmp_path / "ws", title="Early", working_directory=repo, create_root=True)
        state = raw_state(project)
        state["worktrees"] = [worktree_entry(repo)]
        write_state(project, state)
        with pytest.raises(WorkspaceError, match="ALIGNING"):
            approve(project, [LINT])

    def test_a_closed_project_keeps_its_gates(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        state = raw_state(project)
        state["status"] = "CANCELLED"
        state["cancellation_reason"] = "stopped"
        write_state(project, state)
        with pytest.raises(WorkspaceError, match="CANCELLED"):
            approve(project, [LINT])


class TestWaive:
    def test_a_kind_waiver_records_the_users_words_and_the_base(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        result = waive(project, ["types"])
        stored = raw_state(project)["gates"]
        assert stored["waiver"]["kinds"] == ["types"] and stored["waiver"]["reason"] == "no checker"
        assert stored["waiver"]["source"] == USER["source"] and stored["waiver"]["response"] == USER["response"]
        assert stored["base_commit"] == git(repo, "rev-parse", "HEAD") and stored["approval"] is None
        assert result["waived"] == ["types"]
        assert validate_v4_state(raw_state(project), project, check_files=False).errors == []

    def test_waivers_accumulate_their_kinds_without_duplicates(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        waive(project, ["types"])
        result = waive(project, ["lint", "types"], "gw2", "no linter either")
        assert raw_state(project)["gates"]["waiver"]["kinds"] == ["types", "lint"]
        assert result["waived"] == ["types", "lint"]

    def test_a_waiver_does_not_move_the_base_or_touch_the_approval(self, documented: "tuple[Path, Path]") -> None:
        project, repo = documented
        approve(project, [LINT])
        before = copy.deepcopy(raw_state(project)["gates"])
        (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "more")
        waive(project, ["types"])
        after = raw_state(project)["gates"]
        assert {key: after[key] for key in ("base_commit", "approval", "commands", "sources")} == {
            key: before[key] for key in ("base_commit", "approval", "commands", "sources")
        }

    def test_the_no_repository_scope_is_only_for_a_target_that_is_not_a_repository(
        self, documented: "tuple[Path, Path]"
    ) -> None:
        project, repo = documented
        with pytest.raises(WorkspaceError, match=r"no-repository.*is a repository"):
            waive(project, ["no-repository"])
        set_worktrees(project, [worktree_entry(repo, kind="none", repository=None, branch=None, base_commit=None)])
        result = waive(project, ["no-repository"], "gn")
        stored = raw_state(project)["gates"]
        assert stored["waiver"]["kinds"] == ["no-repository"] and "base_commit" not in stored
        assert result["waived"] == ["no-repository"]
        assert validate_v4_state(raw_state(project), project, check_files=False).errors == []

    def test_a_kind_waiver_needs_a_repository(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        set_worktrees(project, [])
        with pytest.raises(WorkspaceError, match="gates-no-repository"):
            waive(project, ["types"])

    @pytest.mark.parametrize(
        ("kinds", "reason", "message"),
        [
            ([], "r", "kinds must be a non-empty list"),
            (["speed"], "r", "kinds must hold only"),
            ("types", "r", "kinds must be a non-empty list"),
            (["types"], " ", "reason must be non-empty text"),
        ],
    )
    def test_malformed_waivers_are_refused(
        self, documented: "tuple[Path, Path]", kinds: Any, reason: str, message: str
    ) -> None:
        project, _ = documented
        with pytest.raises(WorkspaceError, match=message):
            waive(project, kinds, reason=reason)
        assert "gates" not in raw_state(project)


# --- the diff: hunks, file classes and the scan of added lines -----------------------------------------------

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -2,0 +3,3 @@ def f():
+    x = 1  # noqa
+    y = 2
++++ not a header
@@ -10,2 +12,0 @@
-gone
-gone2
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a
+b
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-1
-2
-3
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
diff --git a/a.py b/b.py
similarity index 90%
rename from a.py
rename to b.py
diff --git a/mode.sh b/mode.sh
old mode 100644
new mode 100755
"""


class TestParseDiff:
    def test_it_reads_added_lines_by_new_line_number_and_counts_removals(self) -> None:
        changes = {item.path: item for item in gates.parse_diff(DIFF)}
        app = changes["src/app.py"]
        assert app.status == "modified" and app.added == {3: "    x = 1  # noqa", 4: "    y = 2", 5: "+++ not a header"}
        assert app.removed == 2 and app.deletions == [12]
        assert changes["new.py"].status == "added" and changes["new.py"].added == {1: "a", 2: "b"}

    def test_deleted_binary_renamed_and_mode_only_files(self) -> None:
        changes = {item.path: item for item in gates.parse_diff(DIFF)}
        assert (
            changes["old.py"].status == "deleted" and changes["old.py"].added == {} and changes["old.py"].removed == 3
        )
        assert changes["img.png"].binary is True
        assert (
            changes["b.py"].status == "renamed" and changes["b.py"].old_path == "a.py" and changes["b.py"].added == {}
        )
        assert changes["mode.sh"].status == "modified" and changes["mode.sh"].added == {}

    def test_touched_lines_are_the_added_lines_plus_the_line_before_a_pure_deletion(self) -> None:
        app = next(item for item in gates.parse_diff(DIFF) if item.path == "src/app.py")
        assert app.touched == {3, 4, 5, 12}
        top = gates.FileChange(path="t.py", deletions=[0])
        assert top.touched == set()

    def test_an_empty_diff_has_no_changes(self) -> None:
        assert gates.parse_diff("") == []


class TestClassify:
    @pytest.mark.parametrize(
        ("path", "kind"),
        [
            ("src/app.py", "source"),
            ("tests/helpers.py", "test"),
            ("a/test/x.py", "test"),
            ("test_x.py", "test"),
            ("x_test.py", "test"),
            ("pkg/conftest.py", "source"),
            ("tests/conftest.py", "test"),
            ("pyproject.toml", "config"),
            ("setup.cfg", "config"),
            ("ruff.toml", "config"),
            (".coveragerc", "config"),
            ("tox.ini", "config"),
            ("ci.yml", "config"),
            ("ci.yaml", "config"),
            ("x.cfg", "config"),
            ("README.md", "doc"),
            ("a.rst", "doc"),
            ("n.txt", "doc"),
            ("run.sh", "other"),
            ("src/main.go", "other"),
        ],
    )
    def test_file_classes_follow_the_design_table(self, path: str, kind: str) -> None:
        assert gates.classify_file(path) == kind

    def test_language_is_named_from_the_suffix(self) -> None:
        assert gates.language_of("a.py") == "python" and gates.language_of("a.md") == "markdown"
        assert gates.language_of("a.toml") == "toml" and gates.language_of("Makefile") == "other"


def commit_all(repo: Path, message: str = "more") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def scan(repo: Path, base: str) -> "tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    return gates.scan_changes(repo, gates.changed_files(repo, base))


class TestChangedFiles:
    def test_untracked_files_count_as_wholly_added_and_binary_ones_are_marked(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "new.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        (repo / "src/app.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
        changes = {item.path: item for item in gates.changed_files(repo, base)}
        assert changes["new.py"].status == "added" and sorted(changes["new.py"].added) == [1, 2]
        # The diff is read as text (`--text`), so what makes a file binary is a NUL byte in an added line.
        assert changes["blob.bin"].binary is True
        assert sorted(changes["src/app.py"].added) == [3, 4, 5, 6]

    def test_a_commit_after_the_base_is_still_part_of_the_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "src/app.py").write_text("def f():\n    return 1\n# one\n", encoding="utf-8")
        commit_all(repo)
        (repo / "src/app.py").write_text("def f():\n    return 1\n# one\n# two\n", encoding="utf-8")
        assert sorted(gates.changed_files(repo, base)[0].added) == [3, 4]

    def test_it_does_not_touch_the_index(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        os.utime(repo / "README.md", (1_700_000_000, 1_700_000_000))
        index = repo / ".git" / "index"
        before = index.read_bytes()
        gates.changed_files(repo, base)
        assert index.read_bytes() == before


BYPASSES = [
    ("x = 1  # type: ignore", "type: ignore"),
    ("x = 1  # type:ignore[attr]", "type: ignore"),
    ("x = 1  # noqa: E501", "noqa"),
    ("x = 1  #noqa", "noqa"),
    ("x = 1  # pyright: ignore[reportX]", "pyright: ignore"),
    ("x = 1  # ty: ignore[x]", "ty: ignore"),
    ("y = typing.cast(int, x)", "cast"),
    ("from typing import cast; y = cast(int, x)", "cast"),
    ("@pytest.mark.skip", "pytest.mark.skip"),
    ('@pytest.mark.skip(reason="later")', "pytest.mark.skip"),
    ("pytest.skip()", "pytest.skip"),
    ("@pytest.mark.xfail", "xfail"),
    ("pytest.xfail('x')", "xfail"),
    ("@unittest.skip('no')", "unittest.skip"),
    ("addopts = '--no-cov'", "--no-cov"),
    ("def f():  # pragma: no cover", "pragma: no cover"),
]


class TestDiffScan:
    @pytest.mark.parametrize(("line", "name"), BYPASSES)
    def test_each_bypass_pattern_on_an_added_line_is_a_finding_with_its_line(
        self, tmp_path: Path, line: str, name: str
    ) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        suffix = "py"
        (repo / f"feature.{suffix}").write_text(f"a = 1\n{line}\nb = 2\n", encoding="utf-8")
        findings, info = scan(repo, base)
        assert [(item["code"], item["file"], item["line"], item["detail"]) for item in findings] == [
            ("bypass-added", f"feature.{suffix}", 2, name)
        ], findings
        assert info == []

    def test_an_ignore_already_in_the_base_is_not_a_finding_but_a_new_one_is(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"src/app.py": "x = 1  # noqa\ny = cast(int, 1)\n"})
        base = git(repo, "rev-parse", "HEAD")
        assert scan(repo, base) == ([], [])
        (repo / "src/app.py").write_text("z = 0\nx = 1  # noqa\ny = cast(int, 1)\nw = 2  # noqa\n", encoding="utf-8")
        findings, _ = scan(repo, base)
        assert [(item["line"], item["detail"]) for item in findings] == [(4, "noqa")]

    def test_the_control_fails_when_the_ignore_is_new(self, tmp_path: Path) -> None:
        """Same edit as above on a base without the ignore: the finding appears, so the control above can fail."""
        repo = make_repo(tmp_path, {"src/app.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "src/app.py").write_text("x = 1  # noqa\n", encoding="utf-8")
        assert [item["detail"] for item in scan(repo, base)[0]] == ["noqa"]

    def test_a_removed_ignore_and_a_moved_file_are_not_findings(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"old.py": "x = 1  # noqa\n" + "pad = 0\n" * 20, "keep.py": "k = 1  # noqa\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "keep.py").write_text("k = 1\n", encoding="utf-8")
        git(repo, "mv", "old.py", "new.py")
        assert scan(repo, base) == ([], [])

    def test_broadcast_is_not_a_cast_and_prose_files_are_not_scanned(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text("y = broadcast(x)\nz = forecast(1)\n", encoding="utf-8")
        (repo / "NOTES.md").write_text("Do not write # noqa or --no-cov or cast( here\n", encoding="utf-8")
        assert scan(repo, base) == ([], [])

    def test_no_cov_in_a_workflow_or_script_is_a_finding_and_other_patterns_there_are_not(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "ci.yml").write_text("run: pytest --no-cov  # noqa\n", encoding="utf-8")
        findings, _ = scan(repo, base)
        assert [(item["file"], item["detail"]) for item in findings] == [("ci.yml", "--no-cov")]

    def test_conditional_skips_with_a_reason_are_info_not_findings(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "test_a.py").write_text(
            "import sys\nimport pytest\n\n"
            '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\ndef test_a():\n'
            "    if sys.version_info < (3, 12):\n"
            '        pytest.skip("needs 3.12")\n'
            "    if sys.argv:\n"
            '        pytest.skip(reason="argv")\n'
            "    assert True\n",
            encoding="utf-8",
        )
        findings, info = scan(repo, base)
        assert findings == []
        assert [(item["code"], item["line"]) for item in info] == [
            ("conditional-skip", 4),
            ("conditional-skip", 7),
            ("conditional-skip", 9),
        ]

    def test_a_conditional_skip_without_a_reason_and_an_unguarded_skip_are_findings(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "test_b.py").write_text(
            "import sys\nimport pytest\n\n"
            '@pytest.mark.skipif(sys.platform == "win32")\ndef test_a():\n'
            "    pytest.skip('whenever')\n\n"
            "@pytest.mark.skipif(\n    sys.platform == 'win32',\n    reason='multi line',\n)\ndef test_b():\n"
            "    if sys.argv:\n        pytest.skip()\n"
            "    @unittest.skipIf(True, 'why')\n"
            "    @unittest.skipUnless(True)\n"
            "    pass\n",
            encoding="utf-8",
        )
        findings, info = scan(repo, base)
        assert [(item["line"], item["detail"]) for item in findings] == [
            (4, "pytest.mark.skipif"),
            (6, "pytest.skip"),
            (14, "pytest.skip"),
            (15, "unittest.skipif"),  # a constant condition (`True`) skips always, whatever its reason says
            (16, "unittest.skipif"),
        ]
        assert [(item["code"], item["line"]) for item in info] == [("conditional-skip", 8)]

    def test_a_conftest_change_and_a_deleted_test_file_are_reported_as_information(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"tests/conftest.py": "x = 1\n", "tests/test_old.py": "def test_a():\n    pass\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "tests/conftest.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "tests/test_old.py").unlink()
        findings, info = scan(repo, base)
        assert findings == []
        assert sorted((item["code"], item["file"]) for item in info) == [
            ("conftest-changed", "tests/conftest.py"),
            ("test-deleted", "tests/test_old.py"),
        ]

    def test_a_clean_change_has_nothing_to_report(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "src/app.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
        assert scan(repo, base) == ([], [])


# --- the config thresholds ----------------------------------------------------------------------------------

PYPROJECT = """[project]
name = "x"
version = "1.0"

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I"]
ignore = ["E501"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101"]

[tool.pytest.ini_options]
addopts = "--strict-markers --cov=src"
testpaths = ["tests", "more"]

[tool.coverage.run]
omit = ["src/gen.py"]

[tool.coverage.report]
fail_under = 100
exclude = ["pragma"]
"""


def config_findings(tmp_path: Path, before: str, after: "str | None", name: str = "pyproject.toml") -> "list[str]":
    repo = make_repo(tmp_path, {name: before, "a.py": "x = 1\n"})
    base = git(repo, "rev-parse", "HEAD")
    if after is None:
        (repo / name).unlink()
    else:
        (repo / name).write_text(after, encoding="utf-8")
    return [item["detail"] for item in gates.scan_config(repo, base, gates.changed_files(repo, base))]


class TestConfigThresholds:
    @pytest.mark.parametrize(
        ("old", "new", "fragment"),
        [
            ("fail_under = 100", "fail_under = 90", "fail_under"),
            ("max-complexity = 10", "max-complexity = 15", "max-complexity"),
            ("line-length = 120", "line-length = 200", "line-length"),
            ('ignore = ["E501"]', 'ignore = ["E501", "F401"]', "ignore"),
            ('select = ["E", "F", "I"]', 'select = ["E", "F"]', "select"),
            ('omit = ["src/gen.py"]', 'omit = ["src/gen.py", "src/app.py"]', "omit"),
            ('exclude = ["pragma"]', 'exclude = ["pragma", "if x:"]', "exclude"),
            ('"tests/*" = ["S101"]', '"tests/*" = ["S101", "F401"]', "per-file-ignores"),
            ('"tests/*" = ["S101"]', '"tests/*" = ["S101"]\n"src/*" = ["E"]', "per-file-ignores"),
            ('addopts = "--strict-markers --cov=src"', 'addopts = "--cov=src"', "addopts"),
            (
                'addopts = "--strict-markers --cov=src"',
                'addopts = "--strict-markers --cov=src --ignore=tests/slow"',
                "addopts",
            ),
            (
                'addopts = "--strict-markers --cov=src"',
                'addopts = "--strict-markers --cov=src --deselect x"',
                "addopts",
            ),
            ('addopts = "--strict-markers --cov=src"', 'addopts = "--strict-markers --cov=src -p no:cov"', "addopts"),
            ('testpaths = ["tests", "more"]', 'testpaths = ["tests"]', "testpaths"),
        ],
    )
    def test_each_weakened_key_is_floor_lowered(self, tmp_path: Path, old: str, new: str, fragment: str) -> None:
        assert old in PYPROJECT
        details = config_findings(tmp_path, PYPROJECT, PYPROJECT.replace(old, new))
        assert len(details) == 1 and fragment in details[0], details

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("fail_under = 100", "fail_under = 100"),
            ("fail_under = 100", "fail_under = 100.0"),
            ("max-complexity = 10", "max-complexity = 8"),
            ("line-length = 120", "line-length = 100"),
            ('ignore = ["E501"]', "ignore = []"),
            ('select = ["E", "F", "I"]', 'select = ["E", "F", "I", "B"]'),
            ('omit = ["src/gen.py"]', "omit = []"),
            ('testpaths = ["tests", "more"]', 'testpaths = ["more", "tests", "extra"]'),
            ('version = "1.0"', 'version = "2.0"'),
            ('addopts = "--strict-markers --cov=src"', 'addopts = "--cov=src --strict-markers -ra"'),
        ],
    )
    def test_strengthened_or_unrelated_edits_are_not_findings(self, tmp_path: Path, old: str, new: str) -> None:
        assert config_findings(tmp_path, PYPROJECT, PYPROJECT.replace(old, new)) == []

    def test_a_key_moved_to_another_table_is_not_a_removal(self, tmp_path: Path) -> None:
        moved = PYPROJECT.replace('[tool.ruff.lint]\nselect = ["E", "F", "I"]\nignore = ["E501"]\n\n', "")
        moved = moved.replace(
            "line-length = 120\n", 'line-length = 120\nselect = ["E", "F", "I"]\nignore = ["E501"]\n', 1
        )
        assert moved != PYPROJECT
        assert config_findings(tmp_path, PYPROJECT, moved) == []

    def test_a_removed_floor_or_cap_is_floor_lowered(self, tmp_path: Path) -> None:
        details = config_findings(tmp_path, PYPROJECT, PYPROJECT.replace("fail_under = 100\n", ""))
        assert len(details) == 1 and "fail_under" in details[0] and "removed" in details[0]
        details = config_findings(tmp_path / "b", PYPROJECT, PYPROJECT.replace("max-complexity = 10\n", ""))
        assert len(details) == 1 and "max-complexity" in details[0]

    def test_a_changed_extend_is_floor_lowered(self, tmp_path: Path) -> None:
        before = '[tool.ruff]\nextend = "base.toml"\n'
        details = config_findings(tmp_path, before, '[tool.ruff]\nextend = "loose.toml"\n')
        assert len(details) == 1 and "extend" in details[0]

    def test_ruff_toml_setup_cfg_and_coveragerc_are_read_too(self, tmp_path: Path) -> None:
        assert len(config_findings(tmp_path / "a", "line-length = 100\n", "line-length = 150\n", "ruff.toml")) == 1
        assert (
            len(
                config_findings(
                    tmp_path / "b", "[report]\nfail_under = 100\n", "[report]\nfail_under = 80\n", ".coveragerc"
                )
            )
            == 1
        )
        cfg = "[coverage:report]\nfail_under = 100\nomit =\n    a.py\n"
        assert len(config_findings(tmp_path / "c", cfg, cfg.replace("100", "99"), "setup.cfg")) == 1
        assert len(config_findings(tmp_path / "d", cfg, cfg + "    b.py\n", "setup.cfg")) == 1
        assert config_findings(tmp_path / "e", cfg, cfg.replace("100", "100.0"), "setup.cfg") == []

    def test_an_unparsable_new_file_a_deleted_one_and_a_new_one_with_ignores_are_findings(self, tmp_path: Path) -> None:
        assert any("cannot parse" in item for item in config_findings(tmp_path / "a", PYPROJECT, "[tool\nbroken"))
        assert any("deleted" in item for item in config_findings(tmp_path / "b", PYPROJECT, None))
        repo = make_repo(tmp_path / "c", {"a.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nignore = ["E"]\n[tool.coverage.report]\nfail_under = 10\n', encoding="utf-8"
        )
        details = [item["detail"] for item in gates.scan_config(repo, base, gates.changed_files(repo, base))]
        assert len(details) == 1 and "ignore" in details[0]

    def test_other_files_and_a_clean_tree_have_no_config_findings(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "notes.toml").write_text("fail_under = 1\n", encoding="utf-8")
        assert gates.scan_config(repo, base, gates.changed_files(repo, base)) == []

    def test_a_broken_base_file_is_treated_as_empty(self, tmp_path: Path) -> None:
        assert config_findings(tmp_path, "[tool\nbroken", "[tool.ruff]\nline-length = 100\n") == []

    def test_the_finding_carries_the_code_and_the_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"pyproject.toml": PYPROJECT})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "pyproject.toml").write_text(PYPROJECT.replace("fail_under = 100", "fail_under = 1"), encoding="utf-8")
        [item] = gates.scan_config(repo, base, gates.changed_files(repo, base))
        assert item["code"] == "floor-lowered" and item["file"] == "pyproject.toml"
        assert "100" in item["detail"] and "1" in item["detail"]


# --- diff coverage ---------------------------------------------------------------------------------------------


def coverage_json(files: "dict[str, dict[str, Any]]", branch: bool = True) -> dict[str, Any]:
    return {"meta": {"branch_coverage": branch}, "files": files}


def measured(
    executed: "list[int]",
    missing: "list[int]",
    ex_branches: "list[list[int]] | None" = None,
    miss_branches: "list[list[int]] | None" = None,
) -> dict[str, Any]:
    return {
        "executed_lines": executed,
        "missing_lines": missing,
        "executed_branches": ex_branches or [],
        "missing_branches": miss_branches or [],
    }


def changed(path: str, lines: "list[int]", **extra: Any) -> "gates.FileChange":
    return gates.FileChange(path=path, added=dict.fromkeys(lines, "x = 1"), **extra)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, {"src/app.py": "import os\n\n\ndef f(x):\n    if x:\n        return 1\n    return 2\n"})


class TestDiffCoverage:
    def test_it_counts_changed_executable_lines_and_branches_from_changed_sources(self, source_repo: Path) -> None:
        report = coverage_json(
            {
                "src/app.py": measured([1, 4, 5, 7], [6], [[5, 7]], [[5, 6]]),
            }
        )
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [5, 6, 7])], report)
        # lines 5, 6, 7 are executable: 5 and 7 executed, 6 missing (2 of 3); line 5 has one branch each way (1 of 2).
        assert (result["covered"], result["total"], result["failures"]) == (3, 5, [])

    def test_lines_that_are_not_executable_do_not_count(self, source_repo: Path) -> None:
        report = coverage_json({"src/app.py": measured([1, 4, 5, 7], [6])})
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [2, 3])], report)
        assert (result["covered"], result["total"]) == (0, 0)

    def test_only_changed_source_files_are_measured(self, source_repo: Path) -> None:
        report = coverage_json({"src/app.py": measured([1], [4])})
        changes = [
            changed("tests/test_app.py", [1, 2]),
            changed("README.md", [1]),
            changed("pyproject.toml", [1]),
            changed("src/app.py", [4]),
            changed("src/gone.py", [1], status="deleted"),
            changed("blob.py", [1], binary=True),
        ]
        result = gates.diff_coverage(source_repo, changes, report)
        assert (result["covered"], result["total"], result["failures"]) == (0, 1, [])

    def test_a_changed_source_file_the_run_did_not_measure_is_coverage_missing(self, source_repo: Path) -> None:
        report = coverage_json({"src/other.py": measured([1], [])})
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [4, 5])], report)
        assert result["failures"] and result["failures"][0].startswith("coverage-missing")
        assert "src/app.py" in result["failures"][0]

    def test_an_unmeasured_file_whose_changed_lines_hold_no_statement_is_fine(self, source_repo: Path) -> None:
        (source_repo / "src/app.py").write_text("# only a comment\n\n'''doc'''\n", encoding="utf-8")
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [1, 2])], coverage_json({}))
        assert result["failures"] == []

    def test_an_unmeasured_file_that_does_not_parse_is_coverage_missing(self, source_repo: Path) -> None:
        (source_repo / "src/app.py").write_text("def broken(:\n", encoding="utf-8")
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [1])], coverage_json({}))
        assert result["failures"][0].startswith("coverage-missing")

    def test_absolute_paths_in_the_report_are_matched(self, source_repo: Path) -> None:
        report = coverage_json({str(source_repo / "src/app.py"): measured([4], [])})
        assert gates.diff_coverage(source_repo, [changed("src/app.py", [4])], report)["covered"] == 1

    @pytest.mark.parametrize(
        "report",
        [
            [],
            {"files": {}},
            {"meta": {}, "files": {}},
            {"meta": {"branch_coverage": False}, "files": {}},
            {"meta": {"branch_coverage": True}},
            {"meta": {"branch_coverage": True}, "files": []},
            {"meta": {"branch_coverage": True}, "files": {"src/app.py": {"executed_lines": [1]}}},
            {"meta": {"branch_coverage": True}, "files": {"src/app.py": "x"}},
        ],
    )
    def test_a_report_without_branch_data_is_coverage_format(self, source_repo: Path, report: Any) -> None:
        result = gates.diff_coverage(source_repo, [changed("src/app.py", [4])], report)
        assert result["failures"] and result["failures"][0].startswith("coverage-format")
        assert (result["covered"], result["total"]) == (0, 0)

    def test_a_wrong_shaped_file_entry_is_checked_even_when_it_is_not_changed(self, source_repo: Path) -> None:
        report = coverage_json({"src/other.py": {"executed_lines": [1]}})
        result = gates.diff_coverage(source_repo, [], report)
        assert result["failures"][0].startswith("coverage-format")

    def test_a_renamed_file_is_read_under_its_new_name(self, source_repo: Path) -> None:
        report = coverage_json({"src/new.py": measured([4], [])})
        result = gates.diff_coverage(
            source_repo, [changed("src/new.py", [4], status="renamed", old_path="src/app.py")], report
        )
        assert result["covered"] == 1


# --- touched-function complexity ---------------------------------------------------------------------------------

SOURCE = """import os


@decorator
def outer(x):
    def inner(y):
        return y

    return inner(x)


class Box:
    def method(self):
        return 1

    async def later(self):
        return 2


lam = lambda: 0
"""


class TestFunctionExtents:
    def test_every_function_has_its_def_line_and_full_extent(self) -> None:
        extents = {item.name: item for item in gates.function_extents(SOURCE) or []}
        assert set(extents) == {"outer", "inner", "method", "later"}
        assert (extents["outer"].line, extents["outer"].start, extents["outer"].end) == (5, 4, 9)
        assert (extents["inner"].line, extents["inner"].start, extents["inner"].end) == (6, 6, 7)
        assert (extents["method"].start, extents["method"].end) == (13, 14)
        assert (extents["later"].start, extents["later"].end) == (16, 17)

    def test_a_syntax_error_is_none(self) -> None:
        assert gates.function_extents("def broken(:\n") is None

    def test_a_file_without_functions_has_none(self) -> None:
        assert gates.function_extents("x = 1\n") == []


def big_function(name: str = "big", branches: int = 12) -> str:
    body = "".join(f"    if x == {index}:\n        return {index}\n" for index in range(branches))
    return f"def {name}(x):\n{body}    return -1\n"


def complex_repo(tmp_path: Path) -> "tuple[Path, str]":
    repo = make_repo(
        tmp_path,
        {
            "src/legacy.py": big_function("legacy") + "\n\ndef small():\n    return 1\n",
            "src/clean.py": "def ok():\n    return 1\n",
        },
    )
    return repo, git(repo, "rev-parse", "HEAD")


class TestTouchedComplexity:
    def test_a_legacy_violation_the_change_does_not_touch_is_not_counted(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        text = (repo / "src/legacy.py").read_text(encoding="utf-8")
        (repo / "src/legacy.py").write_text(
            text.replace("def small():\n    return 1", "def small():\n    return 2"), encoding="utf-8"
        )
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert (
            result["over_limit"] == [] and result["failures"] == [] and result["touched"] == 1 and result["max"] is None
        )

    def test_touching_the_complex_function_counts_it(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        text = (repo / "src/legacy.py").read_text(encoding="utf-8").replace("return 3", "return 33")
        (repo / "src/legacy.py").write_text(text, encoding="utf-8")
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert [
            (item["file"], item["function"], item["line"], item["complexity"]) for item in result["over_limit"]
        ] == [("src/legacy.py", "legacy", 1, 13)]
        assert result["max"] == 13 and result["touched"] == 1

    def test_a_deletion_only_change_inside_a_function_touches_it(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        lines = (repo / "src/legacy.py").read_text(encoding="utf-8").splitlines(keepends=True)
        del lines[6:8]  # one whole `if` block in the middle of `legacy`
        (repo / "src/legacy.py").write_text("".join(lines), encoding="utf-8")
        changes = gates.changed_files(repo, base)
        assert changes[0].added == {} and changes[0].deletions
        result = gates.touched_complexity(repo, changes)
        assert [item["function"] for item in result["over_limit"]] == ["legacy"]

    def test_a_new_function_over_the_limit_is_counted_and_a_simple_one_is_not(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text(
            "def ok():\n    return 1\n\n\n" + big_function("fresh", 11) + "\n\ndef easy():\n    return 2\n",
            encoding="utf-8",
        )
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert [(item["function"], item["complexity"]) for item in result["over_limit"]] == [("fresh", 12)]
        assert result["touched"] == 2 and result["max"] == 12

    def test_a_function_at_exactly_the_limit_passes(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text(big_function("edge", 9), encoding="utf-8")
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert result["over_limit"] == [] and result["touched"] == 1

    def test_the_repository_config_cannot_raise_the_limit(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "ruff.toml").write_text("[lint.mccabe]\nmax-complexity = 99\n", encoding="utf-8")
        (repo / "src/clean.py").write_text(big_function("fresh", 11), encoding="utf-8")
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert [item["function"] for item in result["over_limit"]] == ["fresh"]

    def test_a_syntax_error_in_a_changed_file_is_a_failure(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text("def broken(:\n", encoding="utf-8")
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert result["failures"] and "src/clean.py" in result["failures"][0]

    def test_no_changed_python_files_is_an_empty_pass(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "NOTES.md").write_text("hello\n", encoding="utf-8")
        assert gates.touched_complexity(repo, gates.changed_files(repo, base)) == {
            "touched": 0,
            "max": None,
            "over_limit": [],
            "failures": [],
        }

    def test_a_deleted_file_is_not_parsed(self, tmp_path: Path) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").unlink()
        assert gates.touched_complexity(repo, gates.changed_files(repo, base))["failures"] == []

    def test_a_ruff_that_cannot_run_is_a_failure_not_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text(big_function("fresh", 11), encoding="utf-8")
        monkeypatch.setattr(gates, "ruff_argv", lambda: ["definitely-not-a-tool"])
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert result["failures"] and "complexity check could not run" in result["failures"][0]

    def test_ruff_output_that_is_not_json_is_a_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text("def ok():\n    return 2\n", encoding="utf-8")
        monkeypatch.setattr(gates, "ruff_argv", lambda: [sys.executable, "-c", "print('nope')", "--"])
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert result["failures"] and "not JSON" in result["failures"][0]


# --- the gate run --------------------------------------------------------------------------------------------

APP = "def f(x):\n    if x:\n        return 1\n    return 2\n"
APP_TEST = "from app import f\n\n\ndef test_f():\n    assert f(1) == 1\n    assert f(0) == 2\n"
OK = [sys.executable, "-c", "pass"]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
GATE_FILES = {
    ".gitignore": "__pycache__/\n.coverage\n",
    "AGENTS.md": "# Agents\n\n```bash\nuv run pytest -q\n```\n",
    "app.py": APP,
    "tests/test_app.py": APP_TEST,
    "NOTES.md": "notes\n",
}


def ok_command(identity: str, kind: str) -> dict[str, Any]:
    return {"id": identity, "kind": kind, "argv": OK}


def fail_command(identity: str, kind: str, code: int = 3) -> dict[str, Any]:
    return {"id": identity, "kind": kind, "argv": [sys.executable, "-c", f"raise SystemExit({code})"]}


FULL_SET = [ok_command("lint", "lint"), ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}]


def start(project: Path, task: str = "T01") -> None:
    from workspace_operations import task_operation

    task_operation(project, "start", task, expected_revision=raw_state(project)["revision"])


@pytest.fixture
def gated(tmp_path: Path) -> "tuple[Path, Path]":
    """A project with approved commands (lint, types, real pytest) and T01 running; `repo` is a real repository."""
    repo = make_repo(tmp_path, GATE_FILES)
    project = make_project(tmp_path, repo)
    approve(project, FULL_SET)
    start(project)
    return project, repo


def work(repo: Path, *, covered: bool = True) -> None:
    """The worker's change: one new function, with a test that covers it unless `covered` is false."""
    (repo / "app.py").write_text(APP + "\n\ndef g(x):\n    return x + 1\n", encoding="utf-8")
    test = APP_TEST.replace("import f", "import f, g") + (
        "\n\ndef test_g():\n    assert g(1) == 2\n" if covered else ""
    )
    (repo / "tests/test_app.py").write_text(test, encoding="utf-8")


def run_gates(project: Path, task: str = "T01") -> dict[str, Any]:
    return workflow(project, "gate-run", {"task": task})


def notes(project: Path, task: str = "T01") -> str:
    return (project / "tasks" / f"{task}.md").read_text(encoding="utf-8")


class TestTestsArgv:
    def test_cov_dot_and_the_report_options_are_appended_when_the_argv_has_no_cov(self, tmp_path: Path) -> None:
        argv = gates.tests_argv(["uv", "run", "pytest", "-q"], tmp_path)
        assert argv[:4] == ["uv", "run", "pytest", "-q"]
        assert argv[4:] == [
            "--cov=.",
            "--cov-branch",
            f"--cov-report=json:{tmp_path}/coverage.json",
            f"--junitxml={tmp_path}/junit.xml",
        ]

    @pytest.mark.parametrize("given", ["--cov=src", "--cov", "--cov=src/a,src/b"])
    def test_an_approved_cov_option_is_kept_and_not_doubled(self, tmp_path: Path, given: str) -> None:
        argv = gates.tests_argv(["pytest", given], tmp_path)
        assert [item for item in argv if item.startswith("--cov") and "-" not in item[5:6]] == [given]
        assert "--cov=." not in argv and "--cov-branch" in argv

    def test_the_options_go_before_a_double_dash_that_follows_pytest(self, tmp_path: Path) -> None:
        argv = gates.tests_argv(["pytest", "-q", "--", "tests/a.py"], tmp_path)
        assert argv.index("--cov=.") < argv.index("--") < argv.index("tests/a.py")
        assert argv[-1] == "tests/a.py"

    def test_a_double_dash_before_pytest_is_not_the_separator(self, tmp_path: Path) -> None:
        argv = gates.tests_argv(["uv", "run", "--", "pytest", "-q"], tmp_path)
        assert argv[-1].startswith("--junitxml=") and argv.index("--") < argv.index("pytest")

    def test_python_dash_m_pytest_is_found(self, tmp_path: Path) -> None:
        argv = gates.tests_argv(["python", "-m", "pytest", "--", "x"], tmp_path)
        assert argv.index("--cov=.") < argv.index("--")


class TestGateRun:
    def test_a_clean_change_passes_and_leaves_a_record_a_finding_and_evidence(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        assert result["passed"] is True and result["failures"] == [], result["failures"]
        assert result["run"] == "gates-t01-1" and result["task"] == "T01"
        assert [(item["id"], item["passed"]) for item in result["gates"]] == [
            ("lint", True),
            ("types", True),
            ("tests", True),
        ]
        assert result["tests"]["cases"] == 2 and result["diff_coverage"]["percent"] == 100.0
        assert result["complexity"]["over_limit"] == [] and result["scan"]["findings"] == []
        record_path = project / "observability" / "T01.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert gates.observe.validate_record(record) == []
        assert result["record_sha256"] == gates.hashlib.sha256(record_path.read_bytes()).hexdigest()
        assert record["head_tree"] == tree(repo, raw_state(project)["gates"]["base_commit"]) == result["head_tree"]
        assert record["base_commit"] == raw_state(project)["gates"]["base_commit"] and record["run"] == "gates-t01-1"
        assert {item["path"] for item in record["files"]} == {"app.py", "tests/test_app.py"}
        assert "gates-t01-1" in notes(project)
        assert f"head_tree={result['head_tree']} run=gates-t01-1 record_sha256={result['record_sha256']}" in notes(
            project
        )
        assert len(result["evidence"]) == 3 and all(item.startswith("ev-") for item in result["evidence"])
        evidence = (project / "evidence.md").read_text(encoding="utf-8")
        assert "--cov=." in evidence and "--cov-branch" in evidence and "--junitxml=" in evidence
        assert not (project / ".gates-scratch" / "gates-t01-1").exists()
        assert not (project / ".operations" / "gates-t01.lock").exists()
        assert lib.validate_project(project).errors == []

    def test_a_rerun_takes_a_fresh_verify_id_and_never_replays(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        first = run_gates(project)
        second = run_gates(project)
        assert (first["run"], second["run"]) == ("gates-t01-1", "gates-t01-2")
        assert (project / ".operations" / "gates-t01-1.json").is_file() and (
            project / ".operations" / "gates-t01-2.json"
        ).is_file()
        assert set(first["evidence"]).isdisjoint(second["evidence"])
        assert "gates-t01-1" in notes(project) and "gates-t01-2" in notes(project)
        assert run_gates(project)["run"] == "gates-t01-3"

    def test_the_run_does_not_hold_the_project_lock_while_commands_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        lock = project / ".project.lock"
        probe = {
            "id": "probe",
            "kind": "lint",
            "argv": [sys.executable, "-c", f"import os, sys; sys.exit(7 if os.path.exists({str(lock)!r}) else 0)"],
        }
        approve(project, [probe, ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}])
        start(project)
        work(repo)
        result = run_gates(project)
        assert result["gates"][0]["passed"] is True
        # The control: with the lock directory present the very same probe exits 7, so the assertion can fail.
        lock.mkdir()
        try:
            held = subprocess.run(probe["argv"], check=False)
        finally:
            lock.rmdir()
        assert held.returncode == 7

    def test_the_run_holds_a_per_task_lock_for_its_whole_duration(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        lock = project / ".operations" / "gates-t01.lock"
        probe = {
            "id": "probe",
            "kind": "lint",
            "argv": [sys.executable, "-c", f"import os, sys; sys.exit(0 if os.path.isdir({str(lock)!r}) else 9)"],
        }
        approve(project, [probe, ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}])
        start(project)
        work(repo)
        assert run_gates(project)["gates"][0]["passed"] is True

    def test_a_concurrent_run_for_the_same_task_is_refused(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        (project / ".operations").mkdir(exist_ok=True)
        with lib.DirectoryLock(project / ".operations" / "gates-t01.lock"):
            with pytest.raises(lib.WorkspaceConflict, match="lock"):
                gates.gates_run(project, {"task": "T01"}, lock_timeout=0.2)

    def test_a_failing_gate_fails_the_run_but_the_record_and_finding_are_still_written(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        approve(
            project,
            [
                fail_command("lint", "lint"),
                ok_command("types", "types"),
                {"id": "tests", "kind": "tests", "argv": PYTEST},
            ],
        )
        start(project)
        work(repo)
        result = run_gates(project)
        assert result["passed"] is False
        assert any(item.startswith("gate-failed: lint") and "exit 3" in item for item in result["failures"])
        assert [(item["id"], item["passed"]) for item in result["gates"]] == [
            ("lint", False),
            ("types", True),
            ("tests", True),
        ]
        assert (project / "observability" / "T01.json").is_file() and "gates-t01-1" in notes(project)

    def test_a_gate_that_cannot_launch_stops_the_batch_and_the_rest_are_not_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        missing = {"id": "lint", "kind": "lint", "argv": ["definitely-not-installed-tool", "check"]}
        approve(project, [missing, ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}])
        start(project)
        result = run_gates(project)
        assert result["passed"] is False
        assert any(item.startswith("gate-failed: lint") for item in result["failures"])
        assert any(item.startswith("gate-not-run: types") for item in result["failures"])
        assert any(item.startswith("gate-not-run: tests") for item in result["failures"])

    def test_a_tests_command_that_exits_zero_but_ran_nothing_fails(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        idle = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-c", "pass", "pytest"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), idle])
        start(project)
        work(repo)
        failures = run_gates(project)["failures"]
        assert any("wrote no junit report" in item for item in failures), failures
        assert any(item.startswith("coverage-format") for item in failures), failures

    def test_a_junit_report_with_no_cases_fails(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        writer = (
            "import sys, pathlib; "
            "[(pathlib.Path(a.split('=', 1)[1]).write_text('<testsuites><testsuite tests=\"0\"/></testsuites>') "
            "if a.startswith('--junitxml=') else None) for a in sys.argv]"
        )
        empty = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-c", writer, "pytest"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), empty])
        start(project)
        failures = run_gates(project)["failures"]
        assert any("ran 0 test cases" in item for item in failures), failures

    def test_a_bypass_in_the_diff_fails_the_run_and_is_in_the_record(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "z = 1  # noqa\n", encoding="utf-8"
        )
        result = run_gates(project)
        assert result["passed"] is False
        assert any(item.startswith("bypass-added: app.py:") and "noqa" in item for item in result["failures"])
        record = json.loads((project / "observability" / "T01.json").read_text(encoding="utf-8"))
        assert [item["detail"] for item in record["scan"]["findings"]] == ["noqa"]

    def test_a_lowered_threshold_in_the_diff_fails_the_run(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        (repo / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 100\n", encoding="utf-8")
        base = commit_all(repo, "add config")
        state = raw_state(project)
        state["gates"]["base_commit"] = base
        write_state(project, state)
        work(repo)
        (repo / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 50\n", encoding="utf-8")
        failures = run_gates(project)["failures"]
        assert any(item.startswith("floor-lowered: pyproject.toml") for item in failures), failures

    def test_diff_coverage_below_the_floor_fails(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo, covered=False)
        result = run_gates(project)
        assert result["diff_coverage"]["percent"] < 90
        assert any(item.startswith("gate-failed: diff coverage") and "below 90" in item for item in result["failures"])

    def test_a_changed_file_outside_the_cov_directory_is_coverage_missing(self, tmp_path: Path) -> None:
        repo = make_repo(
            tmp_path,
            {
                **GATE_FILES,
                "pkg/__init__.py": "",
                "pkg/mod.py": "def h(x):\n    if x:\n        return 1\n    return 0\n",
                "tests/test_pkg.py": (
                    "from pkg.mod import h\n\n\ndef test_h():\n    assert h(1) == 1\n    assert h(0) == 0\n"
                ),
            },
        )
        project = make_project(tmp_path, repo)
        narrow = {"id": "tests", "kind": "tests", "argv": [*PYTEST, "--cov=pkg"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), narrow])
        start(project)
        work(repo)
        failures = run_gates(project)["failures"]
        assert any(item.startswith("coverage-missing") and "app.py" in item for item in failures), failures

    def test_complexity_over_the_limit_in_a_touched_function_fails(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "\n\n" + big_function("big", 11), encoding="utf-8"
        )
        (repo / "tests/test_app.py").write_text(
            (repo / "tests/test_app.py").read_text(encoding="utf-8")
            + "\n\ndef test_big():\n    from app import big\n\n"
            + "    assert [big(i) for i in range(12)] == [*range(11), -1]\n",
            encoding="utf-8",
        )
        result = run_gates(project)
        assert any(item.startswith("gate-failed: complexity 12") and "big" in item for item in result["failures"]), (
            result["failures"]
        )
        assert result["complexity"]["max"] == 12

    def test_the_commands_run_in_the_target_repository(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        # The test process runs elsewhere, so a command that ignored `cwd` would write its file here.
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        probe = {
            "id": "probe",
            "kind": "lint",
            "argv": [sys.executable, "-c", "import pathlib; pathlib.Path('ran-here.txt').write_text('x')"],
        }
        approve(project, [probe, ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}])
        start(project)
        work(repo)
        run_gates(project)
        assert (repo / "ran-here.txt").is_file() and not (other / "ran-here.txt").exists()
        assert not any(project.glob("ran-here.txt"))

    def test_with_everything_waived_the_run_is_scan_only_and_still_writes_the_finding(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        waive(project, ["lint", "types", "tests"])
        start(project)
        work(repo)
        result = run_gates(project)
        assert result["passed"] is True and result["gates"] == [] and result["evidence"] == []
        journal = json.loads((project / ".operations" / "gates-t01-1.json").read_text(encoding="utf-8"))
        assert journal["kind"] == "scan" and "gates-t01-1" in notes(project)
        assert (project / "observability" / "T01.json").is_file()
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "z = 1  # noqa\n", encoding="utf-8"
        )
        again = run_gates(project)
        assert again["run"] == "gates-t01-2" and again["passed"] is False

    def test_a_waived_tests_kind_leaves_coverage_unmeasured_without_failing(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        approve(project, [ok_command("lint", "lint")])
        waive(project, ["types", "tests"])
        start(project)
        work(repo)
        result = run_gates(project)
        assert result["passed"] is True and result["diff_coverage"]["note"] == "waived"

    def test_no_repository_waiver_needs_no_run(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        set_worktrees(project, [])
        waive(project, ["no-repository"])
        start(project)
        result = run_gates(project)
        assert result["passed"] is True and "no-repository" in result["skipped"]
        assert not (project / "observability").exists()

    @pytest.mark.parametrize(
        ("prepare", "message"),
        [
            ("none", "gates-not-approved: this project has no enabled gates"),
            ("unapproved", "gates-not-approved: no approved gate commands"),
            ("lint-only", r"gates-not-approved: no approved command or waiver for kind\(s\) types, tests"),
            ("source", "gates-source-changed: AGENTS.md"),
            ("not-running", "task T01 is TODO"),
        ],
    )
    def test_a_run_is_refused_without_a_sound_approval(self, tmp_path: Path, prepare: str, message: str) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo, gates_field=gates_object() if prepare == "unapproved" else None)
        if prepare == "lint-only":
            approve(project, [ok_command("lint", "lint")])
        if prepare == "not-running":
            approve(project, FULL_SET)
        if prepare == "source":
            approve(project, FULL_SET)
            (repo / "AGENTS.md").write_text("# Agents\n\n```bash\nuv run pytest -q --other\n```\n", encoding="utf-8")
        if prepare != "not-running":
            start(project)
        with pytest.raises(WorkspaceError, match=message):
            run_gates(project)
        assert not (project / "observability").exists()

    def test_an_unknown_task_and_a_bad_request_are_refused(self, gated: "tuple[Path, Path]") -> None:
        project, _ = gated
        with pytest.raises(WorkspaceError, match="unknown task: T99"):
            run_gates(project, "T99")
        with pytest.raises(WorkspaceError, match="missing required field"):
            workflow(project, "gate-run", {})
        with pytest.raises(WorkspaceError, match="unknown field"):
            workflow(project, "gate-run", {"task": "T01", "extra": 1})

    def test_a_disabled_gates_object_refuses_the_run(self, gated: "tuple[Path, Path]") -> None:
        project, _ = gated
        state = raw_state(project)
        state["gates"]["enabled"] = False
        write_state(project, state)
        with pytest.raises(WorkspaceError, match="no enabled gates"):
            run_gates(project)


# --- the finish gate: every commit path -----------------------------------------------------------------------

REMEDY = "start the task, run gate-run, then finish"


def evidence_anchor(project: Path, task: str = "T01") -> "tuple[str, dict[str, Any]]":
    record = str(lib.record_evidence_result(project, task, OK).record_id)
    return record, {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{record}"}


def revision_of(project: Path) -> int:
    return int(raw_state(project)["revision"])


def finish(project: Path, task: str = "T01", **extra: Any) -> dict[str, Any]:
    from workspace_operations import task_operation

    record = extra.pop("record", None) or evidence_anchor(project, task)[0]
    return task_operation(
        project, "finish", task, expected_revision=revision_of(project), evidence_record_ids=[record], **extra
    )


def refused_text(call: Any) -> str:
    with pytest.raises(WorkspaceError) as caught:
        call()
    return str(caught.value)


def snapshot_files(project: Path) -> "dict[str, bytes]":
    return {name: data for name, data in tree_files(project).items() if not name.startswith(".operations")}


class TestFinishGate:
    def test_finish_is_refused_without_a_run_and_says_what_to_do(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        record = evidence_anchor(project)[0]
        revision = revision_of(project)
        text = refused_text(lambda: finish(project, record=record))
        assert text.startswith("task T01 cannot finish: ") and "gate-not-run" in text and REMEDY in text
        assert raw_state(project)["tasks"][0]["status"] == "RUNNING" and revision_of(project) == revision

    def test_finish_succeeds_after_a_passing_run(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        assert result["passed"] is True
        finish(project, record=result["evidence"][0])
        assert raw_state(project)["tasks"][0]["status"] == "DONE"

    def test_a_change_after_the_run_is_stale_until_the_gates_run_again(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "\n\ndef h():\n    return 0\n", encoding="utf-8"
        )
        text = refused_text(lambda: finish(project, record=result["evidence"][0]))
        assert "gates-stale" in text and "gate-run" in text
        (repo / "tests/test_app.py").write_text(
            (repo / "tests/test_app.py").read_text(encoding="utf-8").replace("import f, g", "import f, g, h")
            + "\n\ndef test_h():\n    assert h() == 0\n",
            encoding="utf-8",
        )
        second = run_gates(project)
        assert second["passed"] is True
        finish(project, record=second["evidence"][0])
        assert raw_state(project)["tasks"][0]["status"] == "DONE"

    def test_a_new_untracked_file_after_the_run_is_stale(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        (repo / "extra.txt").write_text("x\n", encoding="utf-8")
        assert "gates-stale" in refused_text(lambda: finish(project, record=result["evidence"][0]))

    def test_a_failing_run_blocks_the_finish_and_names_the_cause(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        approve(
            project,
            [
                fail_command("lint", "lint"),
                ok_command("types", "types"),
                {"id": "tests", "kind": "tests", "argv": PYTEST},
            ],
        )
        start(project)
        work(repo)
        run_gates(project)
        text = refused_text(lambda: finish(project))
        assert "gate-failed: lint (lint) exited with exit 3" in text

    def test_a_bypass_added_blocks_the_finish(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "z = 1  # noqa\n", encoding="utf-8"
        )
        result = run_gates(project)
        assert "bypass-added: app.py:" in refused_text(lambda: finish(project, record=result["evidence"][0]))

    def test_prose_tasks_and_ungated_projects_finish_without_a_run(
        self, gated: "tuple[Path, Path]", tmp_path: Path
    ) -> None:
        project, _ = gated
        start(project, "T02")
        finish(project, "T02")
        assert raw_state(project)["tasks"][1]["status"] == "DONE"
        for field in (None, gates_object(enabled=False)):
            other = tmp_path / ("a" if field is None else "b")
            other.mkdir()
            ungated = make_project(other, gates_field=field)
            start(ungated)
            finish(ungated)
            assert raw_state(ungated)["tasks"][0]["status"] == "DONE"

    def test_changed_commands_or_sources_after_the_run_force_a_new_run_or_approval(
        self, gated: "tuple[Path, Path]"
    ) -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        approve(project, [{**FULL_SET[0], "argv": [*OK, "--again"]}], "ga2")
        assert "gates-stale" in refused_text(lambda: finish(project, record=result["evidence"][0]))
        (repo / "AGENTS.md").write_text("# Agents\n\nchanged\n", encoding="utf-8")
        assert "gates-source-changed: AGENTS.md" in refused_text(lambda: finish(project, record=result["evidence"][0]))

    def test_a_missing_or_altered_record_a_forged_finding_or_a_failed_journal_are_refused(
        self, gated: "tuple[Path, Path]"
    ) -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        record_path = project / "observability" / "T01.json"
        original = record_path.read_text(encoding="utf-8")
        journal_path = project / ".operations" / "gates-t01-1.json"
        journal_text = journal_path.read_text(encoding="utf-8")
        notes_path = project / "tasks" / "T01.md"
        notes_text = notes_path.read_text(encoding="utf-8")
        record = result["evidence"][0]

        record_path.write_text(original.replace('"cases": 2', '"cases": 3'), encoding="utf-8")
        assert "gate-failed: the record changed since the run" in refused_text(lambda: finish(project, record=record))
        record_path.unlink()
        assert "gate-not-run: the record" in refused_text(lambda: finish(project, record=record))
        record_path.write_text(original, encoding="utf-8")

        journal = json.loads(journal_text)
        journal["attempts"][0]["result"]["exit_code"] = 1
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        assert "gate-failed: lint" in refused_text(lambda: finish(project, record=record))
        journal["attempts"] = journal["attempts"][:1]
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        assert "gate-failed: the run journal holds 1 attempt(s) for 3 approved command(s)" in refused_text(
            lambda: finish(project, record=record)
        )
        journal_path.unlink()
        assert "gate-not-run: the run journal gates-t01-1" in refused_text(lambda: finish(project, record=record))
        journal_path.write_text("{not json", encoding="utf-8")
        assert "gate-failed: the run journal gates-t01-1 cannot be read" in refused_text(
            lambda: finish(project, record=record)
        )
        journal_path.write_text(journal_text, encoding="utf-8")

        forged = notes_text.replace(result["head_tree"], "0" * 64)
        notes_path.write_text(forged, encoding="utf-8")
        assert "gates-stale" in refused_text(lambda: finish(project, record=record))
        other_run = notes_text.replace(f"run={result['run']}", "run=gates-t01-9")
        notes_path.write_text(other_run, encoding="utf-8")
        assert "gates-stale: the record belongs to run gates-t01-1" in refused_text(
            lambda: finish(project, record=record)
        )
        notes_path.write_text(notes_text, encoding="utf-8")
        finish(project, record=record)  # the control: restored files finish again
        assert raw_state(project)["tasks"][0]["status"] == "DONE"

    def test_an_unreadable_finding_line_is_gate_not_run(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        notes_path = project / "tasks" / "T01.md"
        notes_path.write_text(notes_path.read_text(encoding="utf-8").replace("head_tree=", "tree="), encoding="utf-8")
        assert "gate-not-run" in refused_text(lambda: finish(project, record=result["evidence"][0]))

    def test_a_malformed_record_is_refused_even_with_a_matching_hash(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        record_path = project / "observability" / "T01.json"
        broken = json.loads(record_path.read_text(encoding="utf-8"))
        broken["tests"] = "many"
        record_path.write_text(json.dumps(broken), encoding="utf-8")
        digest = gates.hashlib.sha256(record_path.read_bytes()).hexdigest()
        notes_path = project / "tasks" / "T01.md"
        notes_path.write_text(
            notes_path.read_text(encoding="utf-8").replace(result["record_sha256"], digest), encoding="utf-8"
        )
        assert "gate-failed: the record is malformed" in refused_text(
            lambda: finish(project, record=result["evidence"][0])
        )

    def test_only_the_newest_run_counts(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        first = run_gates(project)
        (repo / "app.py").write_text(
            (repo / "app.py").read_text(encoding="utf-8") + "z = 1  # noqa\n", encoding="utf-8"
        )
        second = run_gates(project)
        assert second["passed"] is False and first["passed"] is True
        assert "bypass-added" in refused_text(lambda: finish(project, record=second["evidence"][0]))

    def test_the_no_repository_waiver_is_accepted_instead_of_a_run(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        set_worktrees(project, [])
        waive(project, ["no-repository"])
        start(project)
        finish(project)
        assert raw_state(project)["tasks"][0]["status"] == "DONE"

    def test_a_project_with_nothing_approved_says_so(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, gates_field=gates_object())
        start(project)
        text = refused_text(lambda: finish(project))
        assert "gates-not-approved: no approved gate commands" in text

    def test_a_lint_only_set_never_passes(self, tmp_path: Path) -> None:
        project = make_project(tmp_path, make_repo(tmp_path, GATE_FILES))
        approve(project, [ok_command("lint", "lint")])
        start(project)
        assert "no approved command or waiver for kind(s) types, tests" in refused_text(lambda: finish(project))

    def test_a_target_that_vanished_is_refused(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        set_worktrees(project, [])
        assert "gates-no-repository" in refused_text(lambda: finish(project, record=result["evidence"][0]))


class TestEveryCommitPath:
    """The same refusal and the same remedy on every path that could mark a gated code task DONE."""

    def test_update_refuses_and_writes_nothing(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        _, anchor = evidence_anchor(project)
        before = snapshot_files(project)
        text = refused_text(
            lambda: update_project_data(
                project,
                {"tasks": [{"id": "T01", "status": "DONE", "evidence": [anchor]}]},
                expected_revision=revision_of(project),
            )
        )
        assert "task T01: gate-not-run" in text and REMEDY in text
        assert snapshot_files(project) == before

    def test_reconcile_refuses_and_writes_no_decision_line(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        _, anchor = evidence_anchor(project)
        before = snapshot_files(project)
        text = refused_text(
            lambda: workflow(
                project,
                "reconcile",
                request(
                    project,
                    "rec-1",
                    patch={"tasks": [{"id": "T01", "status": "DONE", "evidence": [anchor]}]},
                    decision="Mark T01 done",
                    continuation={"next": "Go on."},
                ),
            )
        )
        assert "task T01: gate-not-run" in text and REMEDY in text
        assert snapshot_files(project) == before

    def test_backfill_is_refused_before_anything_is_written(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        approve(project, FULL_SET)
        record = evidence_anchor(project)[0]
        before = snapshot_files(project)
        text = refused_text(lambda: finish(project, record=record, backfill=True, note="done earlier"))
        assert "gate-not-run" in text and REMEDY in text
        assert raw_state(project)["tasks"][0]["status"] == "TODO" and snapshot_files(project) == before

    def test_the_shared_gate_covers_correct_even_though_correct_creates_only_todo_tasks(
        self, gated: "tuple[Path, Path]"
    ) -> None:
        project, _ = gated
        previous = raw_state(project)
        candidate = copy.deepcopy(previous)
        candidate["tasks"][0]["status"] = "DONE"
        errors, _warnings = lib.transition_gate_errors(project, previous, candidate, "correct")
        assert any("task T01: gate-not-run" in item and REMEDY in item for item in errors)
        start(project, "T02")
        finish(project, "T02")
        result = workflow(
            project,
            "correct",
            request(
                project,
                "cor-1",
                task="T02",
                reason="redo",
                replacement=task_plan("X"),
            )
            if False
            else request(
                project,
                "cor-1",
                task="T02",
                reason="redo",
                replacement={key: value for key, value in task_plan("X").items() if key != "id"},
            ),
        )
        assert result["task"] == "T03" and raw_state(project)["tasks"][-1]["status"] == "TODO"

    def test_the_commit_check_reports_it_without_committing(self, gated: "tuple[Path, Path]") -> None:
        project, _ = gated
        candidate = copy.deepcopy(raw_state(project))
        candidate["tasks"][0]["status"] = "DONE"
        report = lib.check_state_candidate(project, candidate)
        assert any("gate-not-run" in item for item in report.errors)

    @pytest.mark.parametrize(
        "broken", [{"enabled": True}, {"enabled": True, "commands": "x", "approval": None, "waiver": None}]
    )
    def test_a_malformed_gates_object_in_a_candidate_is_a_finding_not_an_exception(
        self, gated: "tuple[Path, Path]", broken: Any
    ) -> None:
        project, _ = gated
        candidate = copy.deepcopy(raw_state(project))
        candidate["gates"] = broken
        candidate["tasks"][0]["status"] = "DONE"
        report = lib.check_state_candidate(project, candidate, check_files=True)
        assert any("gates" in item for item in report.errors)
        assert not any("gate-not-run" in item for item in report.errors)

    def test_a_task_that_was_already_done_is_not_judged_again(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        finish(project, record=result["evidence"][0])
        (repo / "late.txt").write_text("after the finish\n", encoding="utf-8")
        update_project_data(
            project, {"tasks": [{"id": "T02", "name": "Renamed"}]}, expected_revision=revision_of(project)
        )
        assert raw_state(project)["tasks"][1]["name"] == "Renamed"

    def test_a_passing_run_lets_update_and_reconcile_through_too(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        anchor = {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{result['evidence'][0]}"}
        update_project_data(
            project,
            {"tasks": [{"id": "T01", "status": "DONE", "evidence": [anchor]}]},
            expected_revision=revision_of(project),
        )
        assert raw_state(project)["tasks"][0]["status"] == "DONE"


# --- the default for new projects and the opt-out ------------------------------------------------------------

ENV_NAME = "RESEARCH_NEW_PROJECT_GATES"
DEFAULT_GATES = {"enabled": True, "commands": [], "approval": None, "waiver": None}
LAUNCHER = SKILL_SCRIPTS / "research-project"


def read_constant(value: "str | None") -> str:
    """`NEW_PROJECT_GATES` as a fresh interpreter reads it with the variable set to `value` (None: unset)."""
    environment = {key: item for key, item in os.environ.items() if key != ENV_NAME}
    if value is not None:
        environment[ENV_NAME] = value
    code = (
        f"import sys; sys.path.insert(0, {str(SKILL_SCRIPTS)!r}); "
        "import workspace_lib; print(workspace_lib.NEW_PROJECT_GATES)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment, check=True)
    return done.stdout.strip()


def init_through_launcher(tmp_path: Path, environment: "dict[str, str]") -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    done = subprocess.run(
        [
            str(LAUNCHER),
            "init",
            "--tag",
            "test",
            str(workspace),
            "--title",
            "Launcher project",
            "--working-directory",
            str(target),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return Path(done.stdout.strip())


class TestNewProjectDefault:
    def test_a_new_project_is_gated_when_the_constant_is_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lib, "NEW_PROJECT_GATES", True)
        target = tmp_path / "target"
        target.mkdir()
        project = allocate_project(tmp_path / "ws", title="Fresh", working_directory=target, create_root=True)
        state = raw_state(project)
        assert state["gates"] == DEFAULT_GATES
        assert validate_v4_state(state, project, check_files=True).errors == []

    def test_the_opt_out_leaves_the_field_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lib, "NEW_PROJECT_GATES", False)
        target = tmp_path / "target"
        target.mkdir()
        project = allocate_project(tmp_path / "ws", title="Plain", working_directory=target, create_root=True)
        assert "gates" not in raw_state(project)

    def test_init_and_the_follow_up_project_go_through_the_same_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import workspace_operations

        monkeypatch.setattr(lib, "NEW_PROJECT_GATES", True)
        target = tmp_path / "target"
        target.mkdir()
        initialized, _warnings = workspace_operations.init_project(
            tmp_path / "ws", "Init route", target, create_root=True, tags=["test"]
        )
        assert raw_state(initialized)["gates"] == DEFAULT_GATES

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, "True"), ("1", "True"), ("yes", "True"), ("", "True"), ("0", "False")]
    )
    def test_the_environment_variable_decides_at_import(self, value: "str | None", expected: str) -> None:
        assert read_constant(value) == expected

    def test_the_suite_runs_with_the_variable_set_to_zero_and_the_constant_off(self) -> None:
        assert os.environ[ENV_NAME] == "0"
        assert lib.NEW_PROJECT_GATES is False

    def test_a_project_a_subprocess_creates_follows_the_variable(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        opted_out = init_through_launcher(tmp_path / "a", {**os.environ, ENV_NAME: "0"})
        assert "gates" not in raw_state(opted_out)
        default = {key: item for key, item in os.environ.items() if key != ENV_NAME}
        gated = init_through_launcher(tmp_path / "b", default)
        assert raw_state(gated)["gates"] == DEFAULT_GATES
        validated = subprocess.run(
            [str(SKILL_SCRIPTS / "research-validate"), str(gated)], capture_output=True, text=True, check=False
        )
        assert validated.returncode == 0, validated.stderr

    def test_a_gated_new_project_cannot_finish_a_code_task_until_commands_are_approved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lib, "NEW_PROJECT_GATES", True)
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo, gates_field=copy.deepcopy(DEFAULT_GATES))
        start(project)
        assert "gates-not-approved" in refused_text(lambda: finish(project))


# --- edges: unreadable files, odd input and the defensive branches ----------------------------------------------


class TestEdges:
    @pytest.mark.parametrize(
        ("argv", "kind"),
        [
            (["ruff", "check", ".", "--fix"], None),
            (["ruff", "format", "."], None),
            (["ruff", "format", "--check", "."], "format"),
            (["ruff", "version"], None),
            (["ty", "version"], None),
            (["echo", "hi"], None),
            (["python", "-m", "mypy", "src"], "types"),
            (["flake8"], "lint"),
        ],
    )
    def test_classify_command(self, argv: "list[str]", kind: "str | None") -> None:
        assert gates.classify_command(argv) == kind

    def test_an_unbalanced_quote_is_not_a_command(self) -> None:
        assert gates._split("echo 'unterminated") is None
        assert gates._split("   ") is None

    def test_git_that_cannot_run_is_a_repository_refusal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no git here")

        monkeypatch.setattr(gates.subprocess, "run", broken)
        with pytest.raises(WorkspaceError, match="gates-no-repository: git could not run"):
            gates.current_head(tmp_path)

    def test_discovery_survives_a_broken_pyproject_an_odd_tool_table_and_undecodable_text(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"pyproject.toml": "[tool\nbroken"})
        assert gates.discover_in(repo)["candidates"] == []
        (repo / "pyproject.toml").write_text("tool = 3\n", encoding="utf-8")
        assert gates.discover_in(repo)["candidates"] == []
        (repo / "AGENTS.md").write_bytes(b"\xff\xfe```\npytest\n```\n")
        assert gates.discover_in(repo) == {"candidates": [], "sources": []}

    def test_the_same_command_in_two_documents_is_listed_once(self, tmp_path: Path) -> None:
        text = "```bash\nuv run pytest -q\n```\n"
        repo = make_repo(tmp_path, {"AGENTS.md": text, "CLAUDE.md": text})
        found = gates.discover_in(repo)
        assert [item["source"] for item in found["candidates"]] == ["AGENTS.md"]
        assert {item["path"] for item in found["sources"]} == {"AGENTS.md", "CLAUDE.md"}

    def test_discovery_on_an_unborn_repository_has_no_head(self, tmp_path: Path) -> None:
        repo = tmp_path / "unborn"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        project = make_project(tmp_path, repo)
        assert gates.gates_discover(project)["head"] is None

    def test_an_unknown_operation_is_refused(self, documented: "tuple[Path, Path]") -> None:
        project, _ = documented
        with pytest.raises(WorkspaceError, match="operation must be approve or waive"):
            workflow(project, "gates", gates_request(project, "gz", "zap", confirmation=USER))

    def test_text_before_the_first_file_header_is_ignored(self) -> None:
        assert next(item.path for item in gates.parse_diff("noise\n" + DIFF)) == "src/app.py"

    def test_a_broken_symlink_is_an_added_file_whose_content_is_its_target(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "dangling").symlink_to("nowhere")
        found = {item.path: item for item in gates.changed_files(repo, base)}
        assert found["dangling"].status == "added" and found["dangling"].added == {1: "nowhere"}

    def test_the_scan_still_reads_added_lines_when_the_file_has_gone(self, tmp_path: Path) -> None:
        change = gates.FileChange(path="ghost.py", status="added", added={1: "x = 1  # noqa"})
        findings, _ = gates.scan_changes(tmp_path, [change])
        assert [item["detail"] for item in findings] == ["noqa"]

    def test_a_skip_on_the_line_of_an_if_is_guarded(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "test_c.py").write_text("import pytest\n\nif True: pytest.skip('whole module')\n", encoding="utf-8")
        findings, info = scan(repo, base)
        assert findings == [] and [item["code"] for item in info] == ["conditional-skip"]

    def test_setup_cfg_options_and_a_broken_cfg(self, tmp_path: Path) -> None:
        before = "[tool:pytest]\naddopts = -q --strict-markers\n"
        assert len(config_findings(tmp_path / "a", before, "[tool:pytest]\naddopts = -q\n", "setup.cfg")) == 1
        assert any(
            "cannot parse" in item for item in config_findings(tmp_path / "b", before, "[tool:pytest\nx\n", "setup.cfg")
        )

    def test_a_coverage_key_outside_the_repository_is_kept_as_it_is(self, tmp_path: Path) -> None:
        report = coverage_json({"/elsewhere/other.py": measured([1], [])})
        assert gates.diff_coverage(tmp_path, [], report) == {"covered": 0, "total": 0, "failures": []}

    def test_a_changed_source_file_that_is_gone_is_coverage_missing(self, tmp_path: Path) -> None:
        result = gates.diff_coverage(tmp_path, [changed("ghost.py", [1])], coverage_json({}))
        assert result["failures"][0].startswith("coverage-missing")

    def test_without_a_pytest_token_the_options_go_after_the_whole_argv(self, tmp_path: Path) -> None:
        argv = gates.tests_argv(["python", "-c", "pass"], tmp_path)
        assert argv[:3] == ["python", "-c", "pass"] and argv[3] == "--cov=."

    def test_ruff_is_found_on_the_path_then_as_a_module_then_through_uv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gates.shutil, "which", lambda name: "/bin/ruff")
        assert gates.ruff_argv() == ["/bin/ruff"]
        monkeypatch.setattr(gates.shutil, "which", lambda name: None)
        monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name: object())
        assert gates.ruff_argv() == [sys.executable, "-m", "ruff"]
        monkeypatch.setattr(gates.importlib.util, "find_spec", lambda name: None)
        assert gates.ruff_argv() == ["uv", "run", "ruff"]

    @pytest.mark.parametrize(
        ("program", "message"),
        [("import sys; sys.exit(2)", "ruff exited 2"), ("print('{}')", "was not JSON list")],
    )
    def test_ruff_that_fails_or_answers_oddly_is_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str, message: str
    ) -> None:
        repo, base = complex_repo(tmp_path)
        (repo / "src/clean.py").write_text("def ok():\n    return 2\n", encoding="utf-8")
        monkeypatch.setattr(gates, "ruff_argv", lambda: [sys.executable, "-c", program, "--"])
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert any(message in item for item in result["failures"]), result["failures"]

    def test_a_changed_file_that_cannot_be_read_fails_the_complexity_check(self, tmp_path: Path) -> None:
        change = gates.FileChange(path="ghost.py", added={1: "x"})
        result = gates.touched_complexity(tmp_path, [change])
        assert len(result["failures"]) == 1 and result["failures"][0].startswith("ghost.py: cannot be read")

    def test_a_deleted_approved_source_is_reported_as_changed(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        (repo / "AGENTS.md").unlink()
        with pytest.raises(WorkspaceError, match=r"gates-source-changed: AGENTS\.md"):
            run_gates(project)

    def test_a_scratch_directory_another_run_left_is_not_removed_with_ours(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        other = project / ".gates-scratch" / "other"
        probe = {
            "id": "probe",
            "kind": "lint",
            "argv": [sys.executable, "-c", f"import pathlib; pathlib.Path({str(other)!r}).mkdir(parents=True)"],
        }
        approve(project, [probe, ok_command("types", "types"), {"id": "tests", "kind": "tests", "argv": PYTEST}])
        start(project)
        work(repo)
        assert run_gates(project)["passed"] is True
        assert other.is_dir() and not (project / ".gates-scratch" / "gates-t01-1").exists()

    def test_a_record_the_validator_refuses_is_never_written(
        self, gated: "tuple[Path, Path]", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, repo = gated
        work(repo)
        monkeypatch.setattr(gates.observe, "validate_record", lambda record: ["bad shape"])
        with pytest.raises(WorkspaceError, match="gate record is malformed: bad shape"):
            run_gates(project)
        assert not (project / "observability").exists()

    def test_two_tests_commands_add_their_cases_and_the_first_gives_the_coverage(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        second = {"id": "tests2", "kind": "tests", "argv": [*PYTEST, "-k", "test_g"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), FULL_SET[2], second])
        start(project)
        work(repo)
        result = run_gates(project)
        assert result["passed"] is True and result["tests"]["cases"] == 3
        assert result["diff_coverage"]["percent"] == 100.0

    def test_a_notes_file_without_a_gate_entry_is_not_run(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        (project / "tasks").mkdir(exist_ok=True)
        (project / "tasks" / "T01.md").write_text("# Task T01 notes\n", encoding="utf-8")
        assert "gate-not-run: no gate run is recorded" in refused_text(lambda: finish(project))

    def test_a_record_that_is_not_json_is_malformed_even_with_a_matching_hash(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        result = run_gates(project)
        record_path = project / "observability" / "T01.json"
        record_path.write_text("{not json", encoding="utf-8")
        digest = gates.hashlib.sha256(record_path.read_bytes()).hexdigest()
        notes_path = project / "tasks" / "T01.md"
        notes_path.write_text(
            notes_path.read_text(encoding="utf-8").replace(result["record_sha256"], digest), encoding="utf-8"
        )
        assert "gate-failed: the record is malformed" in refused_text(lambda: finish(project))

    def test_a_tree_that_cannot_be_read_at_finish_is_stale(
        self, gated: "tuple[Path, Path]", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, repo = gated
        work(repo)
        run_gates(project)

        def broken(*args: Any, **kwargs: Any) -> str:
            raise WorkspaceError("git went away")

        monkeypatch.setattr(gates, "head_tree", broken)
        assert "gates-stale: the tree cannot be read (git went away)" in refused_text(lambda: finish(project))


# --- corrections after the diff review (round 17) ------------------------------------------------------------


def findings_of(repo: Path, base: str) -> "list[tuple[str, str]]":
    return [(item["file"], item["detail"]) for item in scan(repo, base)[0]]


class TestGitattributesCannotHideAFile:
    @pytest.mark.parametrize("rule", ["*.py -diff", "feature.py binary", "*.py -text -diff"])
    def test_a_rule_in_the_base_does_not_hide_a_changed_py_file(self, tmp_path: Path, rule: str) -> None:
        repo = make_repo(tmp_path, {".gitattributes": f"{rule}\n", "feature.py": "a = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text("a = 1\nb = 2  # noqa\n", encoding="utf-8")
        changes = gates.changed_files(repo, base)
        assert [(item.path, item.binary, sorted(item.added)) for item in changes] == [("feature.py", False, [2])]
        assert findings_of(repo, base) == [("feature.py", "noqa")]

    def test_a_rule_in_info_attributes_does_not_hide_it_either(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"feature.py": "a = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / ".git" / "info" / "attributes").write_text("*.py -diff\n", encoding="utf-8")
        (repo / "feature.py").write_text("a = 1\nb = 2  # noqa\n", encoding="utf-8")
        assert findings_of(repo, base) == [("feature.py", "noqa")]

    def test_a_changed_gitattributes_is_itself_a_finding(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "sub").mkdir()
        (repo / "sub" / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
        found = findings_of(repo, base)
        assert len(found) == 1 and found[0][0] == "sub/.gitattributes" and ".gitattributes" in found[0][1]

    def test_a_py_file_with_a_nul_byte_is_a_finding_not_a_skip(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_bytes(b"a = 1\n\x00\nb = 2  # noqa\n")
        found = findings_of(repo, base)
        assert found and all(name == "feature.py" for name, _ in found), found

    def test_the_control_without_a_rule_finds_the_same_noqa(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"feature.py": "a = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text("a = 1\nb = 2  # noqa\n", encoding="utf-8")
        assert findings_of(repo, base) == [("feature.py", "noqa")]


class TestHeaderPathsAreStable:
    @pytest.mark.parametrize("option", ["diff.mnemonicPrefix", "diff.noprefix"])
    def test_a_user_prefix_option_does_not_change_the_path_read(self, tmp_path: Path, option: str) -> None:
        repo = make_repo(tmp_path, {"a/mod.py": "x = 1\n", "b/other.py": "y = 1\n", "top.py": "z = 1\n"})
        git(repo, "config", option, "true")
        base = git(repo, "rev-parse", "HEAD")
        for name in ("a/mod.py", "b/other.py", "top.py"):
            (repo / name).write_text("v = 0  # noqa\n", encoding="utf-8")
        assert sorted(item.path for item in gates.changed_files(repo, base)) == ["a/mod.py", "b/other.py", "top.py"]
        assert sorted(findings_of(repo, base)) == [
            ("a/mod.py", "noqa"),
            ("b/other.py", "noqa"),
            ("top.py", "noqa"),
        ]

    @pytest.mark.parametrize("name", ['we"ird.py', "tab\there.py", "new\nline.py", "uni\u00e9.py", "ctl\x01x.py"])
    def test_a_path_git_quotes_is_read_under_its_real_name(self, tmp_path: Path, name: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / name).write_text("a = 1  # noqa\n", encoding="utf-8")
        assert findings_of(repo, base) == [(name, "noqa")]
        (repo / "app.py").unlink()
        removed = {item.path: item.status for item in gates.changed_files(repo, base)}
        assert removed == {"app.py": "deleted", name: "added"}

    def test_a_renamed_quoted_path_is_read_under_its_new_name(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {'we"ird.py': "x = 1\n" + "pad = 0\n" * 20})
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "mv", 'we"ird.py', "tab\there.py")
        changes = gates.changed_files(repo, base)
        assert [(item.path, item.old_path, item.status) for item in changes] == [
            ("tab\there.py", 'we"ird.py', "renamed")
        ]


class TestFinishJudgesPreviousAndCandidate:
    def test_rewriting_the_outputs_of_a_running_code_task_to_prose_and_finishing_is_judged(
        self, gated: "tuple[Path, Path]"
    ) -> None:
        project, repo = gated
        work(repo)
        previous = raw_state(project)
        candidate = copy.deepcopy(previous)
        candidate["tasks"][0]["outputs"] = [{"root": "target", "path": "README.md", "required": True}]
        candidate["tasks"][0]["status"] = "DONE"
        errors, _ = lib.transition_gate_errors(project, previous, candidate, "update")
        assert any("task T01: gate-not-run" in item for item in errors), errors

    def test_changing_the_effect_of_a_running_code_task_and_finishing_is_judged(
        self, gated: "tuple[Path, Path]"
    ) -> None:
        project, _ = gated
        previous = raw_state(project)
        candidate = copy.deepcopy(previous)
        candidate["tasks"][0]["effect"] = {"kind": "none", "description": None}
        candidate["tasks"][0]["status"] = "DONE"
        errors, _ = lib.transition_gate_errors(project, previous, candidate, "update")
        assert any("task T01: gate-not-run" in item for item in errors), errors

    @pytest.mark.parametrize(
        "change",
        [
            {"outputs": [{"root": "target", "path": "README.md", "required": True}]},
            {"effect": {"kind": "none", "description": None}},
        ],
    )
    def test_update_refuses_a_change_of_outputs_or_effect_on_a_running_task(
        self, gated: "tuple[Path, Path]", change: "dict[str, Any]"
    ) -> None:
        project, _ = gated
        before = snapshot_files(project)
        text = refused_text(
            lambda: update_project_data(
                project, {"tasks": [{"id": "T01", **change}]}, expected_revision=revision_of(project)
            )
        )
        assert "task T01" in text and next(iter(change)) in text and "cannot change" in text, text
        assert snapshot_files(project) == before

    def test_the_same_change_on_a_todo_task_is_allowed(self, gated: "tuple[Path, Path]") -> None:
        project, _ = gated
        update_project_data(
            project,
            {"tasks": [{"id": "T02", "outputs": [{"root": "target", "path": "other.py", "required": True}]}]},
            expected_revision=revision_of(project),
        )
        assert raw_state(project)["tasks"][1]["outputs"][0]["path"] == "other.py"


SKIP_BYPASSES = [
    ("# ruff: noqa\nx = 1\n", "ruff: noqa"),
    ("# flake8: noqa\nx = 1\n", "flake8: noqa"),
    ("# mypy: ignore-errors\nx = 1\n", "mypy: ignore-errors"),
    ("# pyright: basic\nx = 1\n", "pyright: basic"),
    ('import pytest\n\n\n@pytest.mark.skipif(True, reason="x")\ndef test_a():\n    pass\n', "skipif"),
    ('import pytest\n\n\n@pytest.mark.skipif(1, reason="x")\ndef test_a():\n    pass\n', "skipif"),
    ('import unittest\n\n\n@unittest.skipIf(True, "x")\ndef test_a():\n    pass\n', "skipif"),
    ('import unittest\n\n\n@unittest.skipUnless(False, "x")\ndef test_a():\n    pass\n', "skipif"),
    ("from pytest import mark\n\n\n@mark.skip\ndef test_a():\n    pass\n", "skip"),
    ("import pytest as pt\n\n\n@pt.mark.skip\ndef test_a():\n    pass\n", "skip"),
    ("from pytest import mark as m\n\n\n@m.skip\ndef test_a():\n    pass\n", "skip"),
    ("from unittest import skip\n\n\n@skip('x')\ndef test_a():\n    pass\n", "skip"),
    ("from pytest import xfail\n\n\ndef test_a():\n    xfail('x')\n", "xfail"),
]


class TestScanGaps:
    @pytest.mark.parametrize(("text", "fragment"), SKIP_BYPASSES)
    def test_each_gap_is_a_finding(self, tmp_path: Path, text: str, fragment: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text(text, encoding="utf-8")
        found = scan(repo, base)[0]
        assert found and all(item["code"] == "bypass-added" for item in found), (found, scan(repo, base)[1])
        assert any(fragment in item["detail"] for item in found), found

    def test_a_skipif_with_a_real_condition_and_a_reason_is_still_only_information(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        text = (
            'import pytest\n\n\n@pytest.mark.skipif(sys.platform == "win32", reason="posix")\ndef test_a():\n    pass\n'
        )
        (repo / "feature.py").write_text(text, encoding="utf-8")
        findings, info = scan(repo, base)
        assert findings == [] and [item["code"] for item in info] == ["conditional-skip"]

    @pytest.mark.parametrize(
        ("name", "before", "after"),
        [
            ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q --ignore=tests/slow\n"),
            ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q --deselect x\n"),
            ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q -p no:cov\n"),
            ("pytest.ini", "[pytest]\ntestpaths = tests\n    more\n", "[pytest]\ntestpaths = tests\n"),
            ("tox.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q --ignore=tests/slow\n"),
            ("tox.ini", "[pytest]\ntestpaths = tests\n    more\n", "[pytest]\ntestpaths = tests\n"),
            (
                "pyproject.toml",
                '[tool.coverage.report]\nexclude_lines = ["pragma: no cover"]\n',
                '[tool.coverage.report]\nexclude_lines = ["pragma: no cover", "if x:"]\n',
            ),
            (
                "pyproject.toml",
                "[tool.coverage.report]\nfail_under = 90\n",
                '[tool.coverage.report]\nfail_under = 90\nexclude_also = ["def f"]\n',
            ),
            (
                ".coveragerc",
                "[report]\nexclude_lines =\n    pragma: no cover\n",
                "[report]\nexclude_lines =\n    pragma: no cover\n    if x:\n",
            ),
            (
                "pyproject.toml",
                '[[tool.ty.overrides]]\ninclude = ["a/**"]\n',
                '[[tool.ty.overrides]]\ninclude = ["a/**"]\nexclude = ["tests/**"]\n',
            ),
            (
                "pyproject.toml",
                '[[tool.ruff.lint.x]]\nexclude = ["a"]\n',
                '[[tool.ruff.lint.x]]\nexclude = ["a", "b"]\n',
            ),
        ],
    )
    def test_each_config_gap_is_floor_lowered(self, tmp_path: Path, name: str, before: str, after: str) -> None:
        assert config_findings(tmp_path, before, after, name), (name, after)

    def test_an_unchanged_array_of_tables_is_not_a_finding(self, tmp_path: Path) -> None:
        text = '[[tool.ty.overrides]]\nexclude = ["tests/**"]\n'
        assert config_findings(tmp_path, text, text + "\n# comment\n") == []

    def test_a_junit_count_that_includes_skipped_cases_is_not_a_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        xml = (
            '<testsuites><testsuite tests="2" skipped="2">'
            '<testcase classname="t" name="a"><skipped/></testcase>'
            '<testcase classname="t" name="b"><skipped/></testcase></testsuite></testsuites>'
        )
        writer = (
            "import sys, pathlib; "
            f"[(pathlib.Path(a.split('=', 1)[1]).write_text({xml!r}) if a.startswith('--junitxml=') else None) "
            "for a in sys.argv]"
        )
        skipped = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-c", writer, "pytest"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), skipped])
        start(project)
        result = run_gates(project)
        assert result["passed"] is False
        assert any("ran 0 test cases" in item and "2 skipped" in item for item in result["failures"]), result

    def test_no_changed_executable_lines_is_reported_in_the_scan_information(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        (repo / "tools.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = run_gates(project)
        codes = [item["code"] for item in result["scan"]["info"]]
        assert "no-executable-lines" in codes, result["scan"]

    def test_changed_executable_lines_are_not_reported_as_missing(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        assert "no-executable-lines" not in [item["code"] for item in run_gates(project)["scan"]["info"]]


class TestComplexityIgnoresNoqa:
    def test_a_noqa_on_the_def_line_does_not_hide_a_worse_function(self, tmp_path: Path) -> None:
        text = big_function("legacy", 12).replace("def legacy(x):", "def legacy(x):  # noqa: C901")
        repo = make_repo(tmp_path, {"src/legacy.py": text})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "src/legacy.py").write_text(text.replace("return -1", "return -2"), encoding="utf-8")
        result = gates.touched_complexity(repo, gates.changed_files(repo, base))
        assert [(item["function"], item["complexity"]) for item in result["over_limit"]] == [("legacy", 13)]


class TestSnapshotScope:
    @pytest.fixture
    def decoy(self, tmp_path: Path) -> Path:
        return make_repo(tmp_path / "decoy", {"other.py": "q = 1\n"})  # its commits are not the real repository's

    @pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"])
    def test_git_variables_of_the_caller_are_not_inherited(
        self, tmp_path: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("x = 2  # noqa\n", encoding="utf-8")
        values = {
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(decoy),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        }
        monkeypatch.setenv(variable, values[variable])
        assert Path(values["GIT_DIR"]).is_dir() and (decoy / ".git" / "index").is_file()
        assert [item.path for item in gates.changed_files(repo, base)] == ["app.py"]
        assert len(tree(repo, base)) == 64
        assert gates.current_head(repo) == base

    def test_the_control_without_the_variable_reads_the_same_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("x = 2  # noqa\n", encoding="utf-8")
        assert [item.path for item in gates.changed_files(repo, base)] == ["app.py"]

    @pytest.mark.parametrize("committed", [True, False])
    def test_an_ignored_but_force_added_file_is_part_of_the_change(self, tmp_path: Path, committed: bool) -> None:
        repo = make_repo(tmp_path, {".gitignore": "gen/\n", "app.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        clean = tree(repo, base)
        (repo / "gen").mkdir()
        (repo / "gen" / "mod.py").write_text("y = 1  # noqa\n", encoding="utf-8")
        assert [item.path for item in gates.changed_files(repo, base)] == []
        git(repo, "add", "-f", "gen/mod.py")
        if committed:
            git(repo, "commit", "-q", "-m", "force")
        assert findings_of(repo, base) == [("gen/mod.py", "noqa")]
        assert tree(repo, base) != clean

    def test_a_force_added_file_removed_again_leaves_no_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {".gitignore": "gen/\n", "app.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "gen").mkdir()
        (repo / "gen" / "mod.py").write_text("y = 1\n", encoding="utf-8")
        git(repo, "add", "-f", "gen/mod.py")
        git(repo, "commit", "-q", "-m", "force")
        git(repo, "rm", "-q", "-f", "gen/mod.py")
        assert gates.changed_files(repo, base) == []

    def test_a_target_inside_a_repository_is_refused_at_approval(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        set_worktrees(project, [worktree_entry(repo, path=str(repo / "tests"))], repo / "tests")
        with pytest.raises(WorkspaceError, match=r"gates-subdirectory: .*tests.*repository root"):
            approve(project, FULL_SET)

    def test_a_target_inside_a_repository_is_refused_at_gate_run_and_finish(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        set_worktrees(project, [worktree_entry(repo, path=str(repo / "tests"))], repo / "tests")
        with pytest.raises(WorkspaceError, match="gates-subdirectory"):
            run_gates(project)
        assert any(
            "gates-subdirectory" in item
            for item in gates.gate_findings(
                project, raw_state(project), {**raw_state(project)["tasks"][0], "status": "DONE"}
            )
        )

    def test_a_snapshot_that_cannot_be_created_is_a_named_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno

        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")

        def full(*_args: Any, **_kwargs: Any) -> str:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(gates.tempfile, "mkdtemp", full)
        with pytest.raises(WorkspaceError, match=r"gates-snapshot: .*No space left"):
            gates.changed_files(repo, base)
        with pytest.raises(WorkspaceError, match="gates-snapshot"):
            tree(repo, base)

    def test_a_snapshot_directory_that_cannot_be_written_is_a_named_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        real = Path.mkdir

        def failing(self: Path, *args: Any, **kwargs: Any) -> None:
            if self.name == "objects" and self.parent.name.startswith("gates-snapshot-"):
                raise OSError(28, "No space left on device")
            real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing)
        with pytest.raises(WorkspaceError, match=r"gates-snapshot: .*could not be written"):
            gates.changed_files(repo, base)

    def test_a_directory_that_is_not_a_repository_has_no_toplevel_problem_to_report(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert gates._toplevel_problem(plain) == ""
        assert gates._toplevel_problem(make_repo(tmp_path)) == ""


class TestConstantCondition:
    @pytest.mark.parametrize(
        ("call", "always"),
        [
            ('@pytest.mark.skipif(True, reason="x")', True),
            ('@pytest.mark.skipif(condition=True, reason="x")', True),
            ('@pytest.mark.skipif(1 == 1, reason="x")', True),
            ('@pytest.mark.skipif(not False, reason="x")', True),
            ("@pytest.mark.skipif(True", True),
            ('@unittest.skipUnless(False, "x")', True),
            ('@unittest.skipUnless(True, "x")', False),
            ('@pytest.mark.skipif(False, reason="x")', False),
            ('@pytest.mark.skipif(None, reason="x")', False),
            ('@pytest.mark.skipif("a, b", reason="x")', False),
            ('@pytest.mark.skipif(sys.platform == "win32", reason="x")', False),
            ('@pytest.mark.skipif(f(1, 2), reason="x")', False),
            ('@pytest.mark.skipif([1, 2] and True, reason="x")', False),
            ('@pytest.mark.skipif(1 +, reason="x")', False),
            ("x = pytest.mark.skipif", False),
        ],
    )
    def test_only_a_condition_of_constants_that_always_skips_counts(self, call: str, always: bool) -> None:
        assert gates._constant_condition("pytest.mark.skipif", call) is always


# --- corrections after the diff re-audit (round 18) -----------------------------------------------------------

BIG = 9 * 1024 * 1024  # over the 8 MiB cap, small enough for a test


def spy_on_git(monkeypatch: pytest.MonkeyPatch) -> "list[tuple[list[str], int]]":
    """Every Git call the runner makes, with the size of what it printed."""
    calls: "list[tuple[list[str], int]]" = []
    real = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(argv, *args, **kwargs)
        calls.append(([str(item) for item in argv], len(done.stdout)))
        return done

    monkeypatch.setattr(gates.subprocess, "run", spy)
    return calls


def binary_repo(tmp_path: Path) -> "tuple[Path, str]":
    repo = make_repo(tmp_path)
    (repo / "old.bin").write_bytes(os.urandom(BIG))
    (repo / "mod.bin").write_bytes(os.urandom(BIG))
    commit_all(repo)
    return repo, git(repo, "rev-parse", "HEAD")


class TestBinaryFilesAreNotDiffed:
    def test_a_large_added_modified_or_deleted_binary_is_never_read_as_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base = binary_repo(tmp_path)
        (repo / "old.bin").unlink()
        (repo / "mod.bin").write_bytes(os.urandom(BIG))
        (repo / "new.bin").write_bytes(os.urandom(BIG))
        (repo / "feature.py").write_text("a = 1  # noqa\n", encoding="utf-8")
        calls = spy_on_git(monkeypatch)
        changes = {item.path: item for item in gates.changed_files(repo, base)}
        assert len(tree(repo, base)) == 64
        assert max(size for _, size in calls) < 1_000_000, [(argv[-6:], size) for argv, size in calls if size > 1e6]
        assert {name: item.status for name, item in changes.items()} == {
            "feature.py": "added",
            "mod.bin": "modified",
            "new.bin": "added",
            "old.bin": "deleted",
        }
        assert all(not changes[name].added and not changes[name].binary for name in ("mod.bin", "new.bin", "old.bin"))
        assert sorted(changes["feature.py"].added) == [1]  # the rest of the change is still read

    def test_a_binary_that_is_not_python_is_not_a_finding_and_a_binary_py_still_is(self, tmp_path: Path) -> None:
        repo, base = binary_repo(tmp_path)
        (repo / "new.bin").write_bytes(os.urandom(BIG))
        (repo / "model.py").write_bytes(b"a = 1\n\x00\x01\n")
        assert findings_of(repo, base) == [("model.py", "python file with a NUL byte: it cannot be scanned")]

    def test_the_exclusion_names_the_path_literally(self, tmp_path: Path) -> None:
        repo, base = binary_repo(tmp_path)
        (repo / "big[1].bin").write_bytes(os.urandom(BIG))
        (repo / "big1.bin").write_text("addopts = --no-cov\n", encoding="utf-8")  # a glob would hide this one
        assert findings_of(repo, base) == [("big1.bin", "--no-cov")]

    def test_a_python_or_config_file_over_the_cap_is_a_finding_other_files_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gates, "MAX_DIFF_BYTES", 1000, raising=False)
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text("x = 1  # noqa\n" * 100, encoding="utf-8")
        (repo / "setup.cfg").write_text("[x]\n" + "a = 1\n" * 300, encoding="utf-8")
        (repo / "tools.sh").write_text("pytest --no-cov\n" * 100, encoding="utf-8")
        assert sorted(findings_of(repo, base)) == [
            ("feature.py", "file over 1000 bytes: it was not read, so it cannot be scanned"),
            ("setup.cfg", "file over 1000 bytes: it was not read, so it cannot be scanned"),
        ]

    def test_the_evidence_hash_follows_the_content_of_a_binary_it_does_not_read(self, tmp_path: Path) -> None:
        repo, base = binary_repo(tmp_path)
        clean = tree(repo, base)
        (repo / "mod.bin").write_bytes(os.urandom(BIG))
        changed_once = tree(repo, base)
        assert changed_once != clean and tree(repo, base) == changed_once
        (repo / "mod.bin").write_bytes(os.urandom(BIG))
        assert tree(repo, base) not in (clean, changed_once)
        (repo / "mod.bin").chmod(0o755)
        assert tree(repo, base) != changed_once


class TestGitlinksAreNeverRead:
    def test_a_nested_repository_next_to_a_changed_file_does_not_crash_and_loses_nothing(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        nested = repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        git(nested, "init", "-q", "-b", "main")
        (nested / "mod.py").write_text("y = 1\n", encoding="utf-8")
        git(nested, "add", "-A")
        git(nested, "commit", "-q", "-m", "inner")
        (repo / "feature.py").write_text("a = 1  # noqa\n", encoding="utf-8")
        changes = {item.path: item for item in gates.changed_files(repo, base)}
        assert sorted(changes) == ["feature.py", "vendor/lib"]
        assert changes["vendor/lib"].added == {} and not changes["vendor/lib"].binary
        assert sorted(changes["feature.py"].added) == [1]
        assert findings_of(repo, base) == [("feature.py", "noqa")]

    def test_a_real_submodule_is_one_changed_path_and_other_files_are_scanned(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path / "source", {"lib.py": "z = 1\n"})
        repo = make_repo(tmp_path / "target")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
        (repo / "feature.py").write_text("a = 1  # noqa\n", encoding="utf-8")
        changes = {item.path: item for item in gates.changed_files(repo, base)}
        assert sorted(changes) == [".gitmodules", "feature.py", "sub"]
        assert findings_of(repo, base) == [("feature.py", "noqa")]

    def test_a_gitlink_is_not_sized_even_when_its_commit_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[bytes] = []

        def fake(repo: Path, *arguments: str, **keywords: Any) -> bytes:
            asked.append(keywords["stdin"])
            return b"".join(f"{line} missing\n".encode() for line in keywords["stdin"].decode().split())

        monkeypatch.setattr(gates, "_git_bytes", fake)
        link = gates.RawChange("added", "0" * 40, "1" * 40, "sub", "000000", "160000")
        assert gates._oversized(Path("."), [link], {}) == []
        assert asked == []  # nothing was asked about the commit of a gitlink

    def test_a_missing_size_counts_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gates, "_git_bytes", lambda *a, **k: b"missing\n")
        item = gates.RawChange("added", "0" * 40, "2" * 40, "odd", "000000", "100644")
        assert gates._oversized(Path("."), [item], {}) == []

    def test_the_null_object_id_follows_the_length_of_the_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert gates._null_oid("a" * 40) == "0" * 40
        assert gates._null_oid("b" * 64) == "0" * 64
        asked: list[bytes] = []

        def fake(repo: Path, *arguments: str, **keywords: Any) -> bytes:
            asked.append(keywords["stdin"])
            return b"100\n"

        monkeypatch.setattr(gates, "_git_bytes", fake)
        item = gates.RawChange("added", "0" * 64, "c" * 64, "f.py", "000000", "100644")
        assert gates._oversized(Path("."), [item], {}) == []
        assert asked == [b"c" * 64 + b"\n"]  # the 64-zero id of a sha256 repository is not looked up


class TestCallerGitVariables:
    def test_config_variables_of_the_caller_reach_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_repo(tmp_path)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "t09.key")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "from-the-caller")
        assert gates._git(repo, "config", "--get", "t09.key") == "from-the-caller"

    def test_a_global_config_file_named_by_the_caller_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        named = tmp_path / "global.cfg"
        named.write_text("[t09]\n\tkey = from-a-file\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(named))
        assert gates._git(repo, "config", "--get", "t09.key") == "from-a-file"

    def test_a_safe_directory_the_caller_supplies_is_still_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repo))
        assert gates._git(repo, "config", "--get-all", "safe.directory") == str(repo)

    def test_the_variables_that_choose_a_repository_are_still_dropped_with_config_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        decoy = make_repo(tmp_path / "decoy", {"other.py": "q = 1\n"})
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_COMMON_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "t09.key")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")
        assert gates._git(repo, "rev-parse", "--show-toplevel") == str(repo.resolve())
        assert gates._git(repo, "config", "--get", "t09.key") == "kept"

    def test_the_prefix_settings_a_caller_supplies_cannot_change_the_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("x = 2  # noqa\n", encoding="utf-8")
        for number, (key, value) in enumerate([("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true")]):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{number}", key)
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{number}", value)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        assert findings_of(repo, base) == [("app.py", "noqa")]


class TestUserConfigDoesNotShapeTheDiff:
    def test_inter_hunk_context_in_the_users_gitconfig_does_not_shift_line_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[diff]\n\tinterHunkContext = 3\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        lines = [f"l{number} = {number}\n" for number in range(1, 9)]
        repo = make_repo(tmp_path, {"feature.py": "".join(lines)})
        base = git(repo, "rev-parse", "HEAD")
        edited = list(lines)
        edited[1] = "l2 = 20\n"
        edited[4] = "l5 = 50  # noqa\n"
        (repo / "feature.py").write_text("".join(edited), encoding="utf-8")
        assert gates._git(repo, "config", "--get", "diff.interHunkContext") == "3"  # the user's value is in force
        changes = gates.changed_files(repo, base)
        assert [sorted(item.added) for item in changes] == [[2, 5]]
        assert [(item["file"], item["line"]) for item in scan(repo, base)[0]] == [("feature.py", 5)]

    def test_a_global_excludes_file_does_hide_a_file_from_the_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shown, not pinned: a global ignore file acts like a .gitignore (documented in the limits)."""
        excludes = tmp_path / "ignore"
        excludes.write_text("secret_*.py\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(excludes))
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "secret_x.py").write_text("x = 1  # noqa\n", encoding="utf-8")
        (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
        assert [item.path for item in gates.changed_files(repo, base)] == ["plain.py"]


class TestCastIsTypingCast:
    @pytest.mark.parametrize(
        "line",
        [
            'df = df.withColumn("a", F.col("a").cast("int"))',
            'query = "SELECT CAST(a AS INT) FROM t"',
            "value = np.cast(x)",
            "broadcast(x)",
        ],
    )
    def test_other_casts_are_not_findings(self, tmp_path: Path, line: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text(f"a = 1\n{line}\n", encoding="utf-8")
        assert scan(repo, base) == ([], [])

    @pytest.mark.parametrize(
        "text",
        [
            "import typing\ny = typing.cast(int, x)\n",
            "from typing import cast\ny = cast(int, x)\n",
            "from typing import Any, cast\ny = cast(int, x)\n",
            "from typing import (\n    Any,\n    cast,\n)\ny = cast(int, x)\n",
            "from typing import cast as force\ny = force(int, x)\n",
            "import typing as t\ny = t.cast(int, x)\n",
            "from typing_extensions import cast\ny = cast(int, x)\n",
        ],
    )
    def test_typing_cast_in_every_spelling_is_a_finding_on_the_call_line(self, tmp_path: Path, text: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text(text, encoding="utf-8")
        findings, _ = scan(repo, base)
        assert [item["detail"] for item in findings] == ["cast"], findings
        assert findings[0]["line"] == len(text.splitlines())

    def test_a_cast_function_of_the_file_itself_is_not_typing_cast(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text("def cast(x):\n    return x\n\n\ny = cast(1)\n", encoding="utf-8")
        assert scan(repo, base) == ([], [])


class TestPragmaOnGuards:
    @pytest.mark.parametrize(
        "line",
        [
            'if __name__ == "__main__":  # pragma: no cover',
            "if __name__ == '__main__':  # pragma: no cover",
            "if TYPE_CHECKING:  # pragma: no cover",
            "if typing.TYPE_CHECKING:  # pragma: no cover",
        ],
    )
    def test_a_pragma_on_a_main_or_type_checking_guard_is_information(self, tmp_path: Path, line: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text(f"a = 1\n{line}\n    pass\n", encoding="utf-8")
        findings, info = scan(repo, base)
        assert findings == []
        assert [(item["code"], item["line"]) for item in info] == [("excluded-guard", 2)]

    @pytest.mark.parametrize(
        "line",
        [
            "def f():  # pragma: no cover",
            "if x:  # pragma: no cover",
            "if __name__ == '__main__' and x:  # pragma: no cover",
            "y = 1  # pragma: no cover",
        ],
    )
    def test_a_pragma_anywhere_else_stays_a_finding(self, tmp_path: Path, line: str) -> None:
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        (repo / "feature.py").write_text(f"a = 1\n{line}\n    pass\n", encoding="utf-8")
        assert [item["detail"] for item in scan(repo, base)[0]] == ["pragma: no cover"]


class TestSubdirectoryCheck:
    def test_a_path_that_differs_from_the_disk_only_in_case_is_the_same_directory(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        other_case = repo.parent / repo.name.upper()
        if not other_case.exists():
            pytest.skip("case-sensitive filesystem: the upper-case spelling is another path")
        assert gates._toplevel_problem(other_case) == ""
        assert gates.current_head(other_case) == git(repo, "rev-parse", "HEAD")

    def test_a_symlink_to_the_root_and_a_trailing_slash_are_the_same_directory(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(repo)
        assert gates._toplevel_problem(link) == ""
        assert gates._toplevel_problem(Path(str(repo) + "/")) == ""

    def test_when_the_directories_cannot_be_compared_the_resolved_paths_decide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreadable(*_args: Any) -> bool:
            raise OSError("cannot stat")

        monkeypatch.setattr(gates.os.path, "samefile", unreadable)
        repo = make_repo(tmp_path, GATE_FILES)
        link = tmp_path / "link"
        link.symlink_to(repo)
        assert gates._toplevel_problem(link) == ""
        assert "gates-subdirectory" in gates._toplevel_problem(repo / "tests")

    def test_a_symlink_to_a_subdirectory_is_still_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        link = tmp_path / "link"
        link.symlink_to(repo / "tests")
        assert "gates-subdirectory" in gates._toplevel_problem(link)


class TestForcedAddIsLiteralAndPrivate:
    @pytest.mark.parametrize(("name", "decoy"), [("a[1].py", "a1.py"), ("*.py", "other.py"), ("q?.py", "qx.py")])
    def test_a_name_with_glob_characters_adds_only_itself(self, tmp_path: Path, name: str, decoy: str) -> None:
        repo = make_repo(tmp_path, {".gitignore": "gen/\n", "app.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "gen").mkdir()
        (repo / "gen" / name).write_text("y = 1  # noqa\n", encoding="utf-8")
        (repo / "gen" / decoy).write_text("z = 1  # noqa\n", encoding="utf-8")  # ignored and never added
        git(repo, "add", "-f", "--", f":(literal)gen/{name}")
        assert [item.path for item in gates.changed_files(repo, base)] == [f"gen/{name}"]

    def test_the_real_index_and_object_store_are_unchanged_by_a_run_over_forced_and_untracked_files(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path, {".gitignore": "gen/\n", "app.py": "x = 1\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "gen").mkdir()
        (repo / "gen" / "mod.py").write_text("y = 1\n", encoding="utf-8")
        git(repo, "add", "-f", "gen/mod.py")
        git(repo, "commit", "-q", "-m", "force")
        (repo / "gen" / "mod.py").write_text("y = 2\n", encoding="utf-8")
        (repo / "fresh.py").write_text("w = " + str(os.urandom(8).hex()) + "\n", encoding="utf-8")
        before = (git(repo, "count-objects", "-v"), (repo / ".git" / "index").read_bytes())
        assert [item.path for item in gates.changed_files(repo, base)] == ["fresh.py", "gen/mod.py"]
        assert len(tree(repo, base)) == 64
        after = (git(repo, "count-objects", "-v"), (repo / ".git" / "index").read_bytes())
        assert after == before


README = [{"root": "target", "path": "README.md", "required": True}]


def patch_task(project: Path, **changes: Any) -> None:
    update_project_data(project, {"tasks": [{"id": "T01", **changes}]}, expected_revision=revision_of(project))


@pytest.fixture
def stamped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "tuple[Path, Path]":
    """Like `gated`, in a project that stamps `started_at` on a first start (a project made after the gates)."""
    repo = make_repo(tmp_path, GATE_FILES)
    project = make_project(tmp_path, repo)
    approve(project, FULL_SET)
    monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")  # after planning, as test_task_stamps
    start(project)
    assert "started_at" in raw_state(project)["tasks"][0]
    return project, repo


class TestOutputsLockFollowsTheFirstStart:
    def test_a_task_sent_back_to_todo_keeps_the_lock(self, stamped: "tuple[Path, Path]") -> None:
        project, _ = stamped
        patch_task(project, status="TODO")
        assert raw_state(project)["tasks"][0]["status"] == "TODO"
        before = snapshot_files(project)
        for change in ({"outputs": README}, {"effect": {"kind": "none", "description": None}}):
            text = refused_text(lambda: patch_task(project, **change))  # noqa: B023
            assert "task T01" in text and next(iter(change)) in text, text
        assert snapshot_files(project) == before

    def test_the_error_names_the_way_out(self, stamped: "tuple[Path, Path]") -> None:
        project, _ = stamped
        text = refused_text(lambda: patch_task(project, outputs=README))
        assert "skip the task and add a new one" in text, text

    def test_a_blocked_task_that_never_ran_may_be_replanned_and_one_that_did_may_not(
        self, stamped: "tuple[Path, Path]"
    ) -> None:
        project, _ = stamped
        update_project_data(
            project,
            {"tasks": [{"id": "T02", "status": "BLOCKED", "block_reason": "waiting"}]},
            expected_revision=revision_of(project),
        )
        assert "started_at" not in raw_state(project)["tasks"][1]
        update_project_data(
            project,
            {"tasks": [{"id": "T02", "outputs": [{"root": "target", "path": "other.py", "required": True}]}]},
            expected_revision=revision_of(project),
        )
        patch_task(project, status="BLOCKED", block_reason="waiting")
        assert "cannot change" in refused_text(lambda: patch_task(project, outputs=README))

    def test_without_start_stamps_a_blocked_task_is_treated_as_started(self, gated: "tuple[Path, Path]") -> None:
        project, _ = gated
        assert "started_at" not in raw_state(project)["tasks"][0]
        patch_task(project, status="BLOCKED", block_reason="waiting")
        assert "cannot change" in refused_text(lambda: patch_task(project, outputs=README))

    def test_a_skipped_code_task_is_not_refused_and_the_next_task_can_be_planned(
        self, stamped: "tuple[Path, Path]"
    ) -> None:
        project, _ = stamped
        patch_task(project, status="SKIPPED", skip_reason="replaced by a new task")
        assert raw_state(project)["tasks"][0]["status"] == "SKIPPED"
        update_project_data(
            project, {"tasks": [{**task_plan("T03"), "status": "TODO"}]}, expected_revision=revision_of(project)
        )
        assert [item["id"] for item in raw_state(project)["tasks"]] == ["T01", "T02", "T03"]

    def test_a_project_without_gates_is_not_locked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = make_project(tmp_path)
        assert "gates" not in raw_state(project)
        monkeypatch.setattr(lib, "GATES_ENFORCED_FROM", "2020-01-01T00:00:00+00:00")
        start(project)
        assert "started_at" in raw_state(project)["tasks"][0]
        patch_task(project, outputs=README)
        assert raw_state(project)["tasks"][0]["outputs"] == README


class TestGateRunRefusesATaskThatIsNotACodeTask:
    def test_a_running_prose_task_is_refused_with_a_named_error_and_the_way_out(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        approve(project, FULL_SET)
        start(project, "T02")
        before = snapshot_files(project)
        text = refused_text(lambda: run_gates(project, "T02"))
        assert text.startswith("gates-not-code-task: ") and "T02" in text, text
        assert "skip the task and add a new one" in text, text
        assert snapshot_files(project) == before

    def test_a_code_task_is_still_run(self, gated: "tuple[Path, Path]") -> None:
        project, repo = gated
        work(repo)
        assert run_gates(project)["passed"] is True


class TestNoExecutableLinesIsNotClaimedWithoutAMeasurement:
    def test_a_failed_tests_command_leaves_no_such_claim(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        failing = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-c", "raise SystemExit(3)", "pytest"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), failing])
        start(project)
        (repo / "tools.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = run_gates(project)
        assert result["passed"] is False
        assert "no-executable-lines" not in [item["code"] for item in result["scan"]["info"]]

    def test_an_unreadable_coverage_report_leaves_no_such_claim(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, GATE_FILES)
        project = make_project(tmp_path, repo)
        silent = {"id": "tests", "kind": "tests", "argv": [sys.executable, "-c", "pass", "pytest"]}
        approve(project, [ok_command("lint", "lint"), ok_command("types", "types"), silent])
        start(project)
        (repo / "tools.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = run_gates(project)
        assert any("coverage-format" in item for item in result["failures"]), result["failures"]
        assert "no-executable-lines" not in [item["code"] for item in result["scan"]["info"]]
