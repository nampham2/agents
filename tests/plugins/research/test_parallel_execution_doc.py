"""Structural checks on the parallel-execution design documents.

The design has been through two expert reviews. Both found claims about the existing code that were
wrong — revision 1 proposed adding a cross-process lock that had been in `workspace_lib.py` all
along — so revision 2 gave every such claim a citation naming a file, a line range, and the literal
text expected inside it. Review 02 then observed that the checker enforcing those citations lived
only in the author's workspace, which makes a documented check unreproducible by anyone reading the
branch. It lives here now, and runs whenever the suite does.

The checks are functions over document text and a repository root, so the same code runs against the
real documents and against deliberately mutated copies. Every check has a mutation that must break
it: a documentation check that cannot be made to fail proves nothing about the documentation.

This module deliberately imports nothing from the shipped scripts package. It is a check on prose,
not on `workspace_lib`, and keeping it out of that package keeps a prose checker clear of the
coverage gate that guards shipped code.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.conftest import REPO_ROOT

REFERENCE = REPO_ROOT / "plugins/research/skills/project/references/parallel-execution.md"
DECISIONS = REPO_ROOT / "docs/parallel-execution-decisions.md"

CITATION_ROW = re.compile(
    r"^\|\s*(?P<id>C\d+)\s*\|\s*`(?P<path>[^`:]+):(?P<start>\d+)-(?P<end>\d+)`\s*\|\s*"
    r"`(?P<needle>[^`]+)`\s*\|",
    re.MULTILINE,
)
SINGLE_LINE_CITATION_ROW = re.compile(
    r"^\|\s*(?P<id>C\d+)\s*\|\s*`(?P<path>[^`:]+):(?P<start>\d+)`\s*\|\s*`(?P<needle>[^`]+)`\s*\|",
    re.MULTILINE,
)

# Sections the reference must carry. A renamed section is a broken cross-reference somewhere.
REFERENCE_SECTIONS = (
    "## 1. Scope and objective",
    "## 2. What the measurement says, before any design",
    "## 3. What already exists",
    "## 4. Architecture",
    "## 5. The execution store",
    "### 5.2 Record schemas",
    "### 5.3 Schema versioning and reader compatibility",
    "### 5.4 Durability classes",
    "### 5.5 Retention",
    "## 6. Attempts",
    "### 6.2 The transition table",
    "### 6.4 The execution contract object",
    "### 6.5 Deriving contract fields that canonical tasks do not carry",
    "## 7. Publication and acceptance",
    "### 7.1 The publication protocol",
    "## 8. Scheduling",
    "### 8.3 The acquisition policy",
    "### 8.5 Cross-project targets",
    "## 9. Filesystem modes",
    "## 10. The worker contract",
    "## 11. Verification: method, actor, capture, adequacy",
    "### 11.4 The adequacy comparison",
    "## 12. Coordinator identity and lock order",
    "### 12.2 Lock order",
    "## 13. The recovery contract",
    "## 14. Failure semantics and the bounded observation period",
    "## 15. Timing and measurement",
    "## 16. Authoring wider plans",
    "## 17. Host adapter contracts",
    "## 18. What this design does not enforce",
    "## 19. Implementation stages",
    "## 20. Verification and benchmark plan",
    "## 21. Citations",
)

DECISIONS_SECTIONS = (
    "## Revision history",
    "## The measurement error that produced revision 1",
    "## Review 01 — disposition",
    "## Review 02 — disposition",
    "### Four points answered rather than adopted",
    "## Rejected, deferred and revisited",
    "## What is still unmeasured",
)

# Every artifact review 02 asked for, and every rule it asked to be stated, reduced to a phrase that
# cannot survive the rule being dropped.
REFERENCE_ELEMENTS = (
    "DISPATCHING",
    "never authorizes redispatch",
    "A receipt contains no revision",
    "An identical repeated publication is success",
    "A differing publication for the same attempt is rejected",
    "Incomplete publication is distinguishable",
    "unreadable, not absent",
    "schema_version",
    "Acquisition is upfront and total; there are no upgrades",
    "no-upgrade",
    "ownership_generation",
    "coordinator_run",
    "A superseded coordinator cannot act",
    "Lock order",
    "authorization_scope",
    "A matching hash never authorizes an action",
    "contract.derivation",
    "`method`",
    "`actor`",
    "`capture`",
    "structured-assessment",
    "process-record",
    "subject_digests",
    "Retention",
    "verified confinement capability",
    "Staging is deferred",
    "inline executor",
    "The protocol does not start",
    "conflict matrix",
    "case-folded",
    "PROTECTED",
    "ascending",
    "monotonic clock",
    "the last activation prerequisite",
)

ATTEMPT_STATES = (
    "PREPARED",
    "DISPATCHING",
    "DISPATCHED",
    "RUNNING",
    "RESULT_READY",
    "VERIFIED",
    "INTEGRATED",
    "UNCERTAIN",
    "LAUNCH_FAILED",
    "ABANDONED",
    "RECONCILED",
    "QUARANTINED",
)

# States that must be left by at least one transition row. Terminal states are excluded by design.
NON_TERMINAL_STATES = (
    "PREPARED",
    "DISPATCHING",
    "DISPATCHED",
    "RUNNING",
    "RESULT_READY",
    "VERIFIED",
    "UNCERTAIN",
    "QUARANTINED",
)

TRANSITION_ROW = re.compile(r"^\|\s*(T\d+)\s*\|", re.MULTILINE)
UNENFORCED_ROW = re.compile(r"^\|\s*(U\d+)\s*\|", re.MULTILINE)
MIN_TRANSITION_ROWS = 25
MIN_UNENFORCED_LABELS = 10
MIN_UNENFORCED_ROWS = 10

# Claims the reference withdrew. They may be discussed in the decision record, which is why the two
# documents are checked against different prohibitions rather than one shared list.
WITHDRAWN_FROM_REFERENCE = (
    "seven high-priority",
    "2-4x",
    "no cross-process locking",
    "the critical section is short",
    "reported in UTC",
    "Nothing to isolate",
    "Strongest",
    "Weakest",
    "worker-reported",
)

# History the reference must not carry, now that the decision record exists.
HISTORY_OUT_OF_REFERENCE = (
    "## Revision history",
    "Revision 1 ",
    "## Rejected, deferred",
    "review 01 asked",
)

# Component names retired with revision 1. They stay legitimate in the decision record's history,
# and in the few reference sections that explain why an alternative was rejected, so the prohibition
# is section-scoped rather than document-wide: what it forbids is the reference proposing one again.
RETIRED_COMPONENTS = ("SQLite", "queue.db", "wave-synchronous")
RETIRED_OK_SECTIONS = (
    "<preamble>",
    "## 3. What already exists",
    "## 4. Architecture",
    "## 19. Implementation stages",
)


def _section_at(text: str, index: int) -> str:
    """The nearest `##`/`###` heading at or before `index`, or a preamble marker."""
    heading = "<preamble>"
    for match in re.finditer(r"^#{2,3} .*$", text, re.MULTILINE):
        if match.start() > index:
            break
        heading = match.group(0)
    return heading


def check_citations(reference: str, repo_root: Path) -> "list[str]":
    """Every cited range must exist and contain its expected literal text."""
    findings: list[str] = []
    body = reference.split("## 21. Citations", 1)
    if len(body) != 2:
        return ["reference has no citation section"]
    table = body[1]
    rows = [(m, int(m.group("start")), int(m.group("end"))) for m in CITATION_ROW.finditer(table)]
    rows += [
        (m, int(m.group("start")), int(m.group("start")))
        for m in SINGLE_LINE_CITATION_ROW.finditer(table)
    ]
    if not rows:
        return ["citation table parsed to zero rows"]
    for match, start, end in rows:
        cid = match.group("id")
        source = repo_root / match.group("path")
        if not source.is_file():
            findings.append(f"{cid}: no such file {match.group('path')}")
            continue
        lines = source.read_text(encoding="utf-8").splitlines()
        if end > len(lines) or start < 1 or start > end:
            findings.append(f"{cid}: range {start}-{end} outside {match.group('path')}")
            continue
        window = "\n".join(lines[start - 1 : end])
        if match.group("needle") not in window:
            findings.append(f"{cid}: {match.group('needle')!r} not in {start}-{end}")
    return findings


def check_sections(text: str, required: "tuple[str, ...]", label: str) -> "list[str]":
    return [f"{label} is missing {heading!r}" for heading in required if heading not in text]


def check_elements(reference: str) -> "list[str]":
    return [f"reference is missing {element!r}" for element in REFERENCE_ELEMENTS
            if element not in reference]


def check_transition_table(reference: str) -> "list[str]":
    """Every state must appear, every non-terminal state must be left, and the table must be whole."""
    findings: list[str] = []
    rows = TRANSITION_ROW.findall(reference)
    if len(rows) < MIN_TRANSITION_ROWS:
        findings.append(f"transition table has {len(rows)} rows, expected {MIN_TRANSITION_ROWS}")
    if len(set(rows)) != len(rows):
        findings.append("transition table has duplicate row ids")
    for state in ATTEMPT_STATES:
        if state not in reference:
            findings.append(f"attempt state {state} is not documented")
    table = reference.split("### 6.2 The transition table", 1)
    if len(table) != 2:
        findings.append("reference has no transition table section")
        return findings
    rows_text = table[1].split("\n## ", 1)[0]
    for state in NON_TERMINAL_STATES:
        if not re.search(rf"^\|[^|]*\|[^|]*\|[^|]*\b{state}\b", rows_text, re.MULTILINE):
            findings.append(f"no transition leaves {state}")
    return findings


def check_unenforced(reference: str) -> "list[str]":
    findings: list[str] = []
    labels = reference.count("**[UNENFORCED]**")
    if labels < MIN_UNENFORCED_LABELS:
        findings.append(f"{labels} [UNENFORCED] labels, expected {MIN_UNENFORCED_LABELS}")
    rows = UNENFORCED_ROW.findall(reference)
    if len(rows) < MIN_UNENFORCED_ROWS:
        findings.append(f"{len(rows)} unenforced-table rows, expected {MIN_UNENFORCED_ROWS}")
    return findings


def check_withdrawn(reference: str) -> "list[str]":
    """Withdrawn claims must be gone from the reference, and retired components section-scoped."""
    findings: list[str] = []
    for claim in WITHDRAWN_FROM_REFERENCE:
        if claim in reference:
            findings.append(f"reference still asserts withdrawn claim {claim!r}")
    for heading in HISTORY_OUT_OF_REFERENCE:
        if heading in reference:
            findings.append(f"reference still carries history {heading!r}")
    for component in RETIRED_COMPONENTS:
        for match in re.finditer(re.escape(component), reference):
            section = _section_at(reference, match.start())
            if section not in RETIRED_OK_SECTIONS:
                findings.append(f"retired component {component!r} named under {section!r}")
    return findings


def check_benchmark_tense(reference: str) -> "list[str]":
    """§2's figures are not reproducible from this repository yet, and must not claim to be."""
    findings: list[str] = []
    if "will be committed" not in reference:
        findings.append("reference does not state the benchmark is a future commitment")
    if "not yet reproducible in this repository" not in reference:
        findings.append("reference does not disclaim present reproducibility")
    return findings


def check_decisions_record(decisions: str) -> "list[str]":
    """The corrections about the design's own record belong here, and must be stated."""
    findings: list[str] = []
    required = (
        "not seven",
        "medium",
        "measure-the-shape-before-the-engine",
        "answered rather than adopted",
        "The benchmark does not ship in this branch",
    )
    for phrase in required:
        if phrase not in decisions:
            findings.append(f"decision record is missing {phrase!r}")
    for number in range(1, 11):
        if not re.search(rf"^\|\s*{number}\s*\|", decisions, re.MULTILINE):
            findings.append(f"review 02 finding {number} has no disposition row")
    return findings


def check_cross_links(reference: str, decisions: str) -> "list[str]":
    findings: list[str] = []
    if "parallel-execution-decisions.md" not in reference:
        findings.append("reference does not link the decision record")
    if "references/parallel-execution.md" not in decisions:
        findings.append("decision record does not link the reference")
    return findings


CHECKS = (
    ("citations", lambda ref, dec, root: check_citations(ref, root)),
    ("reference sections",
     lambda ref, dec, root: check_sections(ref, REFERENCE_SECTIONS, "reference")),
    ("decision sections",
     lambda ref, dec, root: check_sections(dec, DECISIONS_SECTIONS, "decision record")),
    ("elements", lambda ref, dec, root: check_elements(ref)),
    ("transitions", lambda ref, dec, root: check_transition_table(ref)),
    ("unenforced", lambda ref, dec, root: check_unenforced(ref)),
    ("withdrawn", lambda ref, dec, root: check_withdrawn(ref)),
    ("benchmark tense", lambda ref, dec, root: check_benchmark_tense(ref)),
    ("decision record", lambda ref, dec, root: check_decisions_record(dec)),
    ("cross links", lambda ref, dec, root: check_cross_links(ref, dec)),
)


def run_checks(reference: str, decisions: str, repo_root: Path) -> "dict[str, list[str]]":
    return {name: check(reference, decisions, repo_root) for name, check in CHECKS}


# One mutation per check, each expressed as a substitution on whichever document the check reads.
MUTATIONS = (
    ("citations", "reference", "workspace_lib.py:296-316", "workspace_lib.py:1-2"),
    ("citations", "reference", "`os.fsync(handle.fileno())`", "`this text is not in the source`"),
    ("reference sections", "reference", "## 21. Citations", "## 21. References"),
    ("decision sections", "decisions", "## Revision history", "## Changes"),
    ("elements", "reference", "A receipt contains no revision", "A receipt carries the revision"),
    ("elements", "reference", "no-upgrade", "upgrade-on-demand"),
    ("transitions", "reference", "| T25 |", "| T24 |"),
    ("unenforced", "reference", "**[UNENFORCED]**", "Note:"),
    ("withdrawn", "reference", "no strength ranking here",
     "the ranking is Strongest to Weakest"),
    ("withdrawn", "reference", "## 2. What the measurement says, before any design",
     "## Revision history\n\nRevision 1 said other things.\n\n## 2. What the measurement says, "
     "before any design"),
    ("benchmark tense", "reference", "not yet reproducible in this repository",
     "already reproducible in this repository"),
    ("decision record", "decisions", "not seven", "and seven"),
    ("cross links", "reference", "parallel-execution-decisions.md", "somewhere-else.md"),
)


class ParallelExecutionDocTest(unittest.TestCase):
    """The shipped documents pass every check, and every check can be made to fail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = REFERENCE.read_text(encoding="utf-8")
        cls.decisions = DECISIONS.read_text(encoding="utf-8")

    def test_documents_exist(self) -> None:
        self.assertTrue(REFERENCE.is_file(), f"{REFERENCE} is missing")
        self.assertTrue(DECISIONS.is_file(), f"{DECISIONS} is missing")

    def test_documents_pass_every_check(self) -> None:
        results = run_checks(self.reference, self.decisions, REPO_ROOT)
        failures = {name: findings for name, findings in results.items() if findings}
        self.assertEqual({}, failures)

    def test_every_check_has_a_mutation(self) -> None:
        covered = {name for name, _, _, _ in MUTATIONS}
        self.assertEqual({name for name, _ in CHECKS}, covered)

    def test_each_mutation_is_detected(self) -> None:
        for check_name, document, old, new in MUTATIONS:
            with self.subTest(check=check_name, old=old[:40]):
                source = self.reference if document == "reference" else self.decisions
                self.assertIn(old, source, f"mutation anchor {old!r} no longer present")
                mutated = source.replace(old, new)
                self.assertNotEqual(source, mutated)
                reference = mutated if document == "reference" else self.reference
                decisions = mutated if document == "decisions" else self.decisions
                findings = run_checks(reference, decisions, REPO_ROOT)[check_name]
                self.assertTrue(findings, f"{check_name} did not notice {old!r} -> {new!r}")


if __name__ == "__main__":
    unittest.main()
