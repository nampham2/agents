"""Scripted CLI scenarios for policy 4: a crash after launch, a re-block, and a log that cannot be written (T06)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from workspace_invocations import INVOCATIONS_FILENAME
from workspace_session import _load_state

from tests.plugins.research.project.test_durability_effects import make_running, manage
from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_policy_four import _rewrite


@pytest.fixture
def four(tmp_path: Path) -> Path:
    project = make_running(tmp_path)
    _rewrite(project, 4)
    return project


def run(project: Path, action: str, capsys: Any, **fields: Any) -> tuple[int, Any]:
    path = project / f"{fields.get('id', action)}.json"
    payload = {} if action == "resume" else request(project, **fields)  # a read action takes no write keys
    path.write_text(json.dumps(payload), encoding="utf-8")
    capsys.readouterr()
    code = manage("workflow", project, action, path)
    captured = capsys.readouterr()
    try:
        return code, json.loads(captured.out)
    except ValueError:
        return code, captured.err


def event(project: Path, capsys: Any, name: str, identity: str, **extra: Any) -> tuple[int, Any]:
    return run(
        project, "worker-event", capsys, identity=identity, task="T01", handle="agent-1", event=name,
        scope="T01 code", observation="seen", assignment_revision=_load_state(project)["revision"], **extra,
    )  # fmt: skip


def test_a_crash_right_after_launch_reports_the_attempt_and_why_it_is_held(four: Path, capsys: Any) -> None:
    code, opened = event(four, capsys, "intent", "intent-1")
    assert code == 0 and opened["attempt_id"] == "T01-1-1"
    assert event(four, capsys, "launched", "launched-1", attempt_id=opened["attempt_id"])[0] == 0
    # The coordinator session dies here. A fresh one resumes and sees what is unresolved.
    code, resumed = run(four, "resume", capsys, identity="resume-1")
    assert code == 0
    (worker,) = resumed["unresolved_workers"]
    assert (worker["attempt_id"], worker["event"], worker["reason"], worker["superseded"]) == (
        "T01-1-1", "launched", "no terminal event", False,
    )  # fmt: skip
    # A second intent is refused until the first attempt is stopped.
    code, refusal = event(four, capsys, "intent", "intent-2")
    assert code != 0 and "held attempt blocks a new intent" in str(refusal)
    assert event(four, capsys, "stopped", "stop-1", attempt_id="T01-1-1")[0] == 0
    code, again = event(four, capsys, "intent", "intent-3")
    assert code == 0 and again["attempt_id"] == "T01-1-2"


def test_an_assignment_edit_after_launch_marks_the_attempt_superseded(four: Path, capsys: Any) -> None:
    _, opened = event(four, capsys, "intent", "intent-1")
    patch = four / "patch.json"
    patch.write_text(json.dumps({"tasks": [{"id": "T01", "name": "renamed"}]}), encoding="utf-8")
    assert manage("update", four, patch, "--expected-revision", _load_state(four)["revision"]) == 0
    capsys.readouterr()
    _, resumed = run(four, "resume", capsys, identity="resume-1")
    assert resumed["unresolved_workers"][0]["superseded"] is True
    code, refusal = event(four, capsys, "observed", "obs-1", attempt_id=opened["attempt_id"])
    assert code != 0 and "superseded attempt" in str(refusal)


def test_a_re_block_retires_the_wait_through_the_cli(four: Path, capsys: Any) -> None:
    def block() -> None:
        code = manage("task", four, "block", "T01", "--expected-revision", _load_state(four)["revision"],
                      "--reason", "waiting")  # fmt: skip
        assert code == 0
        capsys.readouterr()

    block()
    assert run(four, "wait", capsys, identity="wait-1", task="T01", on="reply")[0] == 0
    assert len(run(four, "resume", capsys, identity="r1")[1]["waits"]) == 1
    block()
    assert run(four, "resume", capsys, identity="r2")[1].get("waits", []) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_unwritable_log_warns_once_and_leaves_the_wait_anchor_alone(four: Path, capsys: Any) -> None:
    manage("task", four, "block", "T01", "--expected-revision", _load_state(four)["revision"], "--reason", "w")
    capsys.readouterr()
    assert run(four, "wait", capsys, identity="wait-1", task="T01", on="reply")[0] == 0
    log = four / INVOCATIONS_FILENAME
    mode = stat.S_IMODE(log.stat().st_mode)
    # Only the log is read-only (a directory mode would not stop an append to an existing file); `context`
    # writes nothing else, so this isolates the warning.
    log.chmod(stat.S_IRUSR)
    try:
        assert manage("context", four) == 0
        captured = capsys.readouterr()
        assert captured.err.count("warning: invocation log not written") == 1
        assert len(json.loads(captured.out)["waits"]) == 1
    finally:
        log.chmod(mode)
