"""The checkpoint names the agent session that wrote it.

A dashboard that offers a resume command needs the exact session, not a guess from transcripts.
Pi sets `PI_SESSION_ID` and Claude Code sets `CLAUDE_CODE_SESSION_ID`; the checkpoint records the
id and host with the working directory the session was started in. Pi wins when both are set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import workspace_context as context
import workspace_journal as journal
import workspace_lib as lib
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import request


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return lib.allocate_project(
        tmp_path / "workspace", title="Checkpoint session", working_directory=target, create_root=True
    )


def checkpoint(project: Path, identity: str) -> str:
    workflow(project, "checkpoint", request(project, identity, continuation={"next": "Continue the work"}))
    return journal.snapshot(project / "handoff.md")[1]


def test_claude_session_is_recorded_with_its_working_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "e3109cdf-0c7b-41e5-acbf-b5a615c509cf")
    monkeypatch.chdir(tmp_path)
    checkpoint(project, "cp-1")
    data = context.checkpoint_data(project)
    assert data is not None
    assert data["session"] == {"host": "claude", "id": "e3109cdf-0c7b-41e5-acbf-b5a615c509cf", "cwd": os.getcwd()}


def test_pi_session_is_recorded_with_its_working_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("PI_SESSION_ID", "pi-session")
    monkeypatch.chdir(tmp_path)
    checkpoint(project, "cp-1")
    data = context.checkpoint_data(project)
    assert data is not None
    assert data["session"] == {"host": "pi", "id": "pi-session", "cwd": os.getcwd()}


def test_pi_wins_when_both_variables_are_set(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_SESSION_ID", "pi-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    assert context.session_observation() == {"host": "pi", "id": "pi-session", "cwd": os.getcwd()}


def test_without_the_variable_the_session_is_null(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    checkpoint(project, "cp-1")
    data = context.checkpoint_data(project)
    assert data is not None
    assert data["session"] is None


def test_blank_variable_counts_as_unknown(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "   ")
    assert context.session_observation() is None


def test_blank_pi_falls_back_to_claude(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_SESSION_ID", "   ")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    assert context.session_observation() == {"host": "claude", "id": "claude-session", "cwd": os.getcwd()}


def test_same_session_leaves_the_checkpoint_alone_and_a_new_one_rewrites_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "first")
    first = checkpoint(project, "cp-1")
    assert checkpoint(project, "cp-2") == first
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "second")
    assert checkpoint(project, "cp-3") != first
    data = context.checkpoint_data(project)
    assert data is not None
    assert data["session"]["id"] == "second"


def test_checkpoint_written_before_the_session_block_still_reads(project: Path) -> None:
    metadata = {"version": 1, "revision": 0, "sources": {}, "continuation": {"next": "Older note"}, "git": {}}
    (project / "handoff.md").write_text(
        "<!-- research-checkpoint-v1\n" + json.dumps(metadata) + "\n-->\n# Continuation\n", encoding="utf-8"
    )
    data = context.checkpoint_data(project)
    assert data is not None
    assert "session" not in data


@pytest.mark.parametrize(
    "session",
    [
        "nope",
        {"host": "claude"},
        {"host": "claude", "id": "", "cwd": "/tmp"},
        {"host": "claude", "id": 1, "cwd": "/"},
        {"host": "other", "id": "x", "cwd": "/"},
    ],
)
def test_malformed_session_block_is_refused(project: Path, session: object) -> None:
    metadata = {
        "version": 1,
        "revision": 0,
        "sources": {},
        "continuation": {"next": "Note"},
        "git": {},
        "session": session,
    }
    (project / "handoff.md").write_text(
        "<!-- research-checkpoint-v1\n" + json.dumps(metadata) + "\n-->\n# Continuation\n", encoding="utf-8"
    )
    with pytest.raises(lib.WorkspaceError):
        context.checkpoint_data(project)
