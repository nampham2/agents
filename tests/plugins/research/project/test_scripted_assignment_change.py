"""A scripted assignment change through both launchers: a reference edit bumps the counter and fences a worker.

A worker opens an attempt with the hash of its packet. The coordinator then edits `spec_refs`: the
assignment counter moves, the old attempt accepts only `stopped`, and the new packet carries a new hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from workspace_journal import snapshot

from tests.plugins.research.project.test_current_lifecycle import SURFACES, Cli, state_of
from tests.plugins.research.project.test_packets import HEADING, OTHER, _project, _set


def event(cli: Cli, project: Path, identity: str, kind: str, **extra: Any) -> Any:
    notes = project / "tasks" / "T01.md"
    payload = {
        "id": identity, "expected_revision": state_of(project)["revision"],
        "tokens": {"tasks/T01": snapshot(notes)[1] if notes.exists() else "missing"},
        "task": "T01", "handle": "worker-1", "event": kind, "scope": "T01 files", "observation": "o",
        "assignment_revision": state_of(project)["revision"], **extra,
    }
    return cli.run("workflow", project, "worker-event", "-", stdin=json.dumps(payload))


@pytest.mark.parametrize("surface", SURFACES)
def test_a_reference_edit_bumps_the_counter_and_fences_the_old_attempt(tmp_path: Path, surface: str) -> None:
    cli = Cli(surface)
    project = _project(tmp_path, 5)
    _set(project, "T01", spec_refs=[HEADING])
    first = json.loads(cli.ok("context", project, "--task", "T01", "--worker").stdout)["packet"]
    assert [s["heading"] for s in first["spec_sections"]] == [HEADING] and first["assignment_counter"] == 1

    opened = event(cli, project, "intent-1", "intent", packet_sha256=first["packet_sha256"])
    assert opened.returncode == 0, opened.stderr
    attempt = json.loads(opened.stdout)["attempt_id"]
    assert attempt == "T01-1-1"

    patch = {"tasks": [{"id": "T01", "spec_refs": [HEADING, OTHER]}]}
    cli.ok("update", project, "-", "--expected-revision", state_of(project)["revision"], stdin=json.dumps(patch))
    assert state_of(project)["tasks"][0]["assignment_counter"] == 2

    refused = event(cli, project, "observed-1", "observed", attempt_id=attempt)
    assert refused.returncode != 0 and "superseded attempt" in refused.stderr
    stopped = event(cli, project, "stopped-1", "stopped", attempt_id=attempt)
    assert stopped.returncode == 0, stopped.stderr

    second = json.loads(cli.ok("context", project, "--task", "T01", "--worker").stdout)["packet"]
    assert second["assignment_counter"] == 2 and second["packet_sha256"] != first["packet_sha256"]
    assert [s["heading"] for s in second["spec_sections"]] == [HEADING, OTHER]
    reopened = event(cli, project, "intent-2", "intent", packet_sha256=second["packet_sha256"])
    assert reopened.returncode == 0
    assert json.loads(reopened.stdout)["attempt_id"] == "T01-2-2"  # the number counts every intent
    again = json.loads(cli.ok("context", project, "--task", "T01", "--worker").stdout)["packet"]
    assert again["notes"] == []  # the latest intent carries the current hash


@pytest.mark.parametrize("surface", SURFACES)
def test_a_bad_reference_is_refused_by_update_and_names_the_task(tmp_path: Path, surface: str) -> None:
    cli = Cli(surface)
    project = _project(tmp_path, 5)
    patch = {"tasks": [{"id": "T02", "spec_refs": ["No such heading"]}]}
    refused = cli.run("update", project, "-", "--expected-revision", state_of(project)["revision"],
                      stdin=json.dumps(patch))
    assert refused.returncode != 0 and "task T02: spec_refs names no" in refused.stderr
    assert "spec_refs" not in state_of(project)["tasks"][1]
