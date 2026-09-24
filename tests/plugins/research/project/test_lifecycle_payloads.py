"""Before/after CLI lifecycle trials; bytes measure payload, not billed tokens or agent context."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import validate_workspace
from workspace_lib import SPEC_CANONICAL_SECTIONS, atomic_write_json

from tests.conftest import REPO_ROOT
from tests.plugins.research.project.test_workspace import WorkspaceFixture


def invoke(arguments: list[str], *, validator: bool = False) -> tuple[int, str]:
    """Invoke the real CLI in-process, preserving its exit and complete emitted text."""
    output = io.StringIO()
    with patch.object(sys, "argv", ["research", *arguments]), contextlib.redirect_stdout(output), \
            contextlib.redirect_stderr(output):
        code = validate_workspace.main() if validator else manage_workspace.main()
    return code, output.getvalue()


def lifecycle(root: Path, *, lean: bool, history: int, interrupted: bool) -> dict[str, int]:
    fixture = WorkspaceFixture(root)
    project = str(fixture.project_dir)
    state = fixture.state("PLANNING")
    state["tasks"] = [fixture.task(f"H{i}") for i in range(history)]
    for task in state["tasks"]:
        task["evidence"][0]["anchor"] = "baseline"
    atomic_write_json(fixture.project_dir / "project.json", state)
    (fixture.project_dir / "evidence.md").write_text(
        "# Evidence\n\n## baseline\nHistorical fixture outputs verified.\n"
    )
    (fixture.project_dir / "spec.md").write_text(
        "# Fixture\n\n## Current specification\n\n" + "".join(
            f"### {title}\n\n" + "Preserve the requested output and verify each change. " * 8 + "\n\n"
            for title, _ in SPEC_CANONICAL_SECTIONS
        ) + "## Decision history\n\nOld record.\n",
    )
    observations: list[str] = []

    def normalized(text: str) -> str:
        return text.replace(str(fixture.project_dir), "<project>") \
            .replace(str(fixture.target_dir), "<target>").replace(str(fixture.workspace_root), "<workspace>")

    def call(arguments: list[str], *, validator: bool = False, expected: int = 0) -> str:
        code, text = invoke(arguments, validator=validator)
        assert code == expected, text
        observations.append(normalized(text))
        return text

    patch_path = root / "patch.json"
    revision = 0

    def update(changes: dict[str, Any]) -> None:
        nonlocal revision
        atomic_write_json(patch_path, changes)
        call(["update", project, str(patch_path), "--expected-revision", str(revision)])
        revision += 1

    call(["context", project])
    call([project], validator=True)
    tasks = [{
        "id": f"T{i}", "name": f"Write and check output {i}", "success_criteria": f"out.txt contains {i}",
        "verification": "Read and compare the output", "effect": {"kind": "local_write", "description": "Edit out.txt"},
        "outputs": [{"root": "target", "path": "out.txt", "required": True}],
        "depends_on": [f"T{i - 1}"] if i else [],
    } for i in range(3)]
    update({"tasks": tasks})
    update({"status": "EXECUTING", "tasks": [{"id": "T0", "status": "RUNNING"}]})
    for index in range(3):
        task_id = f"T{index}"
        context_flags = ["--task-only"] if lean else []
        view = json.loads(call(["context", project, "--task", task_id, *context_flags]))
        assert view["selected_task"]["success_criteria"] == f"out.txt contains {index}"
        assert view["selected_task"]["status"] == "RUNNING"
        command = [sys.executable, "-c", f"from pathlib import Path; assert Path('out.txt').read_text() == '{index}'"]
        if interrupted and index == 1:
            # A failed command and a new resume must not be hidden by the smaller projection.
            call(["record-evidence", project, "--task", task_id, "--", *command], expected=1)
            call(["context", project])
            call([project], validator=True)
            if lean:
                evidence = json.loads(call(["read", project, "evidence", "--task", task_id]))["text"]
            else:
                evidence = (fixture.project_dir / "evidence.md").read_text()
                observations.append(normalized(evidence))
            assert "FAILED" in evidence
        (fixture.target_dir / "out.txt").write_text(str(index))
        call(["record-evidence", project, "--task", task_id, "--", *command])
        transition: dict[str, Any] = {"tasks": [{"id": task_id, "status": "DONE", "evidence": [
            {"root": "workspace", "path": "evidence.md", "anchor": task_id},
        ]}]}
        if index < 2:
            transition["tasks"].append({"id": f"T{index + 1}", "status": "RUNNING"})
        else:
            (fixture.project_dir / "reflection.md").write_text("# Handoff\nOutput verified; no open work.\n")
            transition["status"] = "DONE"
        update(transition)
    call([project, "--close", "--check-index"], validator=True)
    assert (fixture.target_dir / "out.txt").read_text() == "2"
    final = json.loads((fixture.project_dir / "project.json").read_text())
    assert final["status"] == "DONE"
    assert all(task["status"] == "DONE" for task in final["tasks"])
    return {"output_bytes": sum(len(text.encode()) for text in observations),
            "largest_output_bytes": max(len(text.encode()) for text in observations),
            "observations": len(observations)}


@pytest.mark.parametrize("scenario,history,interrupted", [
    ("small", 0, False), ("history", 200, False), ("resumed", 200, True),
])
def test_before_after_lifecycle_payloads(tmp_path: Path, scenario: str, history: int, interrupted: bool) -> None:
    before = lifecycle(tmp_path / "before", lean=False, history=history, interrupted=interrupted)
    after = lifecycle(tmp_path / "after", lean=True, history=history, interrupted=interrupted)
    # These fixtures reuse the same specification. Full task requirements remain lossless.
    assert after["output_bytes"] < before["output_bytes"] * 0.8
    assert after["largest_output_bytes"] < before["largest_output_bytes"]
    assert after["observations"] == before["observations"]
    print(json.dumps({"scenario": scenario, "before": before, "after": after}, sort_keys=True))


def test_default_instruction_payload_budget() -> None:
    skill = REPO_ROOT / "plugins/research/skills/project"
    entry = (skill / "SKILL.md").read_text()
    routine = (skill / "references/commands.md").read_text()
    assert len(entry.split()) <= 900
    assert len((entry + routine).split()) <= 1500


@pytest.mark.parametrize("scenario,references,budget", [
    ("alignment", ("durable-context", "grill", "architecture-review", "memory-operations"), 5800),
    ("resume", ("durable-context", "session-handoff", "memory-operations"), 3400),
    ("alignment-and-handoff", (
        "durable-context", "grill", "architecture-review", "memory-operations", "session-handoff",
    ), 6600),
    ("material-change-after-resume", (
        "durable-context", "session-handoff", "memory-operations", "execution-changes",
        "grill", "architecture-review",
    ), 8000),
])
def test_required_lifecycle_instruction_budget(scenario: str, references: tuple[str, ...], budget: int) -> None:
    """Count phase load sets, including required references, rather than only entrypoint routing.

    Keep these sets aligned with reference routing when procedures move. Each file is loaded once
    per session; a fresh session pays again. These are words, not billed or cache-adjusted tokens.
    Reports, workers and schema repairs are outside these scenarios, not free operations.
    """
    skill = REPO_ROOT / "plugins/research/skills/project"
    documents = [skill / "SKILL.md", skill / "references/commands.md"]
    documents.extend(skill / "references" / f"{name}.md" for name in references)
    words = sum(len(document.read_text(encoding="utf-8").split()) for document in documents)
    assert words <= budget, f"{scenario}: {words} words exceeds {budget}"
    print(json.dumps({"scenario": scenario, "instruction_words": words, "budget": budget}))
