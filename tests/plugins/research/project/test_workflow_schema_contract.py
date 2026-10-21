"""The printed `required_by` entry is the request the runtime accepts, checked by driving it.

`test_workflow_schema.py` compares the printed schema with the table that produced it. That check
shares the defect's mechanism: both sides could drop the same key and stay green. This module builds
each request from the printed schema alone and calls `workflow()`, so an under-reported key fails
here as a refusal rather than as a caller's staged attempt. It then removes one printed key at a time
and requires the refusal to name that key.

The worktree and confirm fixtures come from the modules that already own them, so the test drives the
same git and document paths the lifecycle uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from workspace_lib import WorkspaceError, document_sha256
from workspace_workflows import input_schema, workflow

from tests.plugins.research.project.test_delivery import FILES, write_versions
from tests.plugins.research.project.test_gate_new_projects import PLANNING
from tests.plugins.research.project.test_workflow_automation import fill_spec
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    git,
    load_state,
    make_project,
    make_repository,
)

URL = "https://github.com/o/r/pull/7"
EVIDENCE = "https://github.com/o/r/pull/7 (squash commit abc123 on main)"
BRANCH = "npham/2026-01-01-001-x"

WORKTREE_ORDER = (
    "operation=record",
    "operation=pull_request",
    "operation=alpha",
    "operation=release",
    "operation=merge",
    "operation=close",
)
CONFIRM_KINDS = ("kind=requirements", "kind=planning", "kind=architecture", "kind=alignment")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


def schema_keys(action: str, variant: str) -> set[str]:
    """The complete required key set the tool prints for one discriminator value."""
    return set(input_schema(action)["required_by"][variant])


def worktree_request(
    project: Path, variant: str, values: dict[str, Any], identity: str | None = None
) -> dict[str, Any]:
    """A request whose keys are exactly the printed set, so a missing key cannot be hidden."""
    keys = schema_keys("worktree", variant)
    supplied: dict[str, Any] = {
        "id": identity or f"schema-{variant.replace('=', '-').replace('_', '-')}",
        "expected_revision": load_state(project)["revision"],
        "operation": variant.split("=", 1)[1],
        "confirmation": {"source": "schema contract", "response": "yes"},
        **values,
    }
    assert set(supplied) == keys, f"{variant}: schema prints {sorted(keys)}, request has {sorted(supplied)}"
    return supplied


def fake_origin(repository: Path) -> None:
    """An origin remote with a main ref, so the alpha occupancy check can read released versions."""
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def commit_all(worktree: Path, message: str) -> None:
    """Commit the version files, so a later `close` sees a clean worktree."""
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", message)


def confirm_request(
    project: Path, variant: str, values: dict[str, Any], tokens: dict[str, str], identity: str | None = None
) -> dict[str, Any]:
    keys = schema_keys("confirm", variant)
    supplied: dict[str, Any] = {
        "id": identity or f"schema-{variant.replace('=', '-').replace('_', '-')}",
        "expected_revision": load_state(project)["revision"],
        "kind": variant.split("=", 1)[1],
        "review_id": "A1",
        "response": "yes",
        "source": "user reply",
        "scope": "requirements and design",
        # `tokens` is the universal guard the reference documents, not a discriminator key: it maps
        # every existing document the operation changes, so it cannot be listed per operation.
        "tokens": tokens,
        **values,
    }
    assert set(supplied) - {"tokens"} == keys, (
        f"{variant}: schema prints {sorted(keys)}, request has {sorted(supplied)}"
    )
    return supplied


def worktree_values(linked: Path) -> dict[str, dict[str, Any]]:
    return {
        "operation=record": {"path": str(linked), "branch": BRANCH},
        "operation=pull_request": {"path": str(linked), "host": "github", "url": URL, "number": 7},
        "operation=alpha": {
            "path": str(linked), "base": "0.24.0", "alpha": 1, "reason": "first alpha", "files": FILES,
        },
        "operation=release": {"path": str(linked), "version": "0.24.0"},
        "operation=merge": {
            "path": str(linked), "merge_state": "merged", "method": "squash", "evidence": EVIDENCE,
        },
        "operation=close": {"path": str(linked), "decision": "keep"},
    }


def token(project: Path, name: str) -> str:
    return document_sha256((project / f"{name}.md").read_text(encoding="utf-8"))


class TestWorktreeContract:
    def test_every_operation_accepts_a_request_built_from_its_schema_entry(
        self, tmp_path: Path, repository: Path, linked: Path
    ) -> None:
        fake_origin(repository)
        project = make_project(tmp_path, repository)
        write_versions(linked, "0.24.0", 1)
        commit_all(linked, "alpha version files")
        values = worktree_values(linked)
        for variant in WORKTREE_ORDER:
            if variant == "operation=release":
                write_versions(linked, "0.24.0", None)
                commit_all(linked, "release version files")
            assert workflow(project, "worktree", worktree_request(project, variant, values[variant]))

    @pytest.mark.parametrize("variant", WORKTREE_ORDER)
    def test_omitting_any_printed_key_is_refused_and_names_it(
        self, tmp_path: Path, repository: Path, linked: Path, variant: str
    ) -> None:
        fake_origin(repository)
        project = make_project(tmp_path, repository)
        write_versions(linked, "0.24.0", 1)
        values = worktree_values(linked)
        if variant != "operation=record":
            workflow(project, "worktree", worktree_request(project, "operation=record", values["operation=record"]))
        if variant == "operation=merge":
            workflow(
                project, "worktree",
                worktree_request(project, "operation=pull_request", values["operation=pull_request"]),
            )
        full = worktree_request(project, variant, values[variant])
        for key in sorted(full):
            identity = f"omit-{variant.replace('=', '-').replace('_', '-')}-{key.replace('_', '-')}"
            without = worktree_request(project, variant, values[variant], identity=identity)
            del without[key]
            with pytest.raises(WorkspaceError) as caught:
                workflow(project, "worktree", without)
            assert key in str(caught.value), f"{variant} without {key}: {caught.value}"


class TestConfirmContract:
    def prepared(self, tmp_path: Path, kind: str) -> tuple[Path, dict[str, Any], dict[str, str]]:
        """A project with a spec and a draft planning document, plus token values and guard tokens."""
        target = tmp_path / "target"
        target.mkdir()
        project = make_project(tmp_path, target)
        fill_spec(project)
        (project / "planning.md").write_text(PLANNING, encoding="utf-8")
        tokens = {"spec": token(project, "spec"), "planning": token(project, "planning")}
        if kind == "alignment":
            values = {"requirements_sha256": tokens["spec"], "planning_sha256": tokens["planning"]}
        else:
            values = {"proposal_sha256": tokens["spec" if kind == "requirements" else "planning"]}
        return project, values, tokens

    @pytest.mark.parametrize("variant", CONFIRM_KINDS)
    def test_every_kind_accepts_a_request_built_from_its_schema_entry(self, tmp_path: Path, variant: str) -> None:
        kind = variant.split("=", 1)[1]
        project, values, tokens = self.prepared(tmp_path, kind)
        assert workflow(project, "confirm", confirm_request(project, variant, values, tokens))

    @pytest.mark.parametrize("variant", CONFIRM_KINDS)
    def test_omitting_any_printed_key_is_refused_and_names_it(self, tmp_path: Path, variant: str) -> None:
        kind = variant.split("=", 1)[1]
        project, values, tokens = self.prepared(tmp_path, kind)
        for key in sorted(schema_keys("confirm", variant)):
            identity = f"omit-{variant.replace('=', '-').replace('_', '-')}-{key.replace('_', '-')}"
            without = confirm_request(project, variant, values, tokens, identity=identity)
            del without[key]
            with pytest.raises(WorkspaceError) as caught:
                workflow(project, "confirm", without)
            assert key in str(caught.value), f"{variant} without {key}: {caught.value}"
