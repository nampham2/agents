"""Before/after CLI lifecycle trials; bytes measure payload, not billed tokens or agent context."""

from __future__ import annotations

import contextlib
import io
import json
import re
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
    router = (skill / "references/automation.md").read_text()
    assert len((entry + routine + router).split()) <= 1750
    for reference in ("automation-context", "automation-records", "automation-execution"):
        selected = (skill / f"references/{reference}.md").read_text()
        assert len((entry + routine + router + selected).split()) <= 2350


SKILL_DIR = REPO_ROOT / "plugins/research/skills/project"
CORE = ("SKILL", "commands")
ALIGNMENT = ("durable-context", "grill", "architecture-review", "memory-operations", "automation",
             "automation-records", "handoff-writing")
RESUME = ("durable-context", "session-handoff", "memory-operations", "automation", "automation-context",
          "automation-records", "handoff-writing")

# Each scenario is the complete set of documents the routing requires for that piece of work, in the
# order SKILL.md reaches them. The first version of these budgets omitted the automation router, the
# records reference and the handoff guide that alignment cannot proceed without, so the measured
# cost was about a quarter lower than the cost paid. Documents reached only conditionally are
# classified in CONDITIONAL, below, not left out silently.
SCENARIOS: dict[str, tuple[tuple[str, ...], int]] = {
    "alignment-repository": ((*CORE, *ALIGNMENT, "worktrees"), 7031),
    "alignment-plain": ((*CORE, *ALIGNMENT), 6550),
    "resume": ((*CORE, *RESUME), 5000),
    "resume-with-verifier": ((*CORE, *RESUME, "handoff-verifiers"), 5200),
    "alignment-and-handoff": ((*CORE, *ALIGNMENT, "worktrees", "session-handoff"), 7800),
    "material-change-after-resume": ((*CORE, *RESUME, "execution-changes", "grill", "architecture-review"), 9000),
    "closure": ((*CORE, "durable-context", "memory-operations", "memory-promotion", "automation",
                 "automation-records", "handoff-writing", "worktrees"), 4450),
}

# Words in the same repository-target alignment set as measured at commit 4b3a68f (version 0.21.0),
# before this work added the compact path and removed duplicated procedure. The budget above must stay
# at least 10 percent below it: a simpler procedure, not instructions moved outside the count.
ORIGINAL_ALIGNMENT_WORDS = 7813

# Documents a loaded document links to but that only some work needs, and what triggers the read.
# A link to anything not in the scenario and not listed here is routing drift: either the scenario
# is missing a required read or this table is missing a classification.
CONDITIONAL = {
    "architecture-review": "only when a change reopens alignment",
    "automation-context": "only for context reads such as resume, packet and readiness",
    "automation-execution": "only for worker events, verify batches and lesson triage",
    "execution-changes": "only when execution invalidates assignments, inputs or agreement",
    "grill": "only when a change reopens requirements",
    "handoff-verifiers": "only when a handoff gives the successor a verifier",
    "legacy-executor": "only for old executor ownership or storage",
    "maintenance": "only when reopening a closed project",
    "memory-architecture": "only for memory format, migration or validation repairs",
    "memory-promotion": "only when closing with a promotion, compaction or retirement",
    "report-design": "only when a report is requested",
    "report-execution": "only when an execution report is requested",
    "report-html": "only when an HTML report is requested",
    "session-handoff": "only for handoff or interruption recovery",
    "task-workers": "only when delegating to workers",
    "workspace-schema": "only for schema, migration or validation-error repairs",
    "worktrees": "only for a repository target",
}


def document(name: str) -> Path:
    return SKILL_DIR / ("SKILL.md" if name == "SKILL" else f"references/{name}.md")


def linked_references(name: str) -> set[str]:
    text = document(name).read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r"\]\((?:references/)?([a-z-]+)\.md(?:#[^)]*)?\)", text)}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_required_lifecycle_instruction_budget(scenario: str) -> None:
    """Count the complete load set for each phase, not only the entrypoint and its router.

    Each file is loaded once per session and a fresh session pays again. These are source words, not
    billed or cache-adjusted tokens. Reports, workers and schema repairs are conditional (below).
    """
    names, budget = SCENARIOS[scenario]
    words = sum(len(document(name).read_text(encoding="utf-8").split()) for name in names)
    assert words <= budget, f"{scenario}: {words} words exceeds {budget}"
    print(json.dumps({"scenario": scenario, "instruction_words": words, "budget": budget}))


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_reachable_reference_is_loaded_or_classified_as_conditional(scenario: str) -> None:
    """Routing drift: a link added to a loaded document must be counted or classified, never ignored."""
    names, _ = SCENARIOS[scenario]
    reachable = set().union(*(linked_references(name) for name in names)) - set(names)
    unclassified = sorted(reachable - set(CONDITIONAL))
    assert not unclassified, f"{scenario} reaches {unclassified} without loading or classifying them"


def test_every_reference_is_in_a_scenario_or_classified() -> None:
    placed = {name for names, _ in SCENARIOS.values() for name in names}
    on_disk = {path.stem for path in (SKILL_DIR / "references").glob("*.md")}
    unplaced = sorted(on_disk - placed - set(CONDITIONAL))
    assert not unplaced, f"references no scenario loads and nothing classifies: {unplaced}"
    missing = sorted(name for name in placed | set(CONDITIONAL) if name != "SKILL" and name not in on_disk)
    assert not missing, f"scenarios or classifications name documents that do not exist: {missing}"


def test_the_drift_check_notices_a_required_read_missing_from_a_scenario() -> None:
    """A scenario without the records reference must be reported, or the check proves nothing."""
    names = tuple(name for name in SCENARIOS["alignment-repository"][0] if name != "automation-records")
    reachable = set().union(*(linked_references(name) for name in names)) - set(names)
    assert "automation-records" in reachable - set(CONDITIONAL)


def test_the_compact_alignment_path_costs_at_least_ten_percent_less_than_it_did() -> None:
    names, budget = SCENARIOS["alignment-repository"]
    words = sum(len(document(name).read_text(encoding="utf-8").split()) for name in names)
    assert budget <= ORIGINAL_ALIGNMENT_WORDS * 0.9 and words <= ORIGINAL_ALIGNMENT_WORDS * 0.9
