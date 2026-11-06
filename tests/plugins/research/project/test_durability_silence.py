"""P4: resume flags a worker whose latest event is older than 30 minutes; display only.

Design: artifacts/architecture.md (project 2026-11-06-002) §4 "Resume and context output" and §7
"Workers". `silent_minutes` is added to each `unresolved_workers` entry and nothing else changes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import workspace_durability
from workspace_durability import silent_minutes
from workspace_session import _load_state
from workspace_workflows import workflow

from tests.plugins.research.project.test_durability_effects import make_running
from tests.plugins.research.project.test_lifecycle_commands import request


def observe(project: Path, identity: str = "observe-1", handle: str = "agent-1") -> str:
    workflow(
        project,
        "worker-event",
        request(
            project,
            identity,
            task="T01",
            handle=handle,
            event="observed",
            scope="T01 code",
            observation="still editing",
            assignment_revision=_load_state(project)["revision"],
        ),
    )
    text = (project / "tasks/T01.md").read_text(encoding="utf-8")
    events = [json.loads(match) for match in re.findall(r"^<!-- research-worker (.+) -->$", text, re.MULTILINE)]
    return next(event["recorded"] for event in reversed(events) if event["handle"] == handle)


@pytest.mark.parametrize(("minutes", "expected"), [(31, 31), (29, None), (30, None)])
def test_silent_minutes_after_thirty_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minutes: int, expected: "int | None"
) -> None:
    project = make_running(tmp_path)
    recorded = observe(project)
    moment = datetime.fromisoformat(recorded) + timedelta(minutes=minutes, seconds=5 if minutes != 30 else 0)
    monkeypatch.setattr(workspace_durability, "utcnow", lambda: moment)
    for action, body in (("resume", {}), ("packet", {"task": "T01", "worker": "w", "scope": "s", "lessons": []})):
        (worker,) = workflow(project, action, body)["unresolved_workers"]
        assert worker["handle"] == "agent-1" and worker["event"] == "observed"
        if expected is None:
            assert "silent_minutes" not in worker, action
        else:
            assert worker["silent_minutes"] == expected, action


def test_only_the_silent_worker_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_running(tmp_path)
    first = observe(project, "observe-1", "agent-1")
    observe(project, "observe-2", "agent-2")
    path = project / "tasks/T01.md"
    old = (datetime.fromisoformat(first) - timedelta(hours=2)).isoformat(timespec="seconds")
    path.write_text(
        path.read_text(encoding="utf-8").replace(f'"recorded": "{first}"', f'"recorded": "{old}"', 1), encoding="utf-8"
    )
    monkeypatch.setattr(workspace_durability, "utcnow", lambda: datetime.fromisoformat(first) + timedelta(minutes=1))
    workers = {item["handle"]: item for item in workflow(project, "resume", {})["unresolved_workers"]}
    assert workers["agent-1"]["silent_minutes"] == 121
    assert "silent_minutes" not in workers["agent-2"]


@pytest.mark.parametrize("recorded", [None, 7, "", "not a time", "2026-11-06T10:00:00"])
def test_an_unreadable_time_is_never_silent(recorded: object) -> None:
    assert silent_minutes(recorded) is None
