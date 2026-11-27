"""Before/after CLI lifecycle trials; bytes measure payload, not billed tokens or agent context."""

from __future__ import annotations

import contextlib
import functools
import io
import json
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple
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
    pointer to the policy-4 attempt rules, which live in durability.md (7 words); its ceiling rose
    from 2840 to 2850. With the coordinator and automated modes (policy 6) the entry gained the
    pointer to modes.md (a trimmed sentence pays for part of it), commands.md the two `task finish`
    flags, automation.md a `decide` row and `confirmation` in the `cancel` and `maintenance` rows,
    and automation-execution.md the assessor sentence. The ceilings rose to the new measurement:
    890 to 910, 1440 to 1480, 2290 to 2370, 2630 to 2710 and 2850 to 2950.
    """
    check_budget("SKILL.md", counted(["SKILL"]), 910)
    check_budget("SKILL.md + commands.md", counted(CORE), 1480)
    chain = (*CORE, "automation")
    check_budget("chain", counted(chain), CHAIN_CEILING)
    assert CHAIN_CEILING < 2442
    ceilings = {"automation-context": 2710, "automation-execution": 2950}
    for reference, ceiling in ceilings.items():
        check_budget(f"chain + {reference}.md", counted((*chain, reference)), ceiling)

SKILL_DIR = REPO_ROOT / "plugins/research/skills/project"
CHAIN_CEILING = 2370
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
# material-change-after-resume 8680 to 8730, closure 5990 to 6020. With the policy-6 modes (SKILL.md, commands.md,
# automation.md; goldfish-review.md in closure) every ceiling rose to its new measurement: alignment-repository 7510
# to 7590, alignment-plain 6800 to 6880, resume 5190 to 5270, resume-with-verifier 5380 to 5460, alignment-and-handoff
# 8090 to 8170, material-change-after-resume 8730 to 8810, closure 6020 to 6100. modes.md is in no scenario.
SCENARIOS: dict[str, tuple[tuple[str, ...], int]] = {
    "alignment-repository": ((*CORE, *ALIGNMENT, "worktrees"), 7590),
    "alignment-plain": ((*CORE, *ALIGNMENT), 6880),
    "resume": ((*CORE, *RESUME), 5270),
    "resume-with-verifier": ((*CORE, *RESUME, "handoff-verifiers"), 5460),
    "alignment-and-handoff": ((*CORE, *ALIGNMENT, "worktrees", "session-handoff"), 8170),
    "material-change-after-resume": (
        (*CORE, *RESUME, "execution-changes", "grill", "planning-review"), 8810),
    "closure": ((*CORE, "durable-context", "writing-rules", "memory-operations", "memory-promotion", "automation",
                 "handoff-writing", "worktrees", "pull-requests", "goldfish-review"), 6100),
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
    "coding-standard": "only on a code task, before its first code edit, delegated or not",
    "design-document": "only when writing or updating the design task's artifacts/architecture.md",
    "durability": "only for intents, crash loops, waits, silent workers, busy locks or waivers",
    "execution-changes": "only when execution invalidates assignments, inputs or agreement",
    "gates": "only for the gate actions, a code task's finish refusal or a validation warning about records",
    "goldfish-personas": "only for reviewer subagents; the coordinator hands the file to them",
    "goldfish-review": "only at a gate: a confirmation, the first code task, a release or a merge",
    "grill": "only when a change reopens requirements",
    "handoff-verifiers": "only when a handoff gives the successor a verifier",
    "legacy-executor": "only for old executor ownership or storage",
    "maintenance": "only when reopening a closed project",
    "memory-architecture": "only for memory format, migration or validation repairs",
    "memory-promotion": "only when closing with a promotion, compaction or retirement",
    "modes": "only at policy 6: coordinator mode, the replan statement, the automated-mode offer and decisions",
    "planning-review": "only when a change reopens alignment",
    "pull-requests": "only before a review ask or test deploy in a repository, and at repository closure",
    "records-layout": "only for the workspace records layout, doctor and migrate --layout",
    "refusals": "only when a coordinator or automated-mode refusal needs its next step",
    "report-design": "only when a report is requested",
    "report-execution": "only when an execution report is requested",
    "report-html": "only when an HTML report is requested",
    "review-risk": (
        "only when review risk, fallback, amendments or options are set or changed, "
        "or a policy 0 to 2 round picks its stages"
    ),
    "session-handoff": "only for handoff or interruption recovery",
    "stopping": "only when deciding whether the work may stop after a task",
    "tags-and-follow-up": "only when creating a project, changing its tags or closing one that may leave a follow-up",
    "task-workers": "only when delegating to workers",
    "versioning": "only when proposing a base, recording an alpha line, moving a base or recording a release",
    "worker-personas": "only for a worker on a code task; the coordinator hands the path to it",
    "workspace-schema": "only for schema, migration or validation-error repairs",
    "worktrees": "only for a repository target",
    "writing-rules-extra": "only when a style finding names a preferred word or the rule set changes",
}


def shipped_names() -> list[str]:
    return ["SKILL", *sorted(path.stem for path in (SKILL_DIR / "references").glob("*.md"))]


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
    counts = counted(names)
    check_budget(f"scenario {scenario}", counts, budget)
    words = sum(now for _, _, now in counts)
    print(json.dumps({"scenario": scenario, "instruction_words": words, "budget": budget}))


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_reachable_reference_is_loaded_or_classified_as_conditional(scenario: str) -> None:
    """Routing drift: a link added to a loaded document must be counted or classified, never ignored."""
    names, _ = SCENARIOS[scenario]
    reachable = set().union(*(linked_references(name) for name in names)) - set(names)
    unclassified = sorted(reachable - set(CONDITIONAL))
    assert not unclassified, f"{scenario} reaches {unclassified} without loading or classifying them"


def test_conditional_is_sorted_by_key() -> None:
    assert list(CONDITIONAL) == sorted(CONDITIONAL)


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
    check_budget("goldfish-review.md", counted(["goldfish-review"]), 300)


# --- c8: per-file overage report and the rule-row check against the base -------------------------------


class Row(NamedTuple):
    identity: str
    text: str


class RowChanges(NamedTuple):
    deleted: list[Row]
    moved: list[Row]
    reworded: list[tuple[Row, Row]]


# Identity of a base row -> why deleting it is intended. Keys are the list-item rows of the sentence
# ledger in the design (artifacts/architecture.md section 3.4); any other deleted row fails the test.
INTENDED_ROW_CHANGES: dict[str, str] = {
    "a pr merged without a": "goldfish-review.md item 7 moves to pull-requests.md step 4 (T08)",
    "stages policy 3+ follows review-risk.md": "goldfish-review.md item 3 moves to review-risk.md (T11)",
}

RULE_START = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
FENCE = re.compile(r"^\s*(?:```|~~~)")
SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$")
TRAILING_PUNCTUATION = ".,;:!?)\"'*_"


def collapse(text: str) -> str:
    return " ".join(text.split())


def identity_of(text: str, words: int | None = None) -> str:
    """Backticks removed, a link reduced to its text, trailing punctuation dropped per word, lowercase."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("`", "")
    kept = [word.rstrip(TRAILING_PUNCTUATION).lower() for word in text.split()]
    kept = [word for word in kept if word]
    return " ".join(kept if words is None else kept[:words])


def outside_fences(text: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    return lines


def table_body(table: list[str]) -> list[str]:
    """Drop the header and the separator of a table block."""
    if len(table) > 1 and SEPARATOR.match(table[1]):
        return table[2:]
    return table


def close_item(rows: list[tuple[str, str]], item: list[str]) -> None:
    if item:
        rows.append(("item", " ".join(item)))


def raw_rows(lines: list[str]) -> list[tuple[str, str]]:
    """Table body rows and list items, in order, as (kind, text) with wrapped lines joined."""
    rows: list[tuple[str, str]] = []
    item: list[str] = []
    table: list[str] = []
    for line in [*lines, ""]:
        stripped = line.strip()
        if stripped.startswith("|"):
            close_item(rows, item)
            item = []
            table.append(stripped)
            continue
        rows.extend(("table", row) for row in table_body(table))
        table = []
        if RULE_START.match(line):
            close_item(rows, item)
            item = [stripped]
        elif stripped and item and not stripped.startswith("#"):
            item.append(stripped)
        else:
            close_item(rows, item)
            item = []
    return rows


def rule_rows(text: str) -> list[Row]:
    """The rule rows of one document: table body rows and list items outside code fences."""
    seen: dict[str, int] = {}
    rows: list[Row] = []
    for kind, raw in raw_rows(outside_fences(text)):
        if kind == "table":
            identity = identity_of(raw.strip().strip("|").split("|")[0])
        else:
            identity = identity_of(RULE_START.sub("", raw, count=1), 5)
        seen[identity] = seen.get(identity, 0) + 1
        suffix = f"#{seen[identity]}" if seen[identity] > 1 else ""
        rows.append(Row(identity + suffix, collapse(raw)))
    return rows


def row_changes(base_text: str, new_text: str, elsewhere: Collection[str] = ()) -> RowChanges:
    """Deleted: identity gone from this file and exact text in no other document. Moved: text elsewhere."""
    now = {row.identity: row for row in rule_rows(new_text)}
    changes = RowChanges([], [], [])
    for row in rule_rows(base_text):
        current = now.get(row.identity)
        if current is not None:
            if current.text != row.text:
                changes.reworded.append((row, current))
        elif row.text in elsewhere:
            changes.moved.append(row)
        else:
            changes.deleted.append(row)
    return changes


def deleted_rows(base_text: str, new_text: str, elsewhere: Collection[str] = ()) -> list[Row]:
    return row_changes(base_text, new_text, elsewhere).deleted


def lost_sentences(base: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    """Base sentences (split on '. ', whitespace collapsed) found in no document now. Printed only."""
    now = collapse(" ".join(new.values()))
    lost: list[str] = []
    for name, text in base.items():
        lost += [f"{name}: {s}" for s in collapse(text).split(". ") if s.rstrip(".") and s.rstrip(".") not in now]
    return lost


def trim_report(base: Mapping[str, str], new: Mapping[str, str],
                allowed: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return (refused lines, printed lines) for the base documents against the new ones."""
    refused: list[str] = []
    printed: list[str] = []
    for name, base_text in base.items():
        elsewhere = {row.text for other, text in new.items() if other != name for row in rule_rows(text)}
        changes = row_changes(base_text, new.get(name, ""), elsewhere)
        for row in changes.deleted:
            if row.identity in allowed:
                printed.append(f"allowed {name}: {row.identity} ({allowed[row.identity]})")
            else:
                refused.append(f"{name}: {row.identity}: {row.text}")
        printed += [f"moved {name}: {row.identity}: {row.text}" for row in changes.moved]
        printed += [f"reworded {name}: {old.identity}: {old.text} -> {now.text}" for old, now in changes.reworded]
    printed += [f"sentence gone {line}" for line in lost_sentences(base, new)]
    return refused, printed


def assert_no_deleted_rows(base: Mapping[str, str], new: Mapping[str, str],
                           allowed: Mapping[str, str] | None = None) -> None:
    """Fail on a deleted rule row; print moved and reworded rows and the prose sentences that are gone."""
    refused, printed = trim_report(base, new, INTENDED_ROW_CHANGES if allowed is None else allowed)
    for line in printed:
        print(line)
    assert not refused, "rule rows deleted against the base (list a reviewed deletion in INTENDED_ROW_CHANGES):\n" \
        + "\n".join(refused)


def overage_report(label: str, counts: list[tuple[str, int, int]], ceiling: int) -> str:
    """Each counted file as (name, base words, words now), largest delta first, then the total overage."""
    total = sum(now for _, _, now in counts)
    lines = [f"{label}: {total} words, ceiling {ceiling}, over by {total - ceiling}"]
    for name, base, now in sorted(counts, key=lambda count: (count[1] - count[2], count[0])):
        lines.append(f"  {name}: base {base}, now {now}, delta {now - base:+d}")
    lines.append(f"total overage: {total - ceiling} words")
    return "\n".join(lines)


def check_budget(label: str, counts: list[tuple[str, int, int]], ceiling: int) -> None:
    if sum(now for _, _, now in counts) > ceiling:
        report = overage_report(label, counts, ceiling)
        print(report)
        raise AssertionError(report)


def git(*args: str) -> str | None:
    """Output of a git command in the repository, or None when git or the object is not there."""
    try:
        done = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def base_ref(git_fn: Callable[..., str | None]) -> str | None:
    """The merge-base with origin/main, else HEAD (CI has no origin/main), else None without git."""
    fork = git_fn("merge-base", "HEAD", "origin/main")
    if fork and fork.strip():
        return fork.strip()
    return "HEAD" if git_fn("rev-parse", "HEAD") else None


@functools.cache
def base_snapshot() -> dict[str, str]:
    """Text of every shipped document at the base; a document new since then is empty; no git gives {}."""
    ref = base_ref(git)
    if ref is None:
        return {}
    return {name: git("show", f"{ref}:{document(name).relative_to(REPO_ROOT).as_posix()}") or ""
            for name in shipped_names()}


def counted(names: Iterable[str]) -> list[tuple[str, int, int]]:
    """(name, words at the base, words now) per document; the base equals now when git is unavailable."""
    snapshot = base_snapshot()
    counts: list[tuple[str, int, int]] = []
    for name in names:
        now = len(document(name).read_text(encoding="utf-8").split())
        counts.append((name, len(snapshot[name].split()) if snapshot else now, now))
    return counts


BASE_DOC = """# Title

| Name | Rule |
| --- | --- |
| `alpha` | Keep alpha. |
| beta | Keep beta. |

1. First rule: keep the
   wrapped line together.
2. Second rule: stay.

```
| fenced | row |
- fenced item
```

Prose sentence one. Prose sentence two.
"""
SECOND_RULE = "2. Second rule: stay.\n"
BETA_ROW = "| beta | Keep beta. |\n"


def test_rule_rows_are_table_body_rows_and_list_items_with_wrapped_lines_joined() -> None:
    rows = rule_rows(BASE_DOC)
    assert [row.identity for row in rows] == ["alpha", "beta", "first rule keep the wrapped", "second rule stay"]
    assert rows[2].text == "1. First rule: keep the wrapped line together."
    assert rows[0].text == "| `alpha` | Keep alpha. |"


def test_a_link_reduces_to_its_text_in_an_identity() -> None:
    text = "- Stages: policy 3+ follows [review-risk.md](review-risk.md), then more.\n"
    assert [row.identity for row in rule_rows(text)] == ["stages policy 3+ follows review-risk.md"]


def test_a_repeated_first_cell_takes_a_numbered_suffix() -> None:
    text = "| H | V |\n| --- | --- |\n| x | 1 |\n| x | 2 |\n"
    assert [row.identity for row in rule_rows(text)] == ["x", "x#2"]
    assert [row.identity for row in deleted_rows(text, "| H | V |\n| --- | --- |\n| x | 1 |\n")] == ["x#2"]


def test_a_seeded_rule_row_deletion_is_returned() -> None:
    deleted = deleted_rows(BASE_DOC, BASE_DOC.replace(SECOND_RULE, ""))
    assert [row.identity for row in deleted] == ["second rule stay"]


def test_a_seeded_table_row_deletion_is_returned() -> None:
    assert [row.identity for row in deleted_rows(BASE_DOC, BASE_DOC.replace(BETA_ROW, ""))] == ["beta"]


def test_identical_text_deletes_nothing() -> None:
    assert deleted_rows(BASE_DOC, BASE_DOC) == []


def test_a_prose_sentence_a_header_and_fenced_lines_are_not_rule_rows() -> None:
    trimmed = BASE_DOC.replace(" Prose sentence two.", "").replace("| Name | Rule |\n", "")
    trimmed = trimmed.replace("| fenced | row |\n- fenced item\n", "")
    assert deleted_rows(BASE_DOC, trimmed) == []


def test_a_row_moved_to_another_document_is_printed_not_deleted() -> None:
    new = BASE_DOC.replace(BETA_ROW, "")
    changes = row_changes(BASE_DOC, new, elsewhere={"| beta | Keep beta. |"})
    assert changes.deleted == []
    assert [row.identity for row in changes.moved] == ["beta"]


def test_a_reworded_row_keeps_its_identity_and_is_not_deleted() -> None:
    changes = row_changes(BASE_DOC, BASE_DOC.replace("Keep beta.", "Keep beta now."))
    assert changes.deleted == []
    assert [(old.identity, new.text) for old, new in changes.reworded] == [("beta", "| beta | Keep beta now. |")]


def test_a_new_file_has_no_base_rows() -> None:
    assert deleted_rows("", BASE_DOC) == []


def test_the_overage_report_lists_files_largest_delta_first_then_the_total() -> None:
    counts = [("small.md", 100, 101), ("big.md", 50, 80), ("same.md", 10, 10)]
    lines = overage_report("set", counts, 150).splitlines()
    assert lines[0] == "set: 191 words, ceiling 150, over by 41"
    assert [line.split(":")[0].strip() for line in lines[1:4]] == ["big.md", "small.md", "same.md"]
    assert lines[1] == "  big.md: base 50, now 80, delta +30"
    assert lines[-1] == "total overage: 41 words"


def test_a_budget_over_its_ceiling_fails_with_the_per_file_report(capsys: pytest.CaptureFixture[str]) -> None:
    counts = [("a.md", 5, 9), ("b.md", 4, 4)]
    with pytest.raises(AssertionError, match=r"a\.md: base 5, now 9, delta \+4") as failure:
        check_budget("pair", counts, 10)
    assert "total overage: 3 words" in str(failure.value)
    assert "total overage: 3 words" in capsys.readouterr().out


def test_a_budget_within_its_ceiling_passes() -> None:
    check_budget("pair", [("a.md", 5, 9), ("b.md", 4, 4)], 13)


def test_a_deleted_rule_row_fails_the_check_and_names_file_and_row() -> None:
    new = {"doc.md": BASE_DOC.replace(SECOND_RULE, "")}
    with pytest.raises(AssertionError, match=r"doc\.md: second rule stay") as failure:
        assert_no_deleted_rows({"doc.md": BASE_DOC}, new)
    assert "Second rule: stay." in str(failure.value)


def test_an_allowed_identity_passes_and_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    new = {"doc.md": BASE_DOC.replace(SECOND_RULE, "")}
    assert_no_deleted_rows({"doc.md": BASE_DOC}, new, {"second rule stay": "moved by the reviewed ledger"})
    assert "moved by the reviewed ledger" in capsys.readouterr().out


def test_a_row_moved_between_documents_passes_and_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    base = {"a.md": BASE_DOC, "b.md": "# B\n"}
    new = {"a.md": BASE_DOC.replace(BETA_ROW, ""), "b.md": "# B\n\n" + BETA_ROW.replace("beta", "beta")}
    assert_no_deleted_rows(base, new)
    assert "moved a.md: beta" in capsys.readouterr().out


def test_a_deleted_prose_sentence_is_printed_and_never_fails(capsys: pytest.CaptureFixture[str]) -> None:
    new = {"doc.md": BASE_DOC.replace(" Prose sentence two.", "")}
    assert_no_deleted_rows({"doc.md": BASE_DOC}, new)
    assert "Prose sentence two" in capsys.readouterr().out


def test_without_a_base_the_row_check_does_nothing() -> None:
    assert_no_deleted_rows({}, {"doc.md": ""})


def test_the_base_is_the_merge_base_else_head_else_none() -> None:
    assert base_ref(lambda *args: "abc123\n" if args[0] == "merge-base" else None) == "abc123"
    assert base_ref(lambda *args: None if args[0] == "merge-base" else "def456\n") == "HEAD"
    assert base_ref(lambda *args: None) is None


def test_the_trimmed_documents_delete_no_rule_row_against_the_base() -> None:
    """Local runs compare with the fork point; in CI there is no origin/main, so nothing is deleted."""
    base = base_snapshot()
    new = {name: document(name).read_text(encoding="utf-8") for name in shipped_names()}
    assert_no_deleted_rows(base, new)
