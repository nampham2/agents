"""Relationship checks on the parallel-execution design documents.

Five expert reviews have now found claims about existing code that were wrong, so every such claim in
the reference carries a citation naming a file, a line range, and literal text expected inside it, and
this suite enforces them. Review 03 showed the previous checker three mutations it did not notice, all
of which passed because the checks were mostly *phrase presence*: a required string is still there
after the relationship it described has been broken. Review 04 then showed that the phrase-shaped
checks that remained had the same weakness — a canonical cell of unrelated prose yielded no findings, a
link fragment was discarded before resolution, and the unenforced-rule check counted markers instead of
relating each rule to the site that states it.

So every check here is a relationship, and revision 5's own structure supplies most of them:

* every `[Cn]` used resolves to a §23 row, every row is used, ids are unique and contiguous, and each
  cited range is re-read from the named source and must still contain its needle;
* every Markdown link resolves on disk **and** its fragment resolves to a heading in the target;
* every `§N` and `§N.M` mentioned resolves to a heading here;
* every canonical status, effect kind, authorization status and constant named here is read out of
  `workspace_lib.py`, and §15.1's two lock timeouts are compared against the defaults the shipped
  `DirectoryLock.__init__` and `commit_candidate` actually carry, parsed from the source;
* every canonical cell of §8.1 parses completely — a cell of unrelated prose is a finding, not a
  silence — and names a transition `TASK_TRANSITIONS` permits;
* no §8.1 precondition is stated over a §6.2 label, which is the structural half of revision 5's
  "labels gate nothing"; every name §6.2 matches on is declared in §6.1; every §6.1 fact is read
  outside §6.1;
* every refusal code has exactly one §4 row, is named outside §4, and every code used has a row;
* §8.1's and §19's ids are unique and contiguous, and every id cited outside its table resolves;
* every §19 rule has an `[UNENFORCED U<n>]` marker inside each section it names, and every marker
  belongs to a rule and stands in a section that rule names;
* every finding id of every review file present has a disposition row, and the assessed/answered
  revision pairs form an unbroken chain ending at this revision;
* §7.3's two digests differ by exactly the fields §7.4's *common* envelope lets a writer stamp, and
  §7.2's `EEXIST` branch compares the one that excludes them — the defect revision 6 found by building
  the reference model, where "equal bytes" contradicted §8.2's own completions;
* every top-level family of §7.1's store layout is accounted for by one of the document's own
  statements about what a generation fence covers, which is the other defect the model found: nothing
  fenced `control/stop-requests/`, so a durable stop request read `indeterminate` forever after a
  takeover;
* §20's phase ids are unique and contiguous, and the implementation plan carries a section per phase
  and points back at the reference.

The checks are functions over document text and a repository root, so the same code runs against the
real documents and against deliberately mutated copies. Every check carries at least one mutation that
must break it, and the mutations reviews 03 and 04 reported passing are named among them: a check that
cannot be made to fail proves nothing about the documentation.

A green run means these documents are internally consistent, their citations are real, and their state
vocabulary agrees with the canonical constants. It says nothing about whether the protocol is correct;
§21.1 of the reference gives the worked example of a citation that resolves beside a claim it does not
support.

This module deliberately imports nothing from the shipped scripts package: it reads `workspace_lib.py`
as text, so a prose checker stays clear of the coverage gate that guards shipped code.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from tests.conftest import REPO_ROOT

REFERENCE = REPO_ROOT / "plugins/research/skills/project/references/parallel-execution.md"
DECISIONS = REPO_ROOT / "docs/parallel-execution-decisions.md"
WORKSPACE_LIB = REPO_ROOT / "plugins/research/skills/project/scripts/workspace_lib.py"
PLAN = REPO_ROOT / "docs/parallel-execution-implementation-plan.md"

CITATIONS_HEADING = "## 23. Citations"
REFUSAL_SECTION = "## 4. Refusals"
STORE_SECTION = "### 7.1 Layout"
PUBLISH_SECTION = "### 7.2 Publish-if-absent"
SERIALIZATION_SECTION = "### 7.3 Canonical serialization and identifiers"
RECORD_SECTION = "### 7.4 Record schemas"
DURABILITY_SECTION = "### 7.5 Durability classes and the failure model"
JOURNAL_FENCE_SECTION = "### 12.4 Fencing journal writes"
PHASES_SECTION = "## 20. Implementation phases"
FACTS_SECTION = "### 6.1 The fact set"
LABEL_SECTION = "### 6.2 The derived label"
OPERATION_SECTION = "### 8.1 The operation table"
SETTINGS_SECTION = "### 15.1 Settings"
UNENFORCED_SECTION = "## 19. Cooperative rules the protocol does not enforce"

# `| C7 | `path` | 180-184 | `needle` |`, with a single line number also accepted.
CITATION_ROW = re.compile(
    r"^\|\s*(?P<id>C\d+)\s*\|\s*`(?P<path>[^`]+)`\s*\|\s*(?P<start>\d+)(?:-(?P<end>\d+))?\s*\|"
    r"\s*`(?P<needle>[^`]+)`\s*\|",
    re.MULTILINE,
)
CITATION_USE = re.compile(r"\[(C\d+)\]")
HEADING_LINE = re.compile(r"^#{1,6} .*$", re.MULTILINE)
HEADING = re.compile(r"^#{2,3} (?P<major>\d+)(?:\.(?P<minor>\d+))?\.? ", re.MULTILINE)
SECTION_REF = re.compile(r"§(\d+)(?:\.(\d+))?")
MD_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
LABEL_ROW = re.compile(r"^\|\s*(?P<n>\d+)\s*\|\s*`(?P<label>[A-Z_]+)`\s*\|", re.MULTILINE)
OPERATION_ROW = re.compile(r"^\|\s*(O\d+)\s*\|", re.MULTILINE)
UNENFORCED_ROW = re.compile(r"^\|\s*(U\d+)\s*\|", re.MULTILINE)
O_USE = re.compile(r"\bO(\d+)\b")
U_USE = re.compile(r"\bU(\d+)\b")
MARKER = re.compile(r"\[UNENFORCED U(\d+)\]")
REFUSAL_CODE = re.compile(r"`(R-[A-Z-]+)`")
TRANSITION_TOKEN = re.compile(r"([A-Z]+)\s*→\s*([A-Z]+)")
SETTING_ROW = re.compile(r"^\|\s*`(?P<name>[a-z_]+)`\s*\|\s*(?P<value>[^|]+?)\s*\|", re.MULTILINE)
BACKTICKED_LOWER = re.compile(r"`([a-z_]+)`")
REVISION_HEADER = re.compile(r"\*\*Revision (\d+), \d{4}-\d{2}-\d{2}\.\*\*")
REVISION_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*[`*]", re.MULTILINE)
ASSESSED = re.compile(r"Assessed against revision (\d+); answered in revision (\d+)\.")

# §7.1's ASCII tree. The indent unit is four characters (`│   ` or four spaces), so its length
# divided by four is the depth: the tree is parsed rather than pattern-matched line by line.
TREE_ENTRY = re.compile(r"^(?P<indent>(?:[│ ]   )*)(?:├──|└──) (?P<name>\S+)")
BACKTICKED = re.compile(r"`([^`]+)`")
PHASE_ROW = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)
PLAN_PHASE = re.compile(r"^## Phase (\d+)\b", re.MULTILINE)

# Sections the reference must carry: every major heading, plus the subsections this checker slices or
# another document points at. A renamed heading breaks a cross-reference somewhere.
REFERENCE_SECTIONS = (
    "## 1. The invariant ledger",
    "## 2. Foundations that already exist",
    "## 3. The safety envelope",
    "### 3.1 What v1 supports",
    "### 3.2 The measurement that sets the ceiling",
    REFUSAL_SECTION,
    "## 5. Architecture",
    "## 6. Facts, and the label derived from them",
    FACTS_SECTION,
    LABEL_SECTION,
    "### 6.3 Validity, and why absence is not a fact",
    "## 7. The execution store",
    "### 7.2 Publish-if-absent",
    "### 7.4 Record schemas",
    "### 7.6 Schema version, the canonical gate, and activation",
    "## 8. Operations",
    OPERATION_SECTION,
    "### 8.2 Crash prefixes and their completions",
    "### 8.4 The canonical candidate",
    "## 9. Resources and claims",
    "### 9.1 The execution plan",
    "### 9.2 Claim namespaces",
    "### 9.3 The conflict relation",
    "## 10. Scheduling",
    "### 10.1 Admission",
    "### 10.3 Capacity",
    "### 10.4 The execution registry",
    "## 11. Verification and acceptance",
    "### 11.3 Binding evidence to the bytes that were checked",
    "### 11.4 Adequacy and the rerun cap",
    "### 11.5 Who may assess",
    "## 12. Ownership, takeover, and fencing",
    "### 12.2 Takeover requires proof of death",
    "### 12.3 Fencing canonical mutations",
    "### 12.4 Fencing journal writes",
    "## 13. Recovery",
    "### 13.3 Reconciling partial outputs",
    "## 14. Failure semantics and the dispatch gate",
    "### 14.1 Causes",
    "## 15. Timing, volume, and observability",
    SETTINGS_SECTION,
    "## 16. The worker contract",
    "### 16.4 What a worker must not do",
    "## 17. Host adapter contracts",
    "### 17.1 The matrix",
    "### 17.3 What an adapter may not do",
    "## 18. Authoring plans that can actually run in parallel",
    UNENFORCED_SECTION,
    PHASES_SECTION,
    "## 21. What is verified, and what is not",
    "### 21.1 The documentation checks",
    "### 21.2 The reference model",
    "### 21.3 The benchmark",
    "## 22. Deferred and out of scope",
    CITATIONS_HEADING,
)

DECISIONS_SECTIONS = (
    "## Revision history",
    "## What was wrong with revision 1, established from the commit",
    "## Review 01 — disposition",
    "## Review 02 — disposition",
    "## Review 03 — disposition",
    "## Review 04 — disposition",
    "## Review 05 — disposition",
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

# The only effect kinds a task may carry into parallel execution. A `destructive` or `external` effect
# leaves the workspace, so every `effect.kind ∈ {...}` or `∉ {...}` set in the reference must name
# exactly these two: naming more admits an effect the coordinator was supposed to keep, naming fewer
# refuses a whole class by accident.
CONFINED_EFFECT_KINDS = frozenset({"none", "local_write"})

# Words that read like an authorization status and are not one. R3-05: revision 3 wrote `revoked`,
# which validation would reject. Revision 5 legitimately uses `revoked` for a launch capability, so the
# prohibition is licensed by the word `capability` on the same line rather than lifted.
NOT_AUTHORIZATION_STATUSES = ("revoked", "granted", "authorized", "expired", "rescinded")
CAPABILITY_LICENCE = "capability"

# Claims the reference withdrew. Legitimate in the decision record, which is why the two documents are
# checked against different prohibitions rather than one shared list.
WITHDRAWN_FROM_REFERENCE = (
    "seven high-priority",
    "2-4x",
    "no cross-process locking",
    "the critical section is short",
    "Nothing to isolate",
)

# History the reference must not carry, now that the decision record exists.
HISTORY_OUT_OF_REFERENCE = ("## Revision history", "## Rejected, deferred", "review 01 asked")

# Components retired with revision 1. Legitimate in the decision record's history and in the few
# reference sections explaining why an alternative was rejected, so the prohibition is section-scoped:
# what it forbids is the reference proposing one again.
RETIRED_COMPONENTS = ("SQLite", "queue.db", "wave-synchronous", "JSON-RPC")
RETIRED_OK_SECTIONS = (
    "<preamble>",
    "## 2. Foundations that already exist",
    "## 5. Architecture",
    PHASES_SECTION,
    "## 22. Deferred and out of scope",
)

# review file -> (decision-record section, patterns yielding the ids that need a disposition). The
# order is the review order, which `check_revision_chain` walks to test the assessed/answered chain.
REVIEW_FILES = (
    (
        "docs/parallel-execution-review.md",
        "## Review 01 — disposition",
        (re.compile(r"^### (\d+)\.", re.MULTILINE),),
    ),
    (
        "docs/parallel-execution-review-02.md",
        "## Review 02 — disposition",
        (re.compile(r"^### (\d+)\.", re.MULTILINE),),
    ),
    (
        "docs/parallel-execution-review-03.md",
        "## Review 03 — disposition",
        (re.compile(r"^## (R3-\d+)", re.MULTILINE), re.compile(r"\b(S\d\d)\b")),
    ),
    (
        "docs/parallel-execution-review-04.md",
        "## Review 04 — disposition",
        (re.compile(r"^## (R4-\d+)", re.MULTILINE), re.compile(r"\b(S4-\d\d)\b")),
    ),
    (
        "docs/parallel-execution-review-05.md",
        "## Review 05 — disposition",
        (re.compile(r"^### (R5-\d+)", re.MULTILINE),),
    ),
)


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


def _rows(section: str, id_pattern: "re.Pattern[str]") -> "List[List[str]]":
    """The cells of every table row in `section` whose first column matches `id_pattern`."""
    out: List[List[str]] = []
    for line in section.splitlines():
        if not line.startswith("|") or not id_pattern.match(line):
            continue
        out.append([cell.strip() for cell in line.strip("|").split("|")])
    return out


def _anchor(heading: str) -> str:
    """The GitHub fragment for a heading line, so a link fragment can be resolved rather than dropped."""
    text = heading.lstrip("#").strip().lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return text.replace(" ", "-")


def _anchors(path: Path) -> "Set[str]":
    return {_anchor(match.group(0)) for match in HEADING_LINE.finditer(path.read_text(encoding="utf-8"))}


def _module() -> ast.Module:
    return ast.parse(WORKSPACE_LIB.read_text(encoding="utf-8"))


def _canonical_literal(name: str) -> object:
    """Read one module-level constant out of `workspace_lib.py` without importing it."""
    for node in _module().body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise KeyError(name)


def _family(name: str) -> str:
    """The top-level family of a §7.1 tree entry: its first path segment, or the bare filename."""
    return name.split("/", 1)[0] + "/" if "/" in name else name


def _store_families(reference: str) -> "Dict[str, List[str]]":
    """§7.1's layout, as top-level families mapped to the families one level below them.

    The tree is parsed, not searched for phrases, so a family added to the diagram without being
    accounted for anywhere else in the document is a finding rather than a silence.
    """
    families: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in _slice(reference, STORE_SECTION).splitlines():
        match = TREE_ENTRY.match(line)
        if not match:
            continue
        depth = len(match.group("indent")) // 4
        name = match.group("name")
        if depth == 0:
            current = _family(name)
            families.setdefault(current, [])
        elif depth == 1 and current is not None:
            families[current].append(_family(name))
    return families


def _digest_inputs(reference: str, digest_name: str) -> "Set[str]":
    """The field names one §7.3 digest bullet says are removed from its input.

    The bullet is flattened and read up to its last `removed`, so the names come from the clause that
    states the exclusion rather than from anywhere else in the bullet. `sha256` is dropped because it
    is the algorithm, not a field, and the digest's own name because a digest never covers itself.
    """
    for bullet in re.split(r"\n(?=- )", _slice(reference, SERIALIZATION_SECTION)):
        flat = " ".join(bullet.split())
        if not flat.startswith(f"- `{digest_name}` is "):
            continue
        head = flat[: flat.rfind("removed")] if "removed" in flat else ""
        return set(BACKTICKED.findall(head)) - {"sha256", digest_name}
    return set()


def _envelope_fields(records: str, marker: str) -> "Set[str]":
    """The first-column field names of the table that follows `marker` in §7.4."""
    start = records.find(marker)
    if start < 0:
        return set()
    fields: Set[str] = set()
    for line in records[start:].splitlines():
        if not line.startswith("|"):
            if fields:
                break
            continue
        fields |= set(BACKTICKED.findall(line.strip("|").split("|")[0]))
    return fields


def _non_journal_families(records: str) -> "Set[str]":
    """The §7.1 families §7.4 exempts from the envelope by saying they are not journal records."""
    flat = " ".join(records.split())
    out: Set[str] = set()
    for sentence in re.split(r"(?<=\.)\s", flat):
        if "not journal records" not in sentence:
            continue
        out |= {_family(token) for token in BACKTICKED.findall(sentence)}
    return out


def _store_root_fence_bullet(reference: str) -> "Optional[str]":
    """§12.4's bullet establishing the store-root, project-scope generation fence."""
    for bullet in re.split(r"\n(?=- )", _slice(reference, JOURNAL_FENCE_SECTION)):
        if "store root" in bullet:
            return bullet
    return None


def _phase_ids(reference: str) -> "List[int]":
    return [int(number) for number in PHASE_ROW.findall(_slice(reference, PHASES_SECTION))]


def _canonical_set(name: str) -> "Set[str]":
    value = _canonical_literal(name)
    assert isinstance(value, (set, frozenset, tuple, list)), name
    return {str(item) for item in value}


def _canonical_transitions() -> "Dict[str, Set[str]]":
    value = _canonical_literal("TASK_TRANSITIONS")
    assert isinstance(value, dict), "TASK_TRANSITIONS"
    return {str(key): {str(item) for item in destinations} for key, destinations in value.items()}


def _defaults(function: ast.FunctionDef) -> "Dict[str, object]":
    out: Dict[str, object] = {}
    args = function.args
    positional = args.args[len(args.args) - len(args.defaults):]
    for arg, default in zip(positional, args.defaults, strict=True):
        out[arg.arg] = ast.literal_eval(default)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is not None:
            out[arg.arg] = ast.literal_eval(default)
    return out


def _shipped_default(qualified: str, parameter: str) -> "Optional[object]":
    """The default of `parameter` on `Class.method` or `function`, read from the source."""
    module = _module()
    if "." in qualified:
        class_name, method = qualified.split(".", 1)
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for body in node.body:
                    if isinstance(body, ast.FunctionDef) and body.name == method:
                        return _defaults(body).get(parameter)
        return None
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == qualified:
            return _defaults(node).get(parameter)
    return None


def _heading_numbers(reference: str) -> "Set[str]":
    numbers = set()
    for match in HEADING.finditer(reference):
        numbers.add(match.group("major"))
        if match.group("minor"):
            numbers.add(f"{match.group('major')}.{match.group('minor')}")
    return numbers


def _section_number(heading: str) -> "Optional[str]":
    match = HEADING.match(heading + " ")
    if not match:
        return None
    if match.group("minor"):
        return f"{match.group('major')}.{match.group('minor')}"
    return match.group("major")


def _heading_for(reference: str, number: str) -> "Optional[str]":
    for match in HEADING.finditer(reference):
        found = match.group("major")
        if match.group("minor"):
            found = f"{found}.{match.group('minor')}"
        if found == number:
            return reference[match.start(): reference.index("\n", match.start())]
    return None


def _contiguous(ids: "List[str]", prefix: str, label: str) -> "List[str]":
    findings: List[str] = []
    if not ids:
        return [f"{label}: no rows parsed"]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        findings.append(f"{label}: duplicate ids {duplicates}")
    numbers = sorted(int(i[len(prefix):]) for i in set(ids))
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(f"{label}: ids are not contiguous from {prefix}1: {numbers}")
    return findings


def _ids_cited_outside(text: str, section: str, use: "re.Pattern[str]", defined: "Set[int]",
                       prefix: str) -> "List[str]":
    findings: List[str] = []
    elsewhere = text.replace(section, "")
    for number in sorted({int(n) for n in use.findall(elsewhere)}):
        if number not in defined:
            findings.append(f"{prefix}{number} is cited outside its table and has no row")
    return findings


# --------------------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------------------


def check_citations(reference: str, repo_root: Path) -> "List[str]":
    """Rows resolve to real text, every use resolves to a row, and every row is used.

    The first R3-17 mutation deleted C1's row while the body still wrote `[C1]`. Re-reading ranges
    cannot notice that; referential integrity in both directions can.
    """
    findings: List[str] = []
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


def check_links(reference: str, decisions: str, repo_root: Path) -> "List[str]":
    """Every Markdown link resolves on disk, and its fragment resolves to a heading in the target.

    The third R3-17 mutation repointed the companion link at `../../../../../missing/`; the checker of
    revision 4 resolved the path but split the fragment off and threw it away, so a link into a section
    that does not exist read as valid.
    """
    findings: List[str] = []
    for name, text, source in (
        ("reference", reference, REFERENCE),
        ("decision record", decisions, DECISIONS),
    ):
        targets = {match.group("target") for match in MD_LINK.finditer(text)}
        if not targets:
            findings.append(f"{name} has no Markdown links at all")
        if not any("#" in target for target in targets):
            findings.append(f"{name} has no link with a fragment, so fragments are untested")
        for target in sorted(targets):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            relative, _, fragment = target.partition("#")
            path = source if not relative else (source.parent / relative).resolve()
            if not path.is_file():
                findings.append(f"{name}: link target does not resolve: {target}")
                continue
            if repo_root.resolve() not in path.parents:
                findings.append(f"{name}: link target leaves the repository: {target}")
                continue
            if fragment and fragment not in _anchors(path):
                findings.append(f"{name}: fragment does not resolve to a heading: {target}")
    return findings


def check_section_refs(reference: str) -> "List[str]":
    """Every `§N` and `§N.M` in the prose resolves to a heading in this document."""
    numbers = _heading_numbers(reference)
    if not numbers:
        return ["no numbered headings parsed"]
    findings: List[str] = []
    for match in SECTION_REF.finditer(reference):
        ref = match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}"
        if ref not in numbers:
            findings.append(f"§{ref} does not resolve to a heading")
    return sorted(set(findings))


def check_canonical_enums(reference: str) -> "List[str]":
    """Every canonical name and value the reference asserts is read out of the validator."""
    findings: List[str] = []
    source = WORKSPACE_LIB.read_text(encoding="utf-8")
    for name in CANONICAL_CONSTANTS:
        if name not in reference:
            findings.append(f"reference never names canonical constant {name}")
        if not re.search(rf"^{name}\s*[:=]", source, re.MULTILINE):
            findings.append(f"{name} is not defined in workspace_lib.py")

    effect_kinds = _canonical_set("EFFECT_KINDS")
    statuses = _canonical_set("AUTHORIZATION_STATUSES")

    sets = re.findall(r"effect\.kind [∈∉] \{([^}]*)\}", reference)
    if not sets:
        findings.append("reference states no `effect.kind` set, so nothing bounds delegation")
    for raw in sets:
        named = {kind.strip() for kind in raw.split(",") if kind.strip()}
        for kind in sorted(named - effect_kinds):
            findings.append(f"an effect.kind set names {kind!r}, which is not in EFFECT_KINDS")
        for kind in sorted(named - CONFINED_EFFECT_KINDS):
            findings.append(f"an effect.kind set admits {kind!r}, whose effects are not confined")
        for kind in sorted(CONFINED_EFFECT_KINDS - named):
            findings.append(f"an effect.kind set excludes {kind!r}, which nothing else may delegate")

    for status in sorted(statuses):
        if f"`{status}`" not in reference:
            findings.append(f"authorization status {status!r} is never named in the reference")
    for number, line in enumerate(reference.splitlines(), 1):
        if CAPABILITY_LICENCE in line:
            continue
        for word in NOT_AUTHORIZATION_STATUSES:
            if re.search(rf"`{word}`", line):
                findings.append(
                    f"line {number} uses `{word}` where no launch capability licenses it"
                )
    return findings


def _parse_canonical_cell(cell: str) -> "Tuple[List[Tuple[str, str]], List[str]]":
    """Parse a canonical-effect cell completely: transitions, and whatever would not parse.

    Review 04's S4-06 mutation replaced a cell with unrelated prose. A `findall` grammar reported
    nothing, because nothing matched. This returns the leftovers, so a cell that says `garbage` is a
    finding rather than a silence.
    """
    pairs: List[Tuple[str, str]] = []
    leftovers: List[str] = []
    for raw in re.split(r",|\bor\b", cell):
        token = raw.replace("`", "").strip()
        if not token or token == "none":
            continue
        match = TRANSITION_TOKEN.fullmatch(token)
        if match:
            pairs.append((match.group(1), match.group(2)))
        else:
            leftovers.append(token)
    return pairs, leftovers


def check_operations(reference: str) -> "List[str]":
    """§8.1's ids are contiguous, its canonical cells parse completely, and its transitions are legal.

    The second R3-17 mutation changed a canonical cell to `DONE → TODO`, which the validator forbids.
    """
    findings: List[str] = []
    section = _slice(reference, OPERATION_SECTION)
    if not section:
        return ["reference has no operation table"]
    rows = _rows(section, OPERATION_ROW)
    if not rows:
        return ["operation table parsed to zero rows"]
    ids = [row[0] for row in rows]
    findings += _contiguous(ids, "O", "operation table")
    findings += _ids_cited_outside(reference, section, O_USE, {int(i[1:]) for i in ids}, "O")

    transitions = _canonical_transitions()
    statuses = _canonical_set("TASK_STATUSES")
    pairs: Set[Tuple[str, str]] = set()
    for row in rows:
        if len(row) < 5:
            findings.append(f"{row[0]}: operation row has {len(row)} cells")
            continue
        parsed, leftovers = _parse_canonical_cell(row[3])
        for leftover in leftovers:
            findings.append(f"{row[0]}: canonical cell has unparsed text {leftover!r}")
        pairs |= set(parsed)
    if not pairs:
        findings.append("no canonical transitions parsed from the operation table")
    for source, destination in sorted(pairs):
        if source not in statuses:
            findings.append(f"§8.1 names canonical status {source!r}, absent from TASK_STATUSES")
        elif destination not in transitions.get(source, set()):
            findings.append(f"§8.1 claims {source} → {destination}, which TASK_TRANSITIONS forbids")
    return findings


def check_facts_and_labels(reference: str) -> "List[str]":
    """§6.1 is the vocabulary, §6.2 derives a display label from it, and §8.1 never reads a label.

    Revision 5's central structural claim is that labels gate nothing. That is checkable: a
    precondition stated over a label of §6.2 that is not also a canonical task status is exactly the
    thing the claim forbids.
    """
    findings: List[str] = []
    facts_section = _slice(reference, FACTS_SECTION)
    label_section = _slice(reference, LABEL_SECTION)
    operations = _slice(reference, OPERATION_SECTION)
    if not facts_section or not label_section:
        return ["reference has no fact section or no label section"]

    vocabulary = set(BACKTICKED_LOWER.findall(facts_section))
    fact_names: Set[str] = set()
    for row in _rows(facts_section, re.compile(r"^\|\s*(canonical|plan|journal|registry|validity)\s*\|")):
        if len(row) >= 2:
            fact_names |= set(BACKTICKED_LOWER.findall(row[1]))
    if not fact_names:
        findings.append("§6.1 declares no facts")

    rows = _rows(label_section, LABEL_ROW)
    if not rows:
        return [*findings, "label table parsed to zero rows"]
    numbers = [int(row[0]) for row in rows]
    labels = [row[1].strip("`") for row in rows]
    if numbers != list(range(1, len(numbers) + 1)):
        findings.append(f"label rows are not contiguous from 1: {numbers}")
    if len(set(labels)) != len(labels):
        findings.append(f"label table is not a bijection: {sorted(labels)}")

    words = {4: "Four", 9: "Nine", 12: "Twelve", 13: "Thirteen", 14: "Fourteen", 15: "Fifteen"}
    claimed = words.get(len(rows))
    if claimed is None:
        findings.append(f"no spelled-out count is known for {len(rows)} rows")
    elif f"**{claimed} rows, {claimed.lower()} labels" not in label_section:
        findings.append(f"prose does not claim {claimed} rows and {claimed.lower()} labels")

    prose = label_section.split("one row each.**", 1)
    if len(prose) != 2:
        findings.append("prose does not list the labels after the count")
    else:
        listed = set(re.findall(r"`([A-Z_]+)`", prose[1].split("\n\n", 1)[0]))
        for label in sorted(set(labels) - listed):
            findings.append(f"prose omits label {label!r}, which the table defines")
        for label in sorted(listed - set(labels)):
            findings.append(f"prose lists label {label!r}, absent from the table")

    for row in rows:
        if len(row) < 4:
            findings.append(f"label row {row[0]} has {len(row)} cells; the fourth is the point of it")
            continue
        if not row[3]:
            findings.append(f"label {row[1]} has an empty 'Not usable for' cell")
        for token in sorted(set(BACKTICKED_LOWER.findall(row[2])) - vocabulary):
            findings.append(f"label {row[1]} matches on {token!r}, which §6.1 does not declare")
    if "anything else" not in rows[-1][2]:
        findings.append("the last label row does not match everything, so label() is not total")

    label_only = set(labels) - _canonical_set("TASK_STATUSES")
    for row in _rows(operations, OPERATION_ROW):
        if len(row) < 3:
            continue
        for used in sorted(set(re.findall(r"`([A-Z_]+)`", row[2])) & label_only):
            findings.append(f"{row[0]}'s precondition reads label {used!r}; labels gate nothing")

    outside = reference.replace(facts_section, "")
    for fact in sorted(fact_names):
        if not re.search(rf"`{fact}`", outside):
            findings.append(f"§6.1 declares fact {fact!r}, which nothing outside §6.1 reads")
    return findings


def check_refusals(reference: str) -> "List[str]":
    """Every refusal code has exactly one §4 row, is named outside §4, and every code used has a row.

    §3.1, §4, §10 and §22 all name codes. One row per code is what makes "detected at exactly one
    point" checkable, and requiring a use outside §4 is what stops a code being declared and forgotten.
    """
    findings: List[str] = []
    section = _slice(reference, REFUSAL_SECTION)
    if not section:
        return ["reference has no refusal section"]
    rows = [row[0].strip("`") for row in _rows(section, re.compile(r"^\|\s*`R-[A-Z-]+`\s*\|"))]
    if not rows:
        return ["refusal table parsed to zero rows"]
    for code in sorted({code for code in rows if rows.count(code) > 1}):
        findings.append(f"{code} has more than one row, so it is detected at two points")
    defined = set(rows)
    outside = reference.replace(section, "")
    for code in sorted(set(REFUSAL_CODE.findall(reference)) - defined):
        findings.append(f"{code} is used and has no row in §4")
    for code in sorted(defined - set(REFUSAL_CODE.findall(outside))):
        findings.append(f"{code} has a row in §4 and is never named outside it")
    return findings


def check_unenforced_sites(reference: str) -> "List[str]":
    """Every §19 rule is marked in each section it names, and every marker belongs to a rule there.

    Revision 4 promised this relation and its checker counted markers instead, so moving a rule's
    statement out of the section §19 named would have passed.
    """
    findings: List[str] = []
    section = _slice(reference, UNENFORCED_SECTION)
    if not section:
        return ["reference has no unenforced-rules section"]
    rows = _rows(section, UNENFORCED_ROW)
    if not rows:
        return ["unenforced table parsed to zero rows"]
    ids = [row[0] for row in rows]
    findings += _contiguous(ids, "U", "unenforced table")
    findings += _ids_cited_outside(reference, section, U_USE, {int(i[1:]) for i in ids}, "U")

    sites: Dict[int, Set[str]] = {}
    for row in rows:
        number = int(row[0][1:])
        if len(row) < 3:
            findings.append(f"{row[0]} has {len(row)} cells and names no site")
            continue
        named = {ref if minor is None else f"{ref}.{minor}"
                 for ref, minor in SECTION_REF.findall(row[2])}
        if not named:
            findings.append(f"{row[0]} names no section in its 'Stated at' cell")
        sites[number] = named
        for reference_number in sorted(named):
            heading = _heading_for(reference, reference_number)
            if heading is None:
                findings.append(f"{row[0]} names §{reference_number}, which is not a heading")
                continue
            if f"[UNENFORCED U{number}]" not in _slice(reference, heading):
                findings.append(f"{row[0]} is not marked inside §{reference_number}")

    for match in MARKER.finditer(reference):
        number = int(match.group(1))
        if number not in sites:
            findings.append(f"a marker names U{number}, which has no row in §19")
            continue
        where = _section_number(_section_at(reference, match.start()))
        if where not in sites[number]:
            findings.append(f"U{number} is marked in §{where}, which its row does not name")
    return findings


def check_settings(reference: str) -> "List[str]":
    """§15.1's lock timeouts equal the shipped defaults, and every setting is read somewhere else.

    This is the one check that compares a number in the prose against the source rather than against
    another part of the prose, and §21.1 uses exactly these two rows as its worked example of what a
    resolving citation still does not establish.
    """
    findings: List[str] = []
    section = _slice(reference, SETTINGS_SECTION)
    if not section:
        return ["reference has no settings section"]
    rows = SETTING_ROW.findall(section)
    if not rows:
        return ["settings table parsed to zero rows"]
    values = dict(rows)
    if len(values) != len(rows):
        findings.append("the settings table names a setting twice")

    shipped = {
        "project_lock_timeout": _shipped_default("DirectoryLock.__init__", "timeout"),
        "registry_lock_timeout": _shipped_default("commit_candidate", "lock_timeout"),
    }
    if shipped["project_lock_timeout"] != shipped["registry_lock_timeout"]:
        findings.append(
            "the reference claims one lock default; the source carries "
            f"{shipped['project_lock_timeout']!r} and {shipped['registry_lock_timeout']!r}"
        )
    for name, default in shipped.items():
        if default is None:
            findings.append(f"no shipped default was found for {name}")
        elif name not in values:
            findings.append(f"§15.1 has no {name} row")
        elif values[name].strip() != f"{default} s":
            findings.append(f"§15.1 says {name} is {values[name]!r}; the source carries {default!r}")

    outside = reference.replace(section, "")
    for name in sorted(values):
        if name not in outside:
            findings.append(f"setting {name!r} is defined in §15.1 and named nowhere else")
    return findings


def check_retired(reference: str) -> "List[str]":
    findings: List[str] = []
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


def check_benchmark_honesty(reference: str) -> "List[str]":
    """§3.2's figures are not reproducible here yet, and the retirement is one stated edit.

    S31 asked for a retirement path, because a check demanding a disclaimer forever would have to be
    deleted alongside it. The disclaimer names the phase that retires it, and this check requires that
    naming, so retiring it is an edit the document itself authorizes rather than a silent deletion.
    """
    findings: List[str] = []
    if "not yet reproducible in this repository" not in reference:
        findings.append("reference does not disclaim present reproducibility")
    match = re.search(r"committed by Phase (\d+) \(§\d+(?:\.\d+)?\)", reference)
    if not match:
        findings.append("disclaimer does not name the phase whose landing retires it")
    elif int(match.group(1)) not in _phase_ids(reference):
        findings.append(f"disclaimer names phase {match.group(1)}, which §20 does not carry")
    return findings


def check_content_identity(reference: str) -> "List[str]":
    """§7.2's `identical` compares the digest that excludes exactly the fields a writer stamps.

    This is the defect revision 6 found by building the reference model rather than by reading. §7.2
    step 4 compared bytes, while §7.4's common envelope carries `coordinator_run`,
    `ownership_generation` and a second-precision `written_at` — none of them derived from the record's
    content. A retry, and more sharply a successor completing a predecessor's operation after a
    takeover, therefore re-derives the same record and cannot reproduce the same bytes, so byte
    comparison reports `conflict` for exactly the publications §8.2 promises are `identical`.

    The relationship, not the phrase: whatever `content_digest` removes beyond what `body_digest`
    removes must be a field of the *common* envelope and of no other table, because a field only the
    attempt envelope carries is part of what two records must agree about, and excluding it would make
    two different attempts' records compare equal.
    """
    findings: List[str] = []
    body = _digest_inputs(reference, "body_digest")
    content = _digest_inputs(reference, "content_digest")
    if not body:
        return ["§7.3 states no `body_digest` input"]
    if not content:
        return ["§7.3 defines no `content_digest` for §7.2 to compare"]
    for name in sorted(body - content):
        findings.append(f"`content_digest` keeps {name!r}, which `body_digest` removes")
    if "body_digest" not in content:
        findings.append("`content_digest` does not remove `body_digest`")
    stamped = content - body - {"body_digest"}
    if not stamped:
        findings.append("`content_digest` excludes nothing `body_digest` keeps, so the two are one digest")
    records = _slice(reference, RECORD_SECTION)
    common = _envelope_fields(records, "The **common envelope**")
    attempt = _envelope_fields(records, "The **attempt envelope**")
    if not common:
        findings.append("§7.4 states no common envelope")
    for name in sorted(stamped):
        if name not in common:
            findings.append(f"`content_digest` excludes {name!r}, which is not a common-envelope field")
        if name in attempt:
            findings.append(f"`content_digest` excludes {name!r}, an attempt-envelope field records must agree on")
    eexist = [line for line in _slice(reference, PUBLISH_SECTION).splitlines() if "EEXIST" in line]
    if not eexist:
        findings.append("§7.2 has no `EEXIST` branch to compare anything in")
    for line in eexist:
        if "content_digest" not in line:
            findings.append("§7.2's `EEXIST` branch does not compare `content_digest`")
        if "bytes" in line:
            findings.append("§7.2's `EEXIST` branch still compares bytes")
    return sorted(set(findings))


def check_fence_coverage(reference: str) -> "List[str]":
    """Every top-level family of §7.1's store is fenced, exempted, or is the fence itself.

    The other defect the reference model found. §12.4 originally fenced only `attempts/<attempt-id>/`,
    and an attempt's fence can list only paths under that attempt, so no fence could ever explain
    `control/stop-requests/0001.json` from an earlier generation: §6.3 read it as `indeterminate` for
    the rest of the project's life, which breaks the one guarantee §10.1 asks of the stop gate at
    exactly the moment it matters. This check walks §7.1's tree and demands an account for each family
    from the document itself, so adding a store path without fencing it is a finding.
    """
    families = _store_families(reference)
    if not families:
        return ["§7.1 carries no store layout"]
    fencing = _slice(reference, JOURNAL_FENCE_SECTION)
    bullet = _store_root_fence_bullet(reference)
    if bullet is None:
        return ["§12.4 states no store-root fence for the project-level records"]
    exempt = _non_journal_families(_slice(reference, RECORD_SECTION))
    durability = _slice(reference, DURABILITY_SECTION)
    findings: List[str] = []
    for family, children in sorted(families.items()):
        if family == "attempts/":
            if "attempts/<attempt-id>/generation-fences/" not in fencing:
                findings.append("§12.4 does not fence the per-attempt records of §7.1")
            continue
        if family == "generation-fences/":
            continue  # the fence is what explains the others; nothing explains it
        if family == "runtime/":
            if not re.search(r"^\|\s*D2\s*\|[^|]*runtime/", durability, re.MULTILINE):
                findings.append("`runtime/` is unfenced and §7.5 does not class it D2")
            continue
        for name in sorted(children) or [""]:
            path = family + name
            if _family(path) in exempt or path in exempt:
                continue
            if f"`{path}`" not in bullet:
                findings.append(f"§12.4's store-root fence does not cover {path!r}")
    return sorted(set(findings))


def check_phases(reference: str, repo_root: Path) -> "List[str]":
    """§20's phases are numbered without gaps, and the plan carries a section for each of them."""
    ids = _phase_ids(reference)
    findings: List[str] = []
    if not ids:
        return ["§20 carries no phase rows"]
    if len(set(ids)) != len(ids):
        findings.append("§20 numbers a phase twice")
    if sorted(set(ids)) != list(range(1, max(ids) + 1)):
        findings.append(f"§20's phase ids are not contiguous from 1: {sorted(set(ids))}")
    plan = repo_root / "docs/parallel-execution-implementation-plan.md"
    if not plan.is_file():
        return [*findings, "the implementation plan §20 points at does not exist"]
    text = plan.read_text(encoding="utf-8")
    if "docs/parallel-execution-implementation-plan.md" not in reference:
        findings.append("§20 does not link the implementation plan")
    if "references/parallel-execution.md" not in text:
        findings.append("the implementation plan does not link the normative reference")
    documented = {int(number) for number in PLAN_PHASE.findall(text)}
    for phase in sorted(set(ids) - documented):
        findings.append(f"the implementation plan has no section for phase {phase}")
    for phase in sorted(documented - set(ids)):
        findings.append(f"the implementation plan documents phase {phase}, which §20 does not carry")
    return sorted(set(findings))


def check_dispositions(decisions: str, repo_root: Path) -> "List[str]":
    """Every finding id of every review file present has a row in the matching section."""
    findings: List[str] = []
    seen_any = False
    for relative, heading, patterns in REVIEW_FILES:
        path = repo_root / relative
        if not path.is_file():
            findings.append(f"{relative} is missing; reviews are preserved, never replaced")
            continue
        seen_any = True
        section = _slice(decisions, heading)
        if not section:
            findings.append(f"decision record has no {heading!r} section for {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        ids: List[str] = []
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


def check_revision_chain(reference: str, decisions: str, repo_root: Path) -> "List[str]":
    """The reference's revision, the history table, and each review's assessed/answered pair agree.

    S4-17 and R4-17 both came from the decision record claiming a revision did something it did not.
    The chain below is the part of that a test can hold: each review is assessed against the revision
    the previous one produced, and the newest review is answered by the revision now shipping.
    """
    findings: List[str] = []
    header = REVISION_HEADER.search(reference)
    if not header:
        return ["reference states no revision in its header"]
    current = int(header.group(1))

    history = _slice(decisions, "## Revision history")
    if not history:
        findings.append("decision record has no revision history")
    else:
        listed = {int(number) for number in REVISION_ROW.findall(history)}
        for revision in range(1, current + 1):
            if revision not in listed:
                findings.append(f"revision history has no row for revision {revision}")
        for revision in sorted(listed - set(range(1, current + 1))):
            findings.append(f"revision history has a row for revision {revision}, which is not shipped")

    previous: Optional[Tuple[int, int]] = None
    last_answered: Optional[int] = None
    for relative, heading, _ in REVIEW_FILES:
        if not (repo_root / relative).is_file():
            continue
        section = _slice(decisions, heading)
        match = ASSESSED.search(section)
        if not match:
            findings.append(f"{heading!r} does not record the revisions it was assessed against")
            continue
        assessed, answered = int(match.group(1)), int(match.group(2))
        if assessed >= answered:
            findings.append(f"{heading!r} claims revision {assessed} was answered by {answered}")
        if answered > current:
            findings.append(f"{heading!r} claims revision {answered}, later than the shipped {current}")
        if previous is not None and previous[1] != assessed:
            findings.append(
                f"{heading!r} was assessed against revision {assessed}, "
                f"but the previous review was answered in {previous[1]}"
            )
        previous = (assessed, answered)
        last_answered = answered
    if last_answered is not None and last_answered != current:
        findings.append(
            f"the newest review is answered in revision {last_answered}, not the shipped {current}"
        )
    return findings


def check_cross_links(reference: str, decisions: str) -> "List[str]":
    findings: List[str] = []
    if "parallel-execution-decisions.md" not in reference:
        findings.append("reference does not link the decision record")
    if "references/parallel-execution.md" not in decisions:
        findings.append("decision record does not link the reference")
    if "normative" not in decisions:
        findings.append("decision record does not say which document is normative")
    return findings


def check_sections(text: str, required: "Tuple[str, ...]", label: str) -> "List[str]":
    return [f"{label} is missing {heading!r}" for heading in required if heading not in text]


CHECKS = (
    ("citations", lambda ref, dec, root: check_citations(ref, root)),
    ("links", lambda ref, dec, root: check_links(ref, dec, root)),
    ("section refs", lambda ref, dec, root: check_section_refs(ref)),
    ("canonical enums", lambda ref, dec, root: check_canonical_enums(ref)),
    ("operations", lambda ref, dec, root: check_operations(ref)),
    ("facts and labels", lambda ref, dec, root: check_facts_and_labels(ref)),
    ("refusals", lambda ref, dec, root: check_refusals(ref)),
    ("unenforced sites", lambda ref, dec, root: check_unenforced_sites(ref)),
    ("settings", lambda ref, dec, root: check_settings(ref)),
    ("content identity", lambda ref, dec, root: check_content_identity(ref)),
    ("fence coverage", lambda ref, dec, root: check_fence_coverage(ref)),
    ("retired", lambda ref, dec, root: check_retired(ref)),
    ("benchmark honesty", lambda ref, dec, root: check_benchmark_honesty(ref)),
    ("phases", lambda ref, dec, root: check_phases(ref, root)),
    ("dispositions", lambda ref, dec, root: check_dispositions(dec, root)),
    ("revision chain", lambda ref, dec, root: check_revision_chain(ref, dec, root)),
    ("cross links", lambda ref, dec, root: check_cross_links(ref, dec)),
    ("reference sections",
     lambda ref, dec, root: check_sections(ref, REFERENCE_SECTIONS, "reference")),
    ("decision sections",
     lambda ref, dec, root: check_sections(dec, DECISIONS_SECTIONS, "decision record")),
)


def run_checks(reference: str, decisions: str, repo_root: Path) -> "Dict[str, List[str]]":
    return {name: check(reference, decisions, repo_root) for name, check in CHECKS}


# One mutation per check, each a substitution on whichever document the check reads. Those marked
# R3-17 are the mutations review 03 reported the previous checker passing; those marked R4 are the
# holes review 04 found in its replacement.
MUTATIONS = (
    ("citations", "reference",
     "| C1 | `plugins/research/skills/project/scripts/workspace_lib.py` | 1384-1392 "
     "| `does not match RUNNING tasks` |\n", "",
     "R3-17: delete C1's row while the body still cites [C1]"),
    ("citations", "reference", "| `class DirectoryLock` |", "| `class CrossProcessLock` |",
     "a needle that is not in the cited range"),
    ("links", "reference", "](../../../../../docs/parallel-execution-decisions.md)",
     "](../../../../../missing/parallel-execution-decisions.md)",
     "R3-17: repoint the companion link at a path that does not exist"),
    ("links", "reference", "#rejected-deferred-and-revisited", "#rejected-deferred-and-renamed",
     "R4: a link whose path resolves and whose fragment does not"),
    ("section refs", "reference", "§21.3", "§21.9", "a dangling section reference"),
    ("canonical enums", "reference", "`deferred`", "`revoked`",
     "an authorization status the validator rejects, on a line no capability licenses"),
    ("canonical enums", "reference", "effect.kind ∈ {none, local_write}",
     "effect.kind ∈ {none, local_write, destructive}", "an effect kind delegation must exclude"),
    ("operations", "reference", "| `` `TODO → RUNNING` `` |", "| `` `DONE → TODO` `` |",
     "R3-17: a canonical transition TASK_TRANSITIONS forbids"),
    ("operations", "reference", "| `` `BLOCKED → TODO` `` |", "| whatever the coordinator decides |",
     "R4: a canonical cell of unrelated prose, which a findall grammar reports as no findings"),
    ("operations", "reference", "| O17 | `take-over`", "| O18 | `take-over`",
     "a gap in the operation ids"),
    ("facts and labels", "reference", "| 12 | `PREPARED` |", "| 12 | `RESERVED` |",
     "a duplicated label, breaking the bijection"),
    ("facts and labels", "reference", "`open_causes` contains this `cause_id`",
     "the attempt is `HELD` and this `cause_id` is open",
     "R4: a precondition stated over a label, which revision 5 forbids"),
    ("facts and labels", "reference", "| 2 | `RELEASED` | `released` |",
     "| 2 | `RELEASED` | `let_go` |", "a label matching on a fact §6.1 does not declare"),
    ("refusals", "reference", "| `R-CAPACITY` | granting would exceed",
     "| `R-OVERSUBSCRIBED` | granting would exceed",
     "a refusal code renamed in §4 while every use keeps the old name"),
    ("unenforced sites", "reference", "store edited by hand reads as history [UNENFORCED U5]",
     "store edited by hand reads as history", "R4: a rule whose site no longer marks it"),
    ("unenforced sites", "reference", "| U5 | Do not hand-edit the execution store | §7.2 |",
     "| U5 | Do not hand-edit the execution store | §7.7 |",
     "R4: a rule pointed at a section that does not carry its marker"),
    ("settings", "reference", "| `project_lock_timeout` | 5.0 s |",
     "| `project_lock_timeout` | 30.0 s |",
     "a timeout that disagrees with the shipped DirectoryLock default"),
    ("settings", "reference", "| `tmp_reap` | 3600 s |", "| `tmp_purge` | 3600 s |",
     "a setting defined in §15.1 and named nowhere else"),
    ("content identity", "reference", 'equal content_digest (§7.3) -> "identical"',
     'equal bytes -> "identical"',
     "R6: §7.2 comparing bytes, which no completion after a takeover can reproduce"),
    ("fence coverage", "reference",
     "`owners/`, `control/stop-requests/` and `control/clearances/`",
     "`owners/` and `control/clearances/`",
     "R6: a store family left unfenced, so a durable stop request reads indeterminate forever"),
    ("phases", "reference", "| 6 | An opt-in pilot", "| 7 | An opt-in pilot",
     "a phase id §20 numbers but the implementation plan does not carry"),
    ("retired", "reference", "A crash prefix is a proper prefix",
     "A SQLite crash prefix is a proper prefix",
     "a retired component proposed again outside the sections that may name it"),
    ("benchmark honesty", "reference", "committed by Phase 6 (§21.3)", "committed eventually",
     "a disclaimer with no stated retirement"),
    ("dispositions", "decisions", "| R4-09 |", "| R4-99 |", "a finding with no disposition row"),
    ("revision chain", "decisions",
     "Assessed against revision 4; answered in revision 5.",
     "Assessed against revision 4; answered in revision 6.",
     "R4: a disposition claiming a revision that is not the one shipping"),
    ("cross links", "reference", "parallel-execution-decisions.md", "somewhere-else.md",
     "a reference that no longer names its companion"),
    ("reference sections", "reference", CITATIONS_HEADING, "## 23. References", "a renamed section"),
    ("decision sections", "decisions", "## Revision history", "## Changes", "a renamed section"),
)


class ParallelExecutionDocTest(unittest.TestCase):
    """The shipped documents pass every check, and every check can be made to fail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = REFERENCE.read_text(encoding="utf-8")
        cls.decisions = DECISIONS.read_text(encoding="utf-8")

    def test_documents_exist(self) -> None:
        for path in (REFERENCE, DECISIONS, WORKSPACE_LIB, PLAN):
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

    def test_the_mutations_earlier_checkers_passed_now_fail(self) -> None:
        """Named separately because they are the reason this checker has been rewritten twice."""
        marked = [m for m in MUTATIONS if m[4].startswith(("R3-17", "R4:"))]
        self.assertEqual(9, len(marked), f"expected 9 named mutations, found {len(marked)}")
        self.assertEqual(
            {"citations", "links", "operations", "facts and labels", "unenforced sites",
             "revision chain"},
            {m[0] for m in marked},
            "a check a previous checker passed lost its named mutation",
        )
        for check_name, document, old, new, why in marked:
            with self.subTest(mutation=why):
                source = self.reference if document == "reference" else self.decisions
                mutated = source.replace(old, new)
                reference = mutated if document == "reference" else self.reference
                decisions = mutated if document == "decisions" else self.decisions
                self.assertTrue(run_checks(reference, decisions, REPO_ROOT)[check_name])


if __name__ == "__main__":
    unittest.main()
