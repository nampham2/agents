"""P1: an effect intent survives a crash and resume names it until it is resolved or receipted.

Design: artifacts/architecture.md (project 2026-11-06-002) §4 "Notes entries" and "Workflow inputs
and refusals". The intent is a `research-effect` entry in the task notes; the revision never moves.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_invocations
import workspace_lib as lib
from workspace_context import compact_context
from workspace_durability import effect_intents
from workspace_invocations import INVOCATIONS_FILENAME, Invocation, note
from workspace_lib import WorkspaceError
from workspace_operations import task_operation
from workspace_session import _load_state, update_project_data, validated_project_context
from workspace_workflows import input_schema, worker_events, workflow

from tests.plugins.research.project.test_lifecycle_commands import request
from tests.plugins.research.project.test_workflow_automation import fill_spec

REFERENCE = "references/durability.md"


def plan_task(task_id: str, kind: str = "none") -> dict[str, Any]:
    external = kind in ("external", "destructive")
    return {
        "id": task_id,
        "name": f"Do {task_id}",
        "success_criteria": "ok",
        "verification": "run",
        "depends_on": [],
        "effect": {"kind": kind, "description": "Publish" if kind != "none" else None},
        "outputs": [],
        **({"authorization": {"required": True, "status": "pending"}} if external else {}),
    }


def make_running(tmp_path: Path, *, start: tuple[str, ...] = ("T01",)) -> Path:
    """T01 external and authorized, T02 local with no effect; the named tasks are started."""
    target = tmp_path / "target"
    target.mkdir()
    project = lib.allocate_project(tmp_path / "ws", title="Durable", working_directory=target, create_root=True)
    fill_spec(project)
    update_project_data(
        project, {"status": "PLANNING", "tasks": [plan_task("T01", "external"), plan_task("T02")]}, expected_revision=0
    )
    authorization = {
        "required": True,
        "status": "explicit",
        "scope": "Publish",
        "source": "user",
        "authorized_at": lib.now_iso(),
    }
    workflow(project, "authorize", request(project, "auth-t01", task="T01", authorization=authorization))
    for task_id in start:
        task_operation(project, "start", task_id, expected_revision=_load_state(project)["revision"])
    return project


def manage(*arguments: Any) -> int:
    with patch.object(sys, "argv", ["research-project", *[str(item) for item in arguments]]):
        return manage_workspace.main()


def lines(project: Path) -> list[dict[str, Any]]:
    path = project / INVOCATIONS_FILENAME
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


def intent(project: Path, identity: str, key: str = "pr-63", task: str = "T01", effect: str = "open PR 63") -> Any:
    return workflow(
        project, "effect", request(project, identity, operation="intent", task=task, key=key, effect=effect)
    )


def resolve(project: Path, identity: str, key: str = "pr-63", outcome: str = "done", **extra: Any) -> Any:
    return workflow(
        project,
        "effect",
        request(project, identity, operation="resolve", task="T01", key=key, outcome=outcome, **extra),
    )


def receipt(project: Path, identity: str, value: str, task: str = "T01") -> Any:
    body = {"kind": "publish", "value": value, "destination": "github", "timestamp": lib.now_iso()}
    return workflow(project, "receipt", request(project, identity, task=task, receipt=body))


@pytest.fixture
def running(tmp_path: Path) -> Path:
    return make_running(tmp_path)


def test_an_intent_shows_in_resume_and_context_and_never_moves_the_revision(running: Path, capsys: Any) -> None:
    project = running
    before = _load_state(project)["revision"]
    intent(project, "intent-1")
    assert _load_state(project)["revision"] == before
    text = (project / "tasks/T01.md").read_text(encoding="utf-8")
    marker = re.search(r"^<!-- research-effect (.+) -->$", text, re.MULTILINE)
    assert marker is not None
    entry = json.loads(marker[1])
    assert entry["op"] == "intent" and entry["key"] == "pr-63" and entry["effect"] == "open PR 63"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}[+-]\d\d:\d\d", entry["recorded"])
    expected = [{"task": "T01", "key": "pr-63", "effect": "open PR 63", "recorded": entry["recorded"]}]
    resumed = workflow(project, "resume", {})
    assert resumed["effect_intents"] == expected
    assert resumed["reference"] == REFERENCE
    packet = workflow(project, "packet", {"task": "T01", "worker": "w", "scope": "s", "lessons": []})
    assert packet["effect_intents"] == expected
    compact = compact_context(validated_project_context(project), _load_state(project), project)
    assert compact["effect_intents"] == expected and compact["reference"] == REFERENCE
    capsys.readouterr()
    assert manage("context", project) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["effect_intents"] == expected and full["reference"] == REFERENCE
    assert manage("context", project, "--compact") == 0
    assert json.loads(capsys.readouterr().out)["effect_intents"] == expected
    assert manage("context", project, "--validate") == 0
    assert json.loads(capsys.readouterr().out)["effect_intents"] == expected


def test_nothing_is_added_when_there_is_no_intent(running: Path) -> None:
    resumed = workflow(running, "resume", {})
    assert "effect_intents" not in resumed and "reference" not in resumed and "crash_loop" not in resumed
    compact = compact_context(validated_project_context(running), _load_state(running), running)
    assert "effect_intents" not in compact and "reference" not in compact


def test_resolve_clears_the_intent(running: Path) -> None:
    intent(running, "intent-1")
    resolve(running, "resolve-1", outcome="not-done", note="checked the remote: no PR")
    assert effect_intents(running, _load_state(running)) == []
    assert "effect_intents" not in workflow(running, "resume", {})
    # The key is free again: a new intent with it is open, and only a later resolve closes it.
    intent(running, "intent-2")
    assert [item["key"] for item in effect_intents(running, _load_state(running))] == ["pr-63"]
    resolve(running, "resolve-2", outcome="unknown")
    assert effect_intents(running, _load_state(running)) == []


def test_a_receipt_whose_value_is_the_key_clears_it_and_another_value_does_not(running: Path) -> None:
    intent(running, "intent-1", key="https://github.com/o/r/pull/63")
    receipt(running, "receipt-other", "https://github.com/o/r/pull/64")
    assert [item["key"] for item in effect_intents(running, _load_state(running))] == ["https://github.com/o/r/pull/63"]
    receipt(running, "receipt-same", "https://github.com/o/r/pull/63")
    assert effect_intents(running, _load_state(running)) == []


def test_a_receipt_on_another_task_does_not_clear_it(tmp_path: Path) -> None:
    project = make_running(tmp_path)
    update_project_data(
        project,
        {
            "tasks": [
                {
                    "id": "T02",
                    "effect": {"kind": "destructive", "description": "Delete"},
                    "authorization": {"required": True, "status": "pending"},
                }
            ]
        },
        expected_revision=_load_state(project)["revision"],
    )
    intent(project, "intent-t02", key="publish:k", task="T02", effect="delete branch")
    receipt(project, "receipt-t01", "publish:k")
    found = effect_intents(project, _load_state(project))
    assert [(item["task"], item["key"]) for item in found] == [("T02", "publish:k")]


def test_refusals(running: Path) -> None:
    first = request(running, "intent-1", operation="intent", task="T01", key="pr-63", effect="open PR 63")
    workflow(running, "effect", first)
    with pytest.raises(WorkspaceError, match=r"open intent .*pr-63"):
        intent(running, "intent-dup")
    with pytest.raises(WorkspaceError, match="no open intent"):
        resolve(running, "resolve-missing", key="other")
    with pytest.raises(WorkspaceError, match="external or destructive"):
        intent(running, "intent-t02", task="T02")
    with pytest.raises(WorkspaceError, match="outcome"):
        resolve(running, "resolve-bad", outcome="maybe")
    with pytest.raises(WorkspaceError, match="key"):
        intent(running, "intent-newline", key="a\nb")
    with pytest.raises(WorkspaceError, match="key"):
        intent(running, "intent-long", key="k" * 257)
    with pytest.raises(WorkspaceError, match="operation"):
        workflow(running, "effect", request(running, "bad-op", operation="cancel", task="T01", key="x"))
    with pytest.raises(WorkspaceError, match="missing required field"):
        workflow(running, "effect", request(running, "no-effect", operation="intent", task="T01", key="x"))
    with pytest.raises(WorkspaceError, match="missing required field"):
        workflow(running, "effect", request(running, "no-outcome", operation="resolve", task="T01", key="x"))
    with pytest.raises(WorkspaceError, match="note"):
        resolve(running, "resolve-note", note=7)
    with pytest.raises(WorkspaceError, match="unknown task"):
        intent(running, "intent-t09", task="T09")
    # The same operation ID completes once; changed input conflicts.
    workflow(running, "effect", first)
    with pytest.raises(WorkspaceError, match="already used with different input"):
        workflow(running, "effect", {**first, "effect": "something else"})
    assert len(effect_intents(running, _load_state(running))) == 1


def test_the_schema_prints_both_operations() -> None:
    schema = input_schema("effect")
    assert set(schema["allowed"]) >= {"operation", "task", "key", "effect", "outcome", "note"}
    assert set(schema["required_by"]) == {"operation=intent", "operation=resolve"}
    assert {"effect", "key", "operation", "task"} <= set(schema["required_by"]["operation=intent"])
    assert {"outcome", "key", "operation", "task"} <= set(schema["required_by"]["operation=resolve"])
    assert "note" not in schema["required_by"]["operation=resolve"]


def test_a_malformed_entry_is_skipped_and_the_old_worker_parser_is_not_tripped(running: Path) -> None:
    intent(running, "intent-1", effect="text with --> inside")
    path = running / "tasks/T01.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n<!-- research-effect {not json -->\n<!-- research-effect [1, 2] -->\n"
        + '<!-- research-effect {"op": "intent", "effect": "no key"} -->\n'
        + '<!-- research-effect {"op": "intent", "key": 7, "effect": "bad key"} -->\n'
        + '<!-- research-effect {"op": "explode", "key": "pr-63"} -->\n'
        + '<!-- research-effect {"op": "resolve", "key": ["pr-63"]} -->\n',
        encoding="utf-8",
    )
    found = effect_intents(running, _load_state(running))
    assert [(item["key"], item["effect"]) for item in found] == [("pr-63", "text with --> inside")]
    assert workflow(running, "resume", {})["effect_intents"] == found
    # A 0.33.0-shaped reader matches only `research-worker` lines, so it neither raises nor sees these.
    assert worker_events(running) == []
    written = next(line for line in path.read_text(encoding="utf-8").splitlines() if "inside" in line)
    assert written.count("-->") == 1  # the text's own `-->` is escaped, so the comment closes once


def test_a_missing_or_unreadable_notes_file_is_no_intent(running: Path) -> None:
    assert effect_intents(running, _load_state(running)) == []
    (running / "tasks").mkdir(exist_ok=True)
    (running / "tasks/T01.md").write_bytes(b"\xff\xfe broken")
    assert effect_intents(running, _load_state(running)) == []


def test_the_cli_path_writes_the_task_key(running: Path, capsys: Any) -> None:
    path = running / "intent.json"
    path.write_text(
        json.dumps(request(running, "cli-intent", operation="intent", task="T01", key="publish:k", effect="e"))
    )
    assert manage("workflow", running, "effect", path) == 0
    body = {"kind": "publish", "value": "publish:k", "destination": "github", "timestamp": lib.now_iso()}
    path.write_text(json.dumps(request(running, "cli-receipt", task="T01", receipt=body)))
    assert manage("workflow", running, "receipt", path) == 0
    capsys.readouterr()
    logged = lines(running)
    assert [(line["action"], line.get("task")) for line in logged] == [("effect", "T01"), ("receipt", "T01")]
    assert effect_intents(running, _load_state(running)) == []


def test_note_is_a_no_op_without_a_current_invocation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_invocations, "current", None)
    note(task="T01")  # no current invocation: nothing to attach to, nothing raised
    import argparse

    invocation = Invocation("research-project", argparse.Namespace(command="workflow", action="resume"))
    assert workspace_invocations.current is invocation
    note(worker_packet=True)
    assert invocation.facts == {"worker_packet": True}
