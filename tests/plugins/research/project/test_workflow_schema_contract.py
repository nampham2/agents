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

import itertools
from pathlib import Path
from typing import Any

import pytest
import workspace_lib
from workspace_delivery import delivery_errors
from workspace_lib import WorkspaceError, document_sha256
from workspace_session import update_project_data
from workspace_workflows import input_schema, workflow

from tests.plugins.research.project.test_delivery import FILES, fetched, write_versions
from tests.plugins.research.project.test_gate_new_projects import PLANNING
from tests.plugins.research.project.test_goldfish import CODE_TASK, close, open_round, report
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
PAST = "2000-01-01T00:00:00+00:00"
HEAD = "a" * 40
IDS = itertools.count(1)
# Printed for the caller and demanded only when a gate is on: with the gates off a request may omit them.
GATE_ONLY = {"merged_head"}

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
        "id": identity or f"schema-{next(IDS)}",
        "expected_revision": load_state(project)["revision"],
        "operation": variant.split("=", 1)[1].split(",")[0],
        "confirmation": {"source": "schema contract", "response": "yes"},
        **values,
    }
    assert set(supplied) == keys, f"{variant}: schema prints {sorted(keys)}, request has {sorted(supplied)}"
    return supplied


def short_id(prefix: str) -> str:
    return f"{prefix}-{next(IDS)}"


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
        "id": identity or f"schema-{next(IDS)}",
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
            "merged_head": HEAD,
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
            if key in GATE_ONLY:
                continue
            without = worktree_request(project, variant, values[variant], identity=short_id("omit"))
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
            without = confirm_request(project, variant, values, tokens, identity=short_id("omit"))
            del without[key]
            with pytest.raises(WorkspaceError) as caught:
                workflow(project, "confirm", without)
            assert key in str(caught.value), f"{variant} without {key}: {caught.value}"


class TestVariantsThatDependOnTheRecord:
    """The exempt variants, the release that replaces an exemption and the gated merge, built from the printed keys."""

    def test_the_two_exempt_variants_without_a_remote(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        values = {"path": str(linked)}
        workflow(project, "worktree", worktree_request(project, "operation=record", {**values, "branch": BRANCH}))
        pr = worktree_request(project, "operation=pull_request,exempt=no_remote", {**values, "exempt": "no_remote"})
        assert workflow(project, "worktree", pr)
        assert load_state(project)["worktrees"][0]["pr_exemption"]["reason"] == "no_remote"
        alpha = worktree_request(
            project, "operation=alpha,exempt=true", {**values, "exempt": True, "reason": "docs only"}
        )
        assert workflow(project, "worktree", alpha)
        assert load_state(project)["worktrees"][0]["version_exemption"]["reason"] == "docs only"

    @pytest.mark.parametrize(
        "variant, values",
        (
            ("operation=pull_request,exempt=no_remote", {"exempt": "no_remote"}),
            ("operation=pull_request,exempt=local_only", {"exempt": "local_only"}),
            ("operation=alpha,exempt=true", {"exempt": True, "reason": "docs only"}),
        ),
    )
    def test_an_exempt_variant_without_its_reason_or_exempt_key_is_refused(
        self, tmp_path: Path, repository: Path, linked: Path, variant: str, values: dict[str, Any]
    ) -> None:
        project = make_project(tmp_path, repository)
        workflow(
            project, "worktree",
            worktree_request(project, "operation=record", {"path": str(linked), "branch": BRANCH}),
        )
        full = worktree_request(project, variant, {"path": str(linked), **values})
        for key in sorted(set(values)):
            without = {k: v for k, v in full.items() if k != key}
            without["id"] = short_id("omit")
            with pytest.raises(WorkspaceError) as caught:
                workflow(project, "worktree", without)
            if key == "exempt":
                # Without `exempt` the request is a plain pull_request or alpha: the refusal names the exemption.
                assert "exempt" in str(caught.value), caught.value
            else:
                assert key in str(caught.value), caught.value

    def test_a_request_that_omits_exempt_is_told_about_it(self, tmp_path: Path, repository: Path, linked: Path) -> None:
        project = make_project(tmp_path, repository)
        workflow(
            project, "worktree",
            worktree_request(project, "operation=record", {"path": str(linked), "branch": BRANCH}),
        )
        bare = {
            "id": short_id("bare"), "expected_revision": load_state(project)["revision"],
            "operation": "pull_request", "path": str(linked),
            "confirmation": {"source": "schema contract", "response": "yes"},
        }
        with pytest.raises(WorkspaceError, match="exempt no_remote or local_only"):
            workflow(project, "worktree", bare)


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_lib, "GOLDFISH_FROM", PAST)


@pytest.fixture
def delivered(tmp_path: Path) -> tuple[Path, Path]:
    """A code project with a never-pushed local_only worktree: the diff gate on, no upstream check."""
    repository = make_repository(tmp_path)
    git(repository, "remote", "add", "origin", "https://github.com/o/r.git")
    fetched(repository)
    linked = add_worktree(repository)
    project = make_project(tmp_path, repository)
    update_project_data(project, {"tasks": [CODE_TASK]}, expected_revision=load_state(project)["revision"])
    values = {"path": str(linked)}
    workflow(project, "worktree", worktree_request(project, "operation=record", {**values, "branch": BRANCH}))
    exempt = worktree_request(project, "operation=pull_request,exempt=local_only", {**values, "exempt": "local_only"})
    workflow(project, "worktree", exempt)
    return project, linked


class TestGatedVariants:
    def test_the_release_and_merge_variants_with_the_gates_on(self, gated: None, delivered: tuple[Path, Path]) -> None:
        project, linked = delivered
        write_versions(linked, "0.24.0", None)
        commit_all(linked, "release 0.24.0")
        head = git(linked, "rev-parse", "HEAD")
        opened = open_round(project, "diff", ("combined",), worktree=str(linked))
        close(project, opened, reports=report(("combined",)))
        exempt = worktree_request(
            project, "operation=alpha,exempt=true",
            {"path": str(linked), "exempt": True, "reason": "docs-only project; no version change"},
        )
        assert workflow(project, "worktree", exempt)
        release = worktree_request(
            project, "operation=release,replaces_exemption=true",
            {"path": str(linked), "version": "0.24.0", "files": FILES, "replaces_exemption": True},
        )
        assert workflow(project, "worktree", release)
        values = {"path": str(linked), "merge_state": "merged", "method": "squash", "evidence": EVIDENCE}
        # The printed merge keys include merged_head; with the diff gate on, the request is refused without it.
        without = {
            key: value
            for key, value in worktree_request(
                project, "operation=merge", {**values, "merged_head": head}, identity=short_id("merge")
            ).items()
            if key != "merged_head"
        }
        with pytest.raises(WorkspaceError, match="needs merged_head"):
            workflow(project, "worktree", without)
        assert workflow(
            project, "worktree",
            worktree_request(project, "operation=merge", {**values, "merged_head": head}, identity=short_id("merge")),
        )
        assert load_state(project)["worktrees"][0]["merge"]["merged_head"] == head

    def test_a_merge_record_written_before_merged_head_existed_still_validates(
        self, gated: None, delivered: tuple[Path, Path]
    ) -> None:
        project, linked = delivered
        write_versions(linked, "0.24.0", None)
        commit_all(linked, "release 0.24.0")
        opened = open_round(project, "diff", ("combined",), worktree=str(linked))
        close(project, opened, reports=report(("combined",)))
        workflow(
            project, "worktree",
            worktree_request(
                project, "operation=alpha,exempt=true",
                {"path": str(linked), "exempt": True, "reason": "docs-only project; no version change"},
            ),
        )
        workflow(
            project, "worktree",
            worktree_request(
                project, "operation=release,replaces_exemption=true",
                {"path": str(linked), "version": "0.24.0", "files": FILES, "replaces_exemption": True},
            ),
        )
        state = load_state(project)
        state["worktrees"][0]["merge"] = {
            "state": "merged", "method": "squash", "evidence": EVIDENCE, "source": "s", "response": "r",
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }
        assert delivery_errors(state["worktrees"][0], "worktrees[0]") == []
