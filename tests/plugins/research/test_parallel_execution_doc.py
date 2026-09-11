"""Relationship checks on the parallel-execution design documents.

Two expert reviews found claims about existing code that were wrong, so revision 2 gave every such
claim a citation naming a file, a line range, and literal text expected inside it, and revision 3
moved the checker enforcing them out of the author's workspace and into this suite. Review 03 then
showed the checker through three mutations it did not notice: deleting a citation row while the text
still cited it, changing a canonical transition in §6.2 to one `TASK_TRANSITIONS` forbids, and
pointing the companion-document link at a path that does not exist. All three passed, because the
checks were mostly *phrase presence* — a required string is still present after the relationship it
described has been broken.

So this module checks relationships instead:

* every `[Cn]` used resolves to a §22 row, every row is used, ids are unique and contiguous, and each
  cited range is re-read from the named source and must still contain its needle;
* every Markdown link resolves on disk;
* every `§N` and `§N.M` mentioned resolves to a heading;
* every canonical status, effect kind, authorization status and constant named here is read out of
  `workspace_lib.py`, and every canonical transition in §6.2 is one `TASK_TRANSITIONS` permits;
* §6.1's label table is a bijection, agrees with its own prose, and defines every label §6.2 uses;
* §6.2's and §18's row ids are unique and contiguous, and every id cited outside its table resolves;
* every finding id of every review file present has a disposition row in the decision record.

The checks are functions over document text and a repository root, so the same code runs against the
real documents and against deliberately mutated copies. Every check carries at least one mutation that
must break it, and the three mutations review 03 reported passing are among them: a check that cannot
be made to fail proves nothing about the documentation.

A green run means this document is internally consistent, its citations are real, and its state
machine agrees with the canonical constants. It says nothing about whether the protocol is correct.

This module deliberately imports nothing from the shipped scripts package: it reads `workspace_lib.py`
as text, so a prose checker stays clear of the coverage gate that guards shipped code.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.conftest import REPO_ROOT

REFERENCE = REPO_ROOT / "plugins/research/skills/project/references/parallel-execution.md"
DECISIONS = REPO_ROOT / "docs/parallel-execution-decisions.md"
WORKSPACE_LIB = REPO_ROOT / "plugins/research/skills/project/scripts/workspace_lib.py"

CITATIONS_HEADING = "## 22. Citations"
LABEL_SECTION = "### 6.1 Labels derived from the journal"
TRANSITION_SECTION = "### 6.2 Transitions"
UNENFORCED_SECTION = "## 18. Unenforced rules"

# `| C7 | `path` | 180-184 | `needle` |`, with a single line number also accepted.
CITATION_ROW = re.compile(
    r"^\|\s*(?P<id>C\d+)\s*\|\s*`(?P<path>[^`]+)`\s*\|\s*(?P<start>\d+)(?:-(?P<end>\d+))?\s*\|"
    r"\s*`(?P<needle>[^`]+)`\s*\|",
    re.MULTILINE,
)
CITATION_USE = re.compile(r"\[(C\d+)\]")
HEADING = re.compile(r"^#{2,3} (?P<major>\d+)(?:\.(?P<minor>\d+))?\.? ", re.MULTILINE)
SECTION_REF = re.compile(r"§(\d+)(?:\.(\d+))?")
MD_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
LABEL_ROW = re.compile(r"^\|\s*(?P<n>\d+)\s*\|\s*`(?P<label>[A-Z_]+)`\s*\|", re.MULTILINE)
TRANSITION_ROW = re.compile(r"^\|\s*(T\d+)\s*\|", re.MULTILINE)
UNENFORCED_ROW = re.compile(r"^\|\s*(U\d+)\s*\|", re.MULTILINE)
T_USE = re.compile(r"`?\bT(\d+)\b`?")
U_USE = re.compile(r"`?\bU(\d+)\b`?")
CANONICAL_CELL = re.compile(r"\b([A-Z]+)\s*→\s*([A-Z]+)\b")

# Sections the reference must carry. A renamed section breaks a cross-reference somewhere.
REFERENCE_SECTIONS = (
    "## 1. Scope and objective",
    "## 2. What the measurement says, before any design",
    "## 3. What already exists",
    "## 4. Architecture",
    "## 5. The execution store",
    "### 5.2 The journal, and the two predicates derived from it",
    "### 5.5 Schema versions, the canonical gate, and migration",
    "## 6. Attempts",
    LABEL_SECTION,
    TRANSITION_SECTION,
    "### 6.3 Crash prefixes and finalization",
    "### 6.6 Derivation, and the three identity sets",
    "## 7. Publication and acceptance",
    "### 7.3 The publication classifier, and the dispatch gate",
    "## 8. Scheduling",
    "### 8.1 Admissibility",
    "### 8.5 The workspace execution registry",
    "## 9. Filesystem modes",
    "## 10. The worker contract",
    "## 11. Verification",
    "### 11.5 Per-check adequacy",
    "### 11.6 The checks the coordinator runs itself",
    "## 12. Coordinator ownership and fencing",
    "### 12.2 Fencing canonical commits",
    "### 12.3 Fencing physical dispatch",
    "## 13. Recovery",
    "### 13.3 Reconciling partial outputs",
    "## 14. Failure semantics",
    "### 14.1 Stop requests are not execution failures, and withdrawal is coherent",
    "## 15. Timing and observability",
    "## 16. Authoring plans that can actually run in parallel",
    "## 17. Host adapter contracts",
    UNENFORCED_SECTION,
    "## 19. Implementation stages",
    "## 20. What is verified, and what is not",
    "## 21. Deferred and out of scope",
    CITATIONS_HEADING,
)

DECISIONS_SECTIONS = (
    "## Revision history",
    "## What was wrong with revision 1, established from the commit",
    "## Review 01 — disposition",
    "## Review 02 — disposition",
    "## Review 03 — disposition",
    "### Three recommendations modified rather than adopted as written",
    "### The thirty-one smaller issues",
    "## Three defects the walkthrough found, that no review named",
    "## Conditions for reconsidering a database and an MCP transport",
    "## Rejected, deferred and revisited",
    "## What is still unmeasured",
)

# Canonical names the reference asserts exist. Read out of `workspace_lib.py`, never assumed.
CANONICAL_CONSTANTS = (
    "TASK_STATUSES",
    "TASK_TRANSITIONS",
    "EFFECT_KINDS",
    "AUTHORIZATION_STATUSES",
    "REFERENCE_ROOTS",
    "IMMUTABLE_PROJECT_FIELDS",
)

# The only effect kinds a subagent may be handed. A `destructive` or `external` effect leaves the
# workspace, so §8.1's `delegable` must name exactly these two: naming more delegates an effect the
# coordinator was supposed to keep, naming fewer makes a whole class undelegatable by accident.
CONFINED_EFFECT_KINDS = frozenset({"none", "local_write"})

# Words that read like an authorization status and are not one. R3-05: revision 3 wrote `revoked`,
# which validation would reject, and no phrase check could have noticed.
NOT_AUTHORIZATION_STATUSES = ("revoked", "granted", "authorized", "expired", "rescinded")

# Claims the reference withdrew. Legitimate in the decision record, which is why the two documents are
# checked against different prohibitions rather than one shared list.
WITHDRAWN_FROM_REFERENCE = (
    "seven high-priority",
    "2-4x",
    "no cross-process locking",
    "the critical section is short",
    "Nothing to isolate",
    "any unresolved |",
)

# History the reference must not carry, now that the decision record exists.
HISTORY_OUT_OF_REFERENCE = ("## Revision history", "## Rejected, deferred", "review 01 asked")

# Components retired with revision 1. Legitimate in the decision record's history and in the few
# reference sections explaining why an alternative was rejected, so the prohibition is section-scoped:
# what it forbids is the reference proposing one again.
RETIRED_COMPONENTS = ("SQLite", "queue.db", "wave-synchronous")
RETIRED_OK_SECTIONS = (
    "<preamble>",
    "## 3. What already exists",
    "## 4. Architecture",
    "## 19. Implementation stages",
    "## 21. Deferred and out of scope",
)

# review file -> (decision-record section, patterns yielding the ids that need a disposition)
REVIEW_FILES = {
    "docs/parallel-execution-review.md": (
        "## Review 01 — disposition",
        (re.compile(r"^### (\d+)\.", re.MULTILINE),),
    ),
    "docs/parallel-execution-review-02.md": (
        "## Review 02 — disposition",
        (re.compile(r"^### (\d+)\.", re.MULTILINE),),
    ),
    "docs/parallel-execution-review-03.md": (
        "## Review 03 — disposition",
        (re.compile(r"^## (R3-\d+)", re.MULTILINE), re.compile(r"\b(S\d\d)\b")),
    ),
}


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------


def _section_at(text: str, index: int) -> str:
    heading = "<preamble>"
    for match in re.finditer(r"^#{2,3} .*$", text, re.MULTILINE):
        if match.start() > index:
            break
        heading = match.group(0)
    return heading


def _slice(text: str, heading: str) -> str:
    """The text under `heading`, up to the next heading of the same or shallower depth."""
    start = text.find(heading)
    if start < 0:
        return ""
    depth = len(heading) - len(heading.lstrip("#"))
    body = text[start + len(heading):]
    for match in re.finditer(r"^#{1,6} ", body, re.MULTILINE):
        if len(match.group(0)) - 1 <= depth:
            return body[: match.start()]
    return body


def _canonical_literal(name: str) -> object:
    """Read one module-level constant out of `workspace_lib.py` without importing it."""
    tree = ast.parse(WORKSPACE_LIB.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise KeyError(name)


def _canonical_set(name: str) -> "set[str]":
    value = _canonical_literal(name)
    assert isinstance(value, (set, frozenset, tuple, list)), name
    return {str(item) for item in value}


def _canonical_transitions() -> "dict[str, set[str]]":
    value = _canonical_literal("TASK_TRANSITIONS")
    assert isinstance(value, dict), "TASK_TRANSITIONS"
    return {str(key): {str(item) for item in destinations} for key, destinations in value.items()}


def _heading_numbers(reference: str) -> "set[str]":
    numbers = set()
    for match in HEADING.finditer(reference):
        numbers.add(match.group("major"))
        if match.group("minor"):
            numbers.add(f"{match.group('major')}.{match.group('minor')}")
    return numbers


def _contiguous(ids: "list[str]", prefix: str, label: str) -> "list[str]":
    findings: list[str] = []
    if not ids:
        return [f"{label}: no rows parsed"]
    if len(set(ids)) != len(ids):
        seen: set[str] = set()
        duplicates = sorted({i for i in ids if i in seen or seen.add(i)})  # type: ignore[func-returns-value]
        findings.append(f"{label}: duplicate ids {duplicates}")
    numbers = sorted(int(i[len(prefix):]) for i in set(ids))
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(f"{label}: ids are not contiguous from {prefix}1: {numbers}")
    return findings


# --------------------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------------------


def check_citations(reference: str, repo_root: Path) -> "list[str]":
    """Rows resolve to real text, every use resolves to a row, and every row is used.

    The first R3-17 mutation deleted C1's row while the body still wrote `[C1]`. Re-reading ranges
    cannot notice that; referential integrity in both directions can.
    """
    findings: list[str] = []
    table = _slice(reference, CITATIONS_HEADING)
    if not table:
        return ["reference has no citation section"]
    rows = list(CITATION_ROW.finditer(table))
    if not rows:
        return ["citation table parsed to zero rows"]
    ids = [match.group("id") for match in rows]
    findings += _contiguous(ids, "C", "citation table")

    for match in rows:
        cid = match.group("id")
        source = repo_root / match.group("path")
        start = int(match.group("start"))
        end = int(match.group("end") or match.group("start"))
        if not source.is_file():
            findings.append(f"{cid}: no such file {match.group('path')}")
            continue
        lines = source.read_text(encoding="utf-8").splitlines()
        if start < 1 or start > end or end > len(lines):
            findings.append(f"{cid}: range {start}-{end} outside {match.group('path')}")
            continue
        if match.group("needle") not in "\n".join(lines[start - 1: end]):
            findings.append(f"{cid}: {match.group('needle')!r} not in {start}-{end}")

    body = reference[: reference.find(CITATIONS_HEADING)]
    used = set(CITATION_USE.findall(body))
    defined = set(ids)
    for cid in sorted(used - defined):
        findings.append(f"{cid} is cited in the body and has no row")
    for cid in sorted(defined - used):
        findings.append(f"{cid} has a row and is never cited")
    return findings


def check_links(reference: str, decisions: str, repo_root: Path) -> "list[str]":
    """Every Markdown link target resolves on disk, relative to its own document.

    The third R3-17 mutation repointed the companion link at `../../../../../missing/`. The old check
    asked only whether the filename appeared in the text.
    """
    findings: list[str] = []
    for name, text, directory in (
        ("reference", reference, REFERENCE.parent),
        ("decision record", decisions, DECISIONS.parent),
    ):
        targets = {match.group("target") for match in MD_LINK.finditer(text)}
        if not targets:
            findings.append(f"{name} has no Markdown links at all")
        for target in sorted(targets):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (directory / target.split("#", 1)[0]).resolve()
            if not path.is_file():
                findings.append(f"{name}: link target does not resolve: {target}")
            elif repo_root.resolve() not in path.parents:
                findings.append(f"{name}: link target leaves the repository: {target}")
    return findings


def check_section_refs(reference: str) -> "list[str]":
    """Every `§N` and `§N.M` in the prose resolves to a heading in this document."""
    numbers = _heading_numbers(reference)
    if not numbers:
        return ["no numbered headings parsed"]
    findings: list[str] = []
    for match in SECTION_REF.finditer(reference):
        ref = match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}"
        if ref not in numbers:
            findings.append(f"§{ref} does not resolve to a heading")
    return sorted(set(findings))


def check_canonical_enums(reference: str) -> "list[str]":
    """Every canonical name and value the reference asserts is read out of the validator."""
    findings: list[str] = []
    source = WORKSPACE_LIB.read_text(encoding="utf-8")
    for name in CANONICAL_CONSTANTS:
        if name not in reference:
            findings.append(f"reference never names canonical constant {name}")
        if not re.search(rf"^{name}\s*[:=]", source, re.MULTILINE):
            findings.append(f"{name} is not defined in workspace_lib.py")

    effect_kinds = _canonical_set("EFFECT_KINDS")
    statuses = _canonical_set("AUTHORIZATION_STATUSES")

    delegable = re.search(r"effect\.kind ∈ \{([^}]*)\}", reference)
    if not delegable:
        findings.append("§8.1 has no delegable predicate naming effect kinds")
    else:
        named = {kind.strip() for kind in delegable.group(1).split(",") if kind.strip()}
        for kind in sorted(named - effect_kinds):
            findings.append(f"delegable names {kind!r}, which is not in EFFECT_KINDS")
        for kind in sorted(named - CONFINED_EFFECT_KINDS):
            findings.append(
                f"delegable admits {kind!r}, whose effects are not confined to the workspace"
            )
        for kind in sorted(CONFINED_EFFECT_KINDS - named):
            findings.append(f"delegable excludes {kind!r}, which nothing else may delegate")

    for status in sorted(statuses):
        if f"`{status}`" not in reference:
            findings.append(f"authorization status {status!r} is never named in the reference")
    for word in NOT_AUTHORIZATION_STATUSES:
        if re.search(rf"`{word}`", reference):
            findings.append(f"reference uses `{word}` as if it were an authorization status")
    return findings


def check_canonical_transitions(reference: str) -> "list[str]":
    """Every `X → Y` in §6.2's canonical column is permitted by `TASK_TRANSITIONS`.

    The second R3-17 mutation changed a canonical cell to `DONE → TODO`, which the validator forbids.
    """
    findings: list[str] = []
    table = _slice(reference, TRANSITION_SECTION)
    if not table:
        return ["reference has no transition table section"]
    transitions = _canonical_transitions()
    statuses = _canonical_set("TASK_STATUSES")
    pairs = set()
    for line in table.splitlines():
        if not line.startswith("|") or not TRANSITION_ROW.match(line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            findings.append(f"transition row has {len(cells)} cells: {line[:60]}")
            continue
        for source, destination in CANONICAL_CELL.findall(cells[4]):
            pairs.add((source, destination))
    if not pairs:
        findings.append("no canonical transitions parsed from §6.2")
    for source, destination in sorted(pairs):
        if source not in statuses:
            findings.append(f"§6.2 names canonical status {source!r}, absent from TASK_STATUSES")
        elif destination not in transitions.get(source, set()):
            findings.append(
                f"§6.2 claims {source} → {destination}, which TASK_TRANSITIONS forbids"
            )
    return findings


def check_label_table(reference: str) -> "list[str]":
    """§6.1 is a bijection, agrees with its own prose, and defines every label §6.2 uses."""
    findings: list[str] = []
    section = _slice(reference, LABEL_SECTION)
    if not section:
        return ["reference has no label section"]
    rows = LABEL_ROW.findall(section)
    if not rows:
        return ["label table parsed to zero rows"]
    numbers = [int(n) for n, _ in rows]
    labels = [label for _, label in rows]
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(f"label rows are not contiguous from 1: {numbers}")
    if len(set(labels)) != len(labels):
        findings.append(f"label table is not a bijection: {sorted(labels)}")

    words = {4: "Four", 9: "Nine", 12: "Twelve", 13: "Thirteen", 14: "Fourteen", 15: "Fifteen"}
    claimed = words.get(len(rows))
    if claimed is None:
        findings.append(f"no spelled-out count is known for {len(rows)} rows")
    elif f"**{claimed} rows, {claimed.lower()} labels" not in section:
        findings.append(f"prose does not claim {claimed} rows and {claimed.lower()} labels")

    prose = section.split("one row each.**", 1)
    if len(prose) != 2:
        findings.append("prose does not list the labels after the count")
    else:
        listed = set(re.findall(r"`([A-Z_]+)`", prose[1].split("\n\n", 1)[0]))
        missing = set(labels) - listed
        extra = listed - set(labels)
        if missing:
            findings.append(f"prose omits labels present in the table: {sorted(missing)}")
        if extra:
            findings.append(f"prose lists labels absent from the table: {sorted(extra)}")

    used = set()
    for line in _slice(reference, TRANSITION_SECTION).splitlines():
        if not TRANSITION_ROW.match(line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        used |= set(re.findall(r"`([A-Z_]+)`", cells[1] + " " + cells[3]))
    for label in sorted(used - set(labels)):
        findings.append(f"§6.2 uses label {label!r}, which §6.1 does not define")
    for label in sorted(set(labels) - used):
        findings.append(f"§6.1 defines label {label!r}, which no §6.2 row reaches")
    return findings


def check_row_ids(reference: str) -> "list[str]":
    """§6.2's and §18's ids are unique and contiguous, and every id cited outside resolves."""
    findings: list[str] = []
    for heading, row_pattern, use_pattern, prefix in (
        (TRANSITION_SECTION, TRANSITION_ROW, T_USE, "T"),
        (UNENFORCED_SECTION, UNENFORCED_ROW, U_USE, "U"),
    ):
        section = _slice(reference, heading)
        if not section:
            findings.append(f"reference has no {heading!r} section")
            continue
        ids = row_pattern.findall(section)
        findings += _contiguous(ids, prefix, f"{prefix} table")
        defined = {int(i[1:]) for i in ids}
        elsewhere = reference.replace(section, "")
        for number in sorted({int(n) for n in use_pattern.findall(elsewhere)}):
            if number not in defined:
                findings.append(f"{prefix}{number} is cited outside its table and has no row")
    return findings


def check_retired(reference: str) -> "list[str]":
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
    return sorted(set(findings))


def check_benchmark_honesty(reference: str) -> "list[str]":
    """§2's figures are not reproducible here yet, and the retirement is one stated edit.

    S31 asked for a retirement path, because a check demanding a disclaimer forever would have to be
    deleted alongside it. The disclaimer names the stage that retires it, and this check requires that
    naming, so retiring it is an edit the document itself authorizes rather than a silent deletion.
    """
    findings: list[str] = []
    if "not yet reproducible in this repository" not in reference:
        findings.append("reference does not disclaim present reproducibility")
    if not re.search(r"committed by Stage \d+ \(§\d+(\.\d+)?\)", reference):
        findings.append("disclaimer does not name the stage whose landing retires it")
    return findings


def check_dispositions(decisions: str, repo_root: Path) -> "list[str]":
    """Every finding id of every review file present has a row in the matching section."""
    findings: list[str] = []
    seen_any = False
    for relative, (heading, patterns) in sorted(REVIEW_FILES.items()):
        path = repo_root / relative
        if not path.is_file():
            continue
        seen_any = True
        section = _slice(decisions, heading)
        if not section:
            findings.append(f"decision record has no {heading!r} section for {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        ids: list[str] = []
        for pattern in patterns:
            ids += pattern.findall(text)
        if not ids:
            findings.append(f"{relative}: no finding ids parsed")
            continue
        for finding_id in sorted(set(ids)):
            if not re.search(rf"^\|\s*{re.escape(finding_id)}\s*\|", section, re.MULTILINE):
                findings.append(f"{relative}: {finding_id} has no disposition row")
    if not seen_any:
        findings.append("no review files found at all")
    return findings


def check_cross_links(reference: str, decisions: str) -> "list[str]":
    findings: list[str] = []
    if "parallel-execution-decisions.md" not in reference:
        findings.append("reference does not link the decision record")
    if "references/parallel-execution.md" not in decisions:
        findings.append("decision record does not link the reference")
    if "normative" not in decisions:
        findings.append("decision record does not say which document is normative")
    return findings


def check_sections(text: str, required: "tuple[str, ...]", label: str) -> "list[str]":
    return [f"{label} is missing {heading!r}" for heading in required if heading not in text]


CHECKS = (
    ("citations", lambda ref, dec, root: check_citations(ref, root)),
    ("links", lambda ref, dec, root: check_links(ref, dec, root)),
    ("section refs", lambda ref, dec, root: check_section_refs(ref)),
    ("canonical enums", lambda ref, dec, root: check_canonical_enums(ref)),
    ("canonical transitions", lambda ref, dec, root: check_canonical_transitions(ref)),
    ("label table", lambda ref, dec, root: check_label_table(ref)),
    ("row ids", lambda ref, dec, root: check_row_ids(ref)),
    ("retired", lambda ref, dec, root: check_retired(ref)),
    ("benchmark honesty", lambda ref, dec, root: check_benchmark_honesty(ref)),
    ("dispositions", lambda ref, dec, root: check_dispositions(dec, root)),
    ("cross links", lambda ref, dec, root: check_cross_links(ref, dec)),
    ("reference sections",
     lambda ref, dec, root: check_sections(ref, REFERENCE_SECTIONS, "reference")),
    ("decision sections",
     lambda ref, dec, root: check_sections(dec, DECISIONS_SECTIONS, "decision record")),
)


def run_checks(reference: str, decisions: str, repo_root: Path) -> "dict[str, list[str]]":
    return {name: check(reference, decisions, repo_root) for name, check in CHECKS}


# One mutation per check, each a substitution on whichever document the check reads. The three marked
# R3-17 are the mutations review 03 reported the previous checker passing.
MUTATIONS = (
    ("citations", "reference",
     "| C1 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1345-1352 "
     "| `does not match RUNNING tasks` |\n", "",
     "R3-17: delete C1's row while the body still cites [C1]"),
    ("citations", "reference", "| `class DirectoryLock` |", "| `class CrossProcessLock` |",
     "a needle that is not in the cited range"),
    ("citations", "reference", "| 2768-2789 |", "| 1-2 |", "a range that no longer holds its needle"),
    ("links", "reference", "](../../../../../docs/parallel-execution-decisions.md)",
     "](../../../../../missing/parallel-execution-decisions.md)",
     "R3-17: repoint the companion link at a path that does not exist"),
    ("section refs", "reference", "§20.3", "§20.9", "a dangling section reference"),
    ("canonical enums", "reference", "`deferred`", "`revoked`", "a status the validator rejects"),
    ("canonical enums", "reference", "effect.kind ∈ {none, local_write}",
     "effect.kind ∈ {none, local_write, destructive}", "an effect kind delegation must exclude"),
    ("canonical transitions", "reference", "| `RUNNING → DONE`, or `RUNNING → BLOCKED`",
     "| `DONE → TODO`, or `RUNNING → BLOCKED`",
     "R3-17: a canonical transition TASK_TRANSITIONS forbids"),
    ("label table", "reference", "| 14 | `PREPARED` |", "| 14 | `INTEGRATED` |",
     "a duplicated label, breaking the bijection"),
    ("label table", "reference", "| 13 | `DISPATCHING` |", "| 13 | `DISPATCH_PENDING` |",
     "a label §6.2 uses and §6.1 no longer defines"),
    ("row ids", "reference", "| T29 |", "| T30 |", "a gap in the transition ids"),
    ("retired", "reference", "The journal is the authority for three questions",
     "The SQLite journal is the authority for three questions",
     "a retired component proposed again outside the sections that may name it"),
    ("benchmark honesty", "reference", "committed by Stage 2 (§20.3)", "committed eventually",
     "a disclaimer with no stated retirement"),
    ("dispositions", "decisions", "| R3-09 |", "| R3-99 |", "a finding with no disposition row"),
    ("cross links", "reference", "parallel-execution-decisions.md", "somewhere-else.md",
     "a reference that no longer names its companion"),
    ("reference sections", "reference", CITATIONS_HEADING, "## 22. References", "a renamed section"),
    ("decision sections", "decisions", "## Revision history", "## Changes", "a renamed section"),
)


class ParallelExecutionDocTest(unittest.TestCase):
    """The shipped documents pass every check, and every check can be made to fail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = REFERENCE.read_text(encoding="utf-8")
        cls.decisions = DECISIONS.read_text(encoding="utf-8")

    def test_documents_exist(self) -> None:
        for path in (REFERENCE, DECISIONS, WORKSPACE_LIB):
            self.assertTrue(path.is_file(), f"{path} is missing")

    def test_documents_pass_every_check(self) -> None:
        results = run_checks(self.reference, self.decisions, REPO_ROOT)
        self.assertEqual({}, {name: found for name, found in results.items() if found})

    def test_every_check_has_a_mutation(self) -> None:
        covered = {name for name, _, _, _, _ in MUTATIONS}
        self.assertEqual({name for name, _ in CHECKS}, covered)

    def test_each_mutation_is_detected(self) -> None:
        for check_name, document, old, new, why in MUTATIONS:
            with self.subTest(check=check_name, mutation=why):
                source = self.reference if document == "reference" else self.decisions
                self.assertIn(old, source, f"mutation anchor {old!r} no longer present")
                mutated = source.replace(old, new)
                self.assertNotEqual(source, mutated)
                reference = mutated if document == "reference" else self.reference
                decisions = mutated if document == "decisions" else self.decisions
                findings = run_checks(reference, decisions, REPO_ROOT)[check_name]
                self.assertTrue(findings, f"{check_name} did not notice: {why}")

    def test_the_three_review_03_mutations_now_fail(self) -> None:
        """Named separately because they are the reason this checker was rewritten."""
        marked = [m for m in MUTATIONS if m[4].startswith("R3-17")]
        self.assertEqual(3, len(marked), "the three R3-17 mutations are not all present")
        for check_name, document, old, new, why in marked:
            with self.subTest(mutation=why):
                source = self.reference if document == "reference" else self.decisions
                mutated = source.replace(old, new)
                reference = mutated if document == "reference" else self.reference
                decisions = mutated if document == "decisions" else self.decisions
                self.assertTrue(run_checks(reference, decisions, REPO_ROOT)[check_name])


if __name__ == "__main__":
    unittest.main()
