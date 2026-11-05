"""S4: refusals that name the fix. The inferred stop_reason itself is tested in test_handoff_stop_reasons.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from workspace_lib import WorkspaceError, document_conflict_message
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    load_state,
    make_project,
    make_repository,
    record,
    refused,
    run,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def linked(repository: Path) -> Path:
    return add_worktree(repository)


def test_a_conflict_message_carries_a_request_skeleton() -> None:
    nested = document_conflict_message("spec", "a" * 8, "b" * 8, Path("/p"))
    assert nested.endswith('retry with {"tokens": {"spec": "bbbbbbbb"}}')
    assert "expected token aaaaaaaa, found bbbbbbbb" in nested  # the head the invocation log keeps is unchanged
    flat = document_conflict_message("planning", "a", "b", Path("/p"), use="proposal_sha256")
    assert flat.endswith('retry with {"proposal_sha256": "b"}')


def test_a_confirm_with_missing_fields_says_what_each_one_is(tmp_path: Path) -> None:
    project = make_project(tmp_path, tmp_path)
    with pytest.raises(WorkspaceError) as refusal:
        workflow(project, "confirm", request(project, "confirm-bare"))
    message = str(refusal.value)
    assert "missing required field(s)" in message
    assert "review_id is the identifier (A1, A2) written in planning.md" in message
    assert "response is the user's actual words" in message


def test_a_worktree_request_with_an_empty_or_missing_path_gives_the_default(tmp_path: Path, repository: Path) -> None:
    project = make_project(tmp_path, repository)
    empty = {"operation": "record", "path": "", "branch": "b", "confirmation": {"source": "u", "response": "y"}}
    refused(project, r"use <repo-parent>/<repo>\.worktrees/<project-id>", run, empty)
    refused(project, r"use <repo-parent>/<repo>\.worktrees/<project-id>", run, {**empty, "path": 7})
    with pytest.raises(WorkspaceError, match=r"use <repo-parent>/<repo>\.worktrees/<project-id>"):
        workflow(
            project, "worktree",
            request(
                project, "wt-no-path", operation="record", branch="b", confirmation={"source": "u", "response": "y"},
            ),
        )


def test_a_repository_mismatch_names_the_value_to_send(tmp_path: Path, repository: Path, linked: Path) -> None:
    project = make_project(tmp_path, repository)
    observed = str(repository.resolve())
    refused(project, f"observed {observed}; send repository {observed}", record, linked, repository=str(tmp_path))
    assert not load_state(project).get("worktrees")
