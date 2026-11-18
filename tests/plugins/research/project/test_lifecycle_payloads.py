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
    """The entry, the routine commands and the router carry every project; one branch adds to them.

    automation-records.md is merged into automation.md (0.33.0), so no branch needs a records
    file. Each ceiling is the measured words rounded up to the next 10, and the always-loaded
    chain stays below the 0.32.0 records-branch ceiling of 2442 words. At 0.37.0 the entry gained two
    pointers (tags at creation, follow-up at closure) and `--tag` in the `init` example, 16 words; the
    ceilings rose to the new measurement: 860 to 870, 1400 to 1410, 2190 to 2200, 2520 to 2540 and
    2730 to 2740. At 0.38.0 the delivery target changed what the entry, the routine commands and the
    router say about when a worktree, MR, merge or release is owed: 20 words in the entry, a one-line
    pointer in commands.md and a `retarget` row in automation.md. The ceilings rose to the new
    measurement: 870 to 890, 1410 to 1440, 2200 to 2260, 2540 to 2600 and 2740 to 2800. At 0.39.0 the
    router gained a `settings` row, an `amend` pointer and a policy-3 `retarget` note (27 words); the
    entry gained none. The chain and the two selected-reference ceilings rose to the new measurement:
    2260 to 2290, 2600 to 2630 and 2800 to 2840. At 0.40.0 automation-execution.md gained a one-line
    pointer to the policy-4 attempt rules, which live in durability.md (8 words); its ceiling rose
    from 2840 to 2850.
    """
    skill = REPO_ROOT / "plugins/research/skills/project"
    entry = (skill / "SKILL.md").read_text()
    routine = (skill / "references/commands.md").read_text()
    assert len(entry.split()) <= 890
    assert len((entry + routine).split()) <= 1440
    router = (skill / "references/automation.md").read_text()
    chain = len((entry + routine + router).split())
    assert chain <= 2290 < 2442
    ceilings = {"automation-context": 2630, "automation-execution": 2850}
    for reference, ceiling in ceilings.items():
        selected = (skill / f"references/{reference}.md").read_text()
        assert len((entry + routine + router + selected).split()) <= ceiling


SKILL_DIR = REPO_ROOT / "plugins/research/skills/project"
CORE = ("SKILL", "commands")
ALIGNMENT = ("durable-context", "writing-rules", "grill", "planning-review", "memory-operations", "automation",
             "handoff-writing", "goldfish-review")
RESUME = ("durable-context", "writing-rules", "session-handoff", "memory-operations", "automation",
          "automation-context", "handoff-writing")

# Each scenario is the complete set of documents the routing requires for that piece of work, in the
# order SKILL.md reaches them. Documents reached only conditionally are classified in CONDITIONAL,
# below, not left out silently. Each ceiling is the measured words of the set at 0.33.0 rounded up
# to the next 10, so a ceiling is a measurement, not an allowance; BASELINE is what the same stage
# cost at 0.32.0, and every ceiling must stay strictly below it. At 0.37.0 the entry gained 16 words:
# two pointers to the on-demand tags-and-follow-up.md (tags at creation, follow-up at closure) and
# `--tag` in the `init` example. The procedures live in that reference, which no scenario loads. Every
# scenario that counts SKILL.md rose to its new measurement: alignment-repository 7060 to 7080,
# alignment-plain 6660 to 6680, alignment-and-handoff 7640 to 7660, resume 5080 to 5100,
# resume-with-verifier 5270 to 5290, material-change-after-resume 8590 to 8600, closure 5540 to 5550.
# At 0.38.0 worktrees.md gained the delivery-target table (about 330 words, the one place that says what
# each target owes; the other references point to it), pull-requests.md gained what each target
# changes at release, clean-up and commit, and the entry, commands and automation gained pointers and a
# `retarget` row. Every ceiling below is that new measurement, still strictly below BASELINE:
# alignment-repository 7080 to 7460, alignment-plain 6680 to 6750, resume 5100 to 5160,
# resume-with-verifier 5290 to 5350, alignment-and-handoff 7660 to 8040, material-change-after-resume
# 8600 to 8680, closure 5550 to 5990. The scenarios that load worktrees.md (alignment-repository,
# alignment-and-handoff, closure) carry most of the growth. At 0.39.0 automation.md gained a `settings`
# row and an `amend` pointer, goldfish-review.md a pointer to the on-demand review-risk.md (which no
# scenario loads) and planning-review.md and the entry a failing-first clause. Every scenario that counts
# those files rose to its new measurement: alignment-repository 7460 to 7510, alignment-plain 6750 to
# 6800, resume 5160 to 5190, resume-with-verifier 5350 to 5380, alignment-and-handoff 8040 to 8090,
# material-change-after-resume 8680 to 8730, closure 5990 to 6020.
SCENARIOS: dict[str, tuple[tuple[str, ...], int]] = {
    "alignment-repository": ((*CORE, *ALIGNMENT, "worktrees"), 7510),
    "alignment-plain": ((*CORE, *ALIGNMENT), 6800),
    "resume": ((*CORE, *RESUME), 5190),
    "resume-with-verifier": ((*CORE, *RESUME, "handoff-verifiers"), 5380),
    "alignment-and-handoff": ((*CORE, *ALIGNMENT, "worktrees", "session-handoff"), 8090),
    "material-change-after-resume": (
        (*CORE, *RESUME, "execution-changes", "grill", "planning-review"), 8730),
    "closure": ((*CORE, "durable-context", "writing-rules", "memory-operations", "memory-promotion", "automation",
                 "handoff-writing", "worktrees", "pull-requests", "goldfish-review"), 6020),
}
BASELINE = {
    "alignment-repository": 8205, "alignment-plain": 7726, "resume": 5843, "resume-with-verifier": 6033,
    "alignment-and-handoff": 8938, "material-change-after-resume": 9863, "closure": 6261,
}

# Words in the repository-target alignment set at 0.32.0, the size the 0.33.0 trim is measured
# against: a simpler procedure, not instructions moved outside the count.
ORIGINAL_ALIGNMENT_WORDS = BASELINE["alignment-repository"]

# Documents a loaded document links to but that only some work needs, and what triggers the read.
# A link to anything not in the scenario and not listed here is routing drift: either the scenario
# is missing a required read or this table is missing a classification.
CONDITIONAL = {
    "automation-context": "only for context reads such as resume, packet and readiness",
    "automation-execution": "only for worker events, verify batches and lesson triage",
    "design-document": "only when writing or updating the design task's artifacts/architecture.md",
    "durability": "only for intents, crash loops, waits, silent workers, busy locks or waivers",
    "execution-changes": "only when execution invalidates assignments, inputs or agreement",
    "goldfish-personas": "only for reviewer subagents; the coordinator hands the file to them",
    "goldfish-review": "only at a gate: a confirmation, the first code task, a release or a merge",
    "grill": "only when a change reopens requirements",
    "handoff-verifiers": "only when a handoff gives the successor a verifier",
    "legacy-executor": "only for old executor ownership or storage",
    "maintenance": "only when reopening a closed project",
    "planning-review": "only when a change reopens alignment",
    "memory-architecture": "only for memory format, migration or validation repairs",
    "records-layout": "only for the workspace records layout, doctor and migrate --layout",
    "memory-promotion": "only when closing with a promotion, compaction or retirement",
    "pull-requests": "only before a review ask or test deploy in a repository, and at repository closure",
    "review-risk": "only when review risk, fallback, amendments or options are set or changed",
    "report-design": "only when a report is requested",
    "report-execution": "only when an execution report is requested",
    "report-html": "only when an HTML report is requested",
    "session-handoff": "only for handoff or interruption recovery",
    "tags-and-follow-up": "only when creating a project, changing its tags or closing one that may leave a follow-up",
    "stopping": "only when deciding whether the work may stop after a task",
    "task-workers": "only when delegating to workers",
    "versioning": "only when proposing a base, recording an alpha line, moving a base or recording a release",
    "workspace-schema": "only for schema, migration or validation-error repairs",
    "worktrees": "only for a repository target",
    "writing-rules-extra": "only when a style finding names a preferred word or the rule set changes",
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
    """A scenario without a required reference must be reported, or the check proves nothing."""
    names = tuple(name for name in SCENARIOS["alignment-repository"][0] if name != "handoff-writing")
    reachable = set().union(*(linked_references(name) for name in names)) - set(names)
    assert "handoff-writing" in reachable - set(CONDITIONAL)


def test_the_alignment_path_stays_below_its_0_32_0_size() -> None:
    """The alignment path may not grow back past its 0.32.0 size (ORIGINAL_ALIGNMENT_WORDS)."""
    names, budget = SCENARIOS["alignment-repository"]
    words = sum(len(document(name).read_text(encoding="utf-8").split()) for name in names)
    assert budget < ORIGINAL_ALIGNMENT_WORDS and words < ORIGINAL_ALIGNMENT_WORDS


def test_writing_rules_is_loaded_in_every_scenario_that_writes_records() -> None:
    for scenario, (names, _) in SCENARIOS.items():
        assert "writing-rules" in names, scenario


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_ceiling_is_strictly_below_the_0_32_0_baseline(scenario: str) -> None:
    assert SCENARIOS[scenario][1] < BASELINE[scenario]


def test_durable_context_points_to_the_writing_rules_in_one_sentence() -> None:
    text = document("durable-context").read_text(encoding="utf-8")
    assert "Write each record as [writing-rules.md](writing-rules.md) says." in text
    assert "writing-rules" in linked_references("durable-context")


def test_the_goldfish_procedure_stays_within_300_words() -> None:
    """The coordinator reads this at every gate; by the user's decision (2026-10-27) it is at most 300 words."""
    assert len(document("goldfish-review").read_text(encoding="utf-8").split()) <= 300
