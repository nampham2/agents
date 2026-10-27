"""Goldfish review rounds: one fresh-reader review of one target, kept as a managed document.

A round is `reviews/goldfish_NN.md`. Its first line is a JSON marker the tool writes; the rest is
the consolidated report the tool renders from the coordinator's input. The tool computes the
version a round covers and the outcome it reaches. The coordinator supplies reports, findings and
the user's words, never a version or an outcome. A hand-written file at the managed path is trusted
(the tool proves that its own code path ran, as a confirmation does), which is a stated limit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import workspace_lib
from workspace_context import worktree_observation
from workspace_journal import document_path, list_input, object_input, snapshot, string_input
from workspace_lib import (
    DESIGN_DOCUMENT,
    DESIGN_HEADING,
    DESIGN_STATUS_SECTION,
    PROPOSAL_MARKER,
    WorkspaceError,
    at_or_after,
    document_sha256,
    is_code_task,
    planning_path,
    proposal_digest,
    read_text,
)
from workspace_occupancy import _query

TARGETS = ("spec", "planning", "design", "diff")
STAGES = ("comprehension", "design-critic", "zoom-out", "readiness", "combined")
OUTCOMES = ("open", "accepted", "accepted-residual", "reaudit-required", "waived")
PASSING = frozenset({"accepted", "accepted-residual", "waived"})
SEVERITIES = ("Blocker", "Major", "Nit")
TRIAGES = ("FIX", "CONTEST", "ESCALATE", "DECLINE")
OPERATIONS = ("open", "close", "waive")
ROUND_FILE = re.compile(r"goldfish_([0-9]{2}|[1-9][0-9]{2,})\.md")
MARKER = re.compile(r"<!-- research-goldfish (\{.*\}) -->")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MARKER_KEYS = frozenset(
    {"target", "worktree", "pinned", "reviewed", "resolved", "outcome", "stages", "reaudit_of", "delta_of"}
)
FINDING_FIELDS = frozenset(
    {"id", "reviewer", "severity", "scope", "summary", "triage", "evidence", "resolution", "artifact"}
)
FINDING_REQUIRED = frozenset({"id", "reviewer", "severity", "scope", "summary", "triage"})
OPERATION_FIELDS = {
    "open": frozenset({"operation", "target", "worktree", "commit", "stages"}),
    "close": frozenset({"operation", "round", "reports", "findings", "confirmation"}),
    "waive": frozenset({"operation", "target", "worktree", "commit", "reason", "confirmation"}),
}
OPERATION_REQUIRED = {
    "open": frozenset({"target", "stages"}),
    "close": frozenset({"round", "reports", "findings"}),
    "waive": frozenset({"target", "reason", "confirmation"}),
}
UNIVERSAL = frozenset({"id", "expected_revision", "tokens"})
# What an operation hands back: the documents to write, and the result to return.
Written: TypeAlias = "tuple[dict[str, str], dict[str, Any]]"


@dataclass(frozen=True)
class Round:
    number: int
    target: str
    worktree: "str | None"
    pinned: bool
    reviewed: str
    resolved: "str | None"
    outcome: str
    stages: "tuple[str, ...]"
    reaudit_of: "int | None"
    delta_of: "int | None"

    @property
    def key(self) -> "tuple[str, str | None]":
        return (self.target, self.worktree)


def round_name(number: int) -> str:
    return f"reviews/goldfish_{number:02d}"


def render_marker(round_: Round) -> str:
    data = {
        "target": round_.target,
        "worktree": round_.worktree,
        "pinned": round_.pinned,
        "reviewed": round_.reviewed,
        "resolved": round_.resolved,
        "outcome": round_.outcome,
        "stages": list(round_.stages),
        "reaudit_of": round_.reaudit_of,
        "delta_of": round_.delta_of,
    }
    return f"<!-- research-goldfish {json.dumps(data, separators=(',', ':'))} -->"


def _optional_number(value: object) -> bool:
    return value is None or (type(value) is int and value > 0)


def parse_round(number: int, text: str) -> "Round | None":
    """The round a document records, or None when its first line is not a well-formed marker."""
    match = MARKER.fullmatch(text.split("\n", 1)[0].rstrip("\r"))
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != MARKER_KEYS:
        return None
    stages = data["stages"]
    valid = (
        data["target"] in TARGETS
        and data["outcome"] in OUTCOMES
        and type(data["pinned"]) is bool
        and isinstance(data["reviewed"], str)
        and (data["resolved"] is None or isinstance(data["resolved"], str))
        and (data["worktree"] is None or isinstance(data["worktree"], str))
        and isinstance(stages, list)
        and all(stage in STAGES for stage in stages)
        and _optional_number(data["reaudit_of"])
        and _optional_number(data["delta_of"])
    )
    if not valid:
        return None
    return Round(
        number, data["target"], data["worktree"], data["pinned"], data["reviewed"], data["resolved"],
        data["outcome"], tuple(stages), data["reaudit_of"], data["delta_of"],
    )


def _round_numbers(project: Path) -> "list[int]":
    reviews = project / "reviews"
    if not reviews.is_dir():
        return []
    found = (ROUND_FILE.fullmatch(path.name) for path in reviews.iterdir())
    return sorted(int(match.group(1)) for match in found if match is not None)


def round_documents(project: Path) -> "list[Round]":
    """Every well-formed round, oldest first. A file whose marker does not parse is not a round."""
    rounds = []
    for number in _round_numbers(project):
        try:
            text, _ = snapshot(document_path(project, round_name(number)))
        except WorkspaceError:
            continue  # a directory or an unreadable file at a round's path is not a round
        parsed = parse_round(number, text)
        if parsed is not None:
            rounds.append(parsed)
    return rounds


def design_digest(text: str) -> str:
    """SHA-256 of the design without its status table and `Reconciled through` line.

    The section is found the way `design_document_warnings` finds it, by a heading that names it, so a
    numbered heading such as `## 13. Implementation status` matches. Code tasks edit that section, and
    editing it must not make a reviewed design stale.
    """
    headings = list(DESIGN_HEADING.finditer(text))
    body = text
    named = re.compile(r"(?:\d+(?:\.\d+)*[.)]?\s+)?" + re.escape(DESIGN_STATUS_SECTION.lower()) + r"\s*:?")
    for index, heading in enumerate(headings):
        if named.fullmatch(heading.group("title").strip().lower()):
            level = len(heading.group("marks"))
            later = (item.start() for item in headings[index + 1 :] if len(item.group("marks")) <= level)
            end = next(later, len(text))
            body = text[: heading.start()] + text[end:]
            break
    lines = [line.rstrip() for line in body.splitlines() if line.strip() and not line.startswith("Reconciled through:")]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _objects(value: object) -> "list[dict[str, Any]]":
    """The JSON objects in a list; a record that is hand-edited into anything else is skipped, not a crash."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def has_code_task(state: "dict[str, Any]") -> bool:
    """Whether the project has a code task that is not skipped."""
    return any(is_code_task(task) and task.get("status") != "SKIPPED" for task in _objects(state.get("tasks")))


def worktree_entry(state: "dict[str, Any]", path: object) -> "dict[str, Any]":
    resolved = str(Path(string_input(path, "worktree")).resolve())
    for entry in _objects(state.get("worktrees")):
        if entry.get("path") == resolved and entry.get("repository"):
            return entry
    raise WorkspaceError(f"worktree {resolved} is not a recorded repository worktree")


def checked_commit(repository: str, commit: object) -> str:
    """A full lowercase hex commit that exists in `repository`; abbreviations and refs are refused."""
    text = string_input(commit, "commit")
    if COMMIT.fullmatch(text) is None:
        raise WorkspaceError("commit must be a full lowercase hex object name (40 or 64 digits)")
    if _query(Path(repository), "cat-file", "-e", f"{text}^{{commit}}") is None:
        raise WorkspaceError(f"commit {text} is not in repository {repository}")
    return text


def diff_version(entry: "dict[str, Any]", commit: object = None) -> str:
    """The commit a diff round covers: the given one, or the head of the worktree on its recorded branch."""
    if commit is not None:
        return checked_commit(entry["repository"], commit)
    observation = worktree_observation(Path(entry["path"]))
    if not observation["available"]:
        raise WorkspaceError(f"Git could not be consulted for worktree {entry['path']}")
    if not observation["head"]:
        raise WorkspaceError(f"worktree {entry['path']} has no head; review a commit with `commit`")
    if observation["branch"] != entry["branch"]:
        raise WorkspaceError(
            f"worktree {entry['path']} is on branch {observation['branch'] or '(detached)'!r}, "
            f"but records {entry['branch']!r}; review a commit with `commit`"
        )
    if observation["dirty"]:
        shown = ", ".join(observation["dirty"][:5])
        raise WorkspaceError(
            f"worktree {entry['path']} has uncommitted changes ({shown}); commit them first. "
            "Untracked files count; ignored files do not"
        )
    return str(observation["head"])


def _document(path: Path, label: str) -> str:
    if not path.is_file():
        raise WorkspaceError(f"{label} is missing: {path}")
    return read_text(path)


def version_for(
    project: Path, state: "dict[str, Any]", target: str, worktree: "str | None" = None, commit: object = None
) -> str:
    """What a round on `target` covers right now: a content digest for a document, a commit for a diff."""
    if target == "spec":
        return proposal_digest("requirements", _document(project / "spec.md", "spec.md"))
    if target == "planning":
        return proposal_digest("planning", _document(planning_path(project), "the planning document"))
    if target == "design":
        return design_digest(_document(project / DESIGN_DOCUMENT, DESIGN_DOCUMENT))
    return diff_version(worktree_entry(state, worktree), commit)


def _choice(value: object, allowed: "tuple[str, ...]", field: str) -> str:
    if value not in allowed:
        raise WorkspaceError(f"{field} must be one of {', '.join(allowed)}")
    return str(value)


def _target_fields(state: "dict[str, Any]", request: "dict[str, Any]") -> "tuple[str, str | None, str | None]":
    """The target, the worktree path a diff round names and the commit it pins; both None for a document."""
    target = _choice(request.get("target"), TARGETS, "target")
    if target != "diff":
        if "worktree" in request or "commit" in request:
            raise WorkspaceError("worktree and commit apply to a diff target only")
        if target == "design" and not has_code_task(state):
            raise WorkspaceError("this project has no code task, so it has no design gate")
        return target, None, None
    if not has_code_task(state):
        raise WorkspaceError("this project has no code task, so it has no diff gate")
    entry = worktree_entry(state, request.get("worktree"))
    commit = request.get("commit")
    return target, entry["path"], commit


def _confirmation(value: object) -> "dict[str, str]":
    confirmation = object_input(value, {"source", "response"}, {"source", "response"}, where="confirmation")
    return {key: string_input(confirmation[key], f"confirmation.{key}") for key in ("source", "response")}


def _stages(value: object) -> "tuple[str, ...]":
    stages = list_input(value, len(STAGES), "stages")
    for stage in stages:
        _choice(stage, STAGES, "stage")
    if not stages or len(set(stages)) != len(stages):
        raise WorkspaceError("stages must list each stage once and at least one")
    return tuple(stages)


def _check_stage_rules(target: str, stages: "tuple[str, ...]", delta: bool) -> None:
    if "combined" not in stages and "design-critic" not in stages:
        raise WorkspaceError("every round lists the combined stage or the design-critic stage")
    if (target == "spec" or delta) and not {"comprehension", "combined"} <= set(stages):
        raise WorkspaceError("a spec round and a delta round list the comprehension and combined stages")


def _round_text(directory: Path, number: int) -> "tuple[str, str]":
    """The document name and text of round `number`, or an error naming what is wrong."""
    name = round_name(number)
    text, _ = snapshot(document_path(directory, name))
    return name, text


def _write(number: int, round_: Round, body: str) -> "dict[str, str]":
    return {round_name(number): render_marker(round_) + "\n\n" + body.rstrip("\n") + "\n"}


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def open_round(project: Path, state: "dict[str, Any]", request: "dict[str, Any]") -> Written:
    target, worktree, commit = _target_fields(state, request)
    stages = _stages(request.get("stages"))
    reviewed = version_for(project, state, target, worktree, commit)
    rounds = round_documents(project)
    closed = [item for item in rounds if item.key == (target, worktree) and item.outcome != "open"]
    reaudit_of = delta_of = None
    if closed and closed[-1].outcome == "reaudit-required":
        reaudit_of = closed[-1].number
    else:
        passing = [item for item in closed if item.outcome in PASSING]
        if passing and passing[-1].resolved != reviewed:
            delta_of = passing[-1].number
    _check_stage_rules(target, stages, delta_of is not None)
    number = max(_round_numbers(project), default=0) + 1
    round_ = Round(number, target, worktree, commit is not None, reviewed, None, "open", stages, reaudit_of, delta_of)
    documents = _write(number, round_, f"# Goldfish round {number:02d}: {target}\n\nOpen. No report yet.")
    result = {
        "round": number,
        "document": round_name(number),
        "token": document_sha256(documents[round_name(number)]),
        "reviewed": reviewed,
        "reaudit_of": reaudit_of,
        "delta_of": delta_of,
    }
    return documents, result


def _reports(value: object, stages: "tuple[str, ...]") -> "list[dict[str, str]]":
    reports = []
    for item in list_input(value, len(STAGES), "reports"):
        report = object_input(item, {"stage", "summary"}, {"stage", "summary"}, where="report")
        reports.append({"stage": str(report["stage"]), "summary": string_input(report["summary"], "report summary")})
    if sorted(report["stage"] for report in reports) != sorted(stages):
        raise WorkspaceError(f"reports must hold exactly one report for each opened stage: {', '.join(stages)}")
    return reports


def _artifact_exists(project: Path, artifact: str) -> bool:
    candidate = (project / artifact).resolve()
    return candidate.is_relative_to(project.resolve()) and candidate.is_file()


def _findings(value: object, project: Path) -> "list[dict[str, Any]]":
    findings: "list[dict[str, Any]]" = []
    for item in list_input(value, 200, "findings"):
        finding = object_input(item, FINDING_FIELDS, FINDING_REQUIRED, where="finding")
        for field in ("id", "reviewer", "scope", "summary"):
            string_input(finding[field], f"finding {field}")
        label = f"finding {finding['id']}"
        if any(earlier["id"] == finding["id"] for earlier in findings):
            raise WorkspaceError(f"{label}: duplicate finding id")
        severity = _choice(finding["severity"], SEVERITIES, f"{label} severity")
        triage = _choice(finding["triage"], TRIAGES, f"{label} triage")
        if triage == "DECLINE" and severity != "Nit":
            raise WorkspaceError(f"{label}: only a Nit may be declined")
        if triage == "CONTEST" and not finding.get("evidence"):
            raise WorkspaceError(f"{label}: a CONTEST needs evidence the reviewer could read")
        if triage == "FIX" and not finding.get("resolution"):
            raise WorkspaceError(f"{label}: a FIX needs a resolution")
        artifact = finding.get("artifact")
        if artifact is not None and not (isinstance(artifact, str) and _artifact_exists(project, artifact)):
            raise WorkspaceError(f"{label}: artifact must be an existing workspace-relative file")
        findings.append(finding)
    return findings


def _finding_line(finding: "dict[str, Any]") -> str:
    parts = [
        f"- {finding['id']} ({finding['reviewer']}, {finding['severity']}, {_flat(finding['scope'])}): "
        f"{_flat(finding['summary'])} Triage: {finding['triage']}."
    ]
    for key in ("evidence", "resolution", "artifact"):
        if finding.get(key):
            parts.append(f"{key.title()}: {_flat(finding[key])}.")
    return " ".join(parts)


def _confirmation_lines(confirmation: "dict[str, str]") -> "list[str]":
    source, response = _flat(confirmation["source"]), _flat(confirmation["response"])
    return ["## Confirmation", "", f"Source: {source}", f"Response: {response}"]


def close_round(project: Path, state: "dict[str, Any]", request: "dict[str, Any]") -> Written:
    number = request.get("round")
    if type(number) is not int or number < 1:
        raise WorkspaceError("round must be a round number")
    name, text = _round_text(project, number)
    current = parse_round(number, text) if text else None
    if current is None:
        raise WorkspaceError(f"{name} is not a Goldfish round")
    if current.outcome != "open":
        raise WorkspaceError(f"round {number:02d} is already closed ({current.outcome}); open a new round")
    series = [item for item in round_documents(project) if item.key == current.key]
    if series[-1].number != number:
        raise WorkspaceError(f"round {number:02d} is not the latest round for its target; open a new round")
    reports = _reports(request.get("reports"), current.stages)
    findings = _findings(request.get("findings"), project)
    confirmation = _confirmation(request["confirmation"]) if "confirmation" in request else None
    if current.pinned:
        resolved = current.reviewed
    else:
        resolved = version_for(project, state, current.target, current.worktree)
    changed = resolved != current.reviewed
    important = [item for item in findings if item["severity"] in ("Blocker", "Major")]
    fixed = [item for item in important if item["triage"] == "FIX"]
    if changed and not any(item["triage"] == "FIX" for item in findings):
        raise WorkspaceError("the target changed after the review, so at least one finding must be a FIX")
    for item in fixed:
        if not changed and not item.get("artifact"):
            raise WorkspaceError(
                f"finding {item['id']}: a Blocker or Major FIX on an unchanged target must name an artifact"
            )
    if confirmation is None and any(item["triage"] in ("ESCALATE", "CONTEST") for item in important):
        raise WorkspaceError("a Blocker or Major that is contested or escalated needs the user's confirmation")
    if current.reaudit_of is not None and important:
        if confirmation is None:
            raise WorkspaceError("a re-audit that still holds a Blocker or Major needs the user's confirmation")
        outcome = "accepted-residual"
    elif current.reaudit_of is None and (fixed or (changed and important)):
        outcome = "reaudit-required"
    else:
        outcome = "accepted"
    lines = [f"# Goldfish round {number:02d}: {current.target}", "", f"Outcome: {outcome}", "", "## Reports", ""]
    lines += [f"- **{report['stage']}**: {_flat(report['summary'])}" for report in reports]
    lines += ["", "## Findings", ""]
    lines += [_finding_line(item) for item in findings] or ["None."]
    if confirmation is not None:
        lines += ["", *_confirmation_lines(confirmation)]
    closed = Round(
        number, current.target, current.worktree, current.pinned, current.reviewed, resolved, outcome,
        current.stages, current.reaudit_of, current.delta_of,
    )
    return _write(number, closed, "\n".join(lines)), {"round": number, "outcome": outcome, "resolved": resolved}


def waive_round(project: Path, state: "dict[str, Any]", request: "dict[str, Any]") -> Written:
    target, worktree, commit = _target_fields(state, request)
    reason = string_input(request.get("reason"), "reason")
    confirmation = _confirmation(request.get("confirmation"))
    version = version_for(project, state, target, worktree, commit)
    number = max(_round_numbers(project), default=0) + 1
    round_ = Round(number, target, worktree, commit is not None, version, version, "waived", (), None, None)
    lines = [
        f"# Goldfish round {number:02d}: {target} (waived)", "", "## Waiver", "", f"Reason: {_flat(reason)}", "",
        *_confirmation_lines(confirmation),
    ]
    documents = _write(number, round_, "\n".join(lines))
    return documents, {"round": number, "document": round_name(number), "outcome": "waived", "resolved": version}


def goldfish_operation(project: Path, state: "dict[str, Any]", request: "dict[str, Any]") -> Written:
    """Apply one `workflow goldfish` operation: documents to write and the result to return."""
    operation = _choice(request.get("operation"), OPERATIONS, "operation")
    allowed = OPERATION_FIELDS[operation] | UNIVERSAL
    required = OPERATION_REQUIRED[operation] | {"operation"}
    object_input(request, allowed, required, where=f"workflow goldfish {operation}")
    handler = {"open": open_round, "close": close_round, "waive": waive_round}[operation]
    return handler(project, state, request)


# --- the gates -----------------------------------------------------------------------------------

DIFF_GATE = "goldfish gate 'diff'"
SUBJECT = {"spec": "spec.md", "planning": "the planning document", "design": DESIGN_DOCUMENT, "diff": "the diff"}
ENTRY_HEADING = re.compile(r"^### (?P<stamp>\d{4}-\d{2}-\d{2}T\S+) \u2014 ", re.MULTILINE)


def round_errors(project: Path, target: str, worktree: "str | None", version: str) -> "list[str]":
    """Why no current round covers `version` of the target; empty when the latest round passes and matches.

    The latest round for the target decides, so a later open, unaccepted or stale round closes the gate
    even when an earlier round passed.
    """
    series = [item for item in round_documents(project) if item.key == (target, worktree)]
    head = f"goldfish gate '{target}'"
    shown = f"{SUBJECT[target]} version {version[:12]}"
    if not series:
        return [
            f"{head}: no round covers {shown}; run a round (references/goldfish-review.md) "
            "or record a waiver with 'workflow goldfish' operation waive"
        ]
    latest = series[-1]
    if latest.outcome not in PASSING:
        return [
            f"{head}: the latest round ({latest.number:02d}, {latest.outcome}) is not accepted, so it does not cover "
            f"{shown}; close it, run a new round or record a waiver"
        ]
    if latest.resolved != version:
        return [
            f"{head}: the latest round ({latest.number:02d}) covers version {str(latest.resolved)[:12]}, but "
            f"{SUBJECT[target]} is now {version[:12]}; run a new round or record a waiver"
        ]
    return []


def confirm_gate_errors(project: Path, kinds: "tuple[str, ...]", spec_text: str, planning_text: str) -> "list[str]":
    """The spec and planning gates: a confirmation needs a current round for each document it confirms."""
    if not workspace_lib.is_goldfish_gated():
        return []
    errors: "list[str]" = []
    if "requirements" in kinds:
        errors += round_errors(project, "spec", None, proposal_digest("requirements", spec_text))
    if "planning" in kinds:
        errors += round_errors(project, "planning", None, proposal_digest("planning", planning_text))
    return errors


def _code_started(state: "dict[str, Any]") -> bool:
    # A task that ran leaves a status, a start stamp or evidence. An in-flight project from before the
    # stamps may show none of them for a blocked task, and then its resume asks for a design round once.
    return any(
        is_code_task(task)
        and (task.get("status") in ("RUNNING", "DONE") or task.get("started_at") or task.get("evidence"))
        for task in _objects(state.get("tasks"))
    )


def design_gate_errors(project: Path, previous: "dict[str, Any]", candidate: "dict[str, Any]") -> "list[str]":
    """The design gate: the first code task to run needs a current round for the design document.

    Each state is classified from its own data, so a task that becomes a code task by an outputs edit
    while running fires the gate, and a started task that resumes (`BLOCKED` to `RUNNING`) does not.
    """
    if not workspace_lib.is_goldfish_gated() or _code_started(previous):
        return []
    running = any(
        is_code_task(task) and task.get("status") in ("RUNNING", "DONE") for task in _objects(candidate.get("tasks"))
    )
    if not running:
        return []
    path = project / DESIGN_DOCUMENT
    if not path.is_file():
        return [f"goldfish gate 'design': {DESIGN_DOCUMENT} is missing; write the design and run a round first"]
    return round_errors(project, "design", None, design_digest(read_text(path)))


def upstream_errors(entry: "dict[str, Any]", head: str) -> "list[str]":
    """The release check on a pushed branch: its upstream tracking ref holds `head`. The tool never fetches."""
    branch, path = entry["branch"], entry["path"]
    name = _query(Path(path), "rev-parse", "--abbrev-ref", "@{upstream}")
    if name is None:
        return [f"{DIFF_GATE}: branch {branch} has no upstream; push it with -u first"]
    if not name.endswith("/" + branch):
        return [f"{DIFF_GATE}: the upstream {name} does not track a branch named {branch}"]
    if _query(Path(path), "rev-parse", "@{upstream}") != head:
        return [f"{DIFF_GATE}: the upstream {name} differs from HEAD; push first (a push updates the tracking ref)"]
    return []


def delivery_gate_errors(
    project: Path, state: "dict[str, Any]", entry: "dict[str, Any]", event: str, request: "dict[str, Any]"
) -> "list[str]":
    """The diff gate at a release, a merge record, or the first closure of a worktree with no remote."""
    if not workspace_lib.is_goldfish_gated() or not has_code_task(state) or not entry.get("repository"):
        return []
    worktree = entry["path"]
    repository, ref = Path(entry["repository"]), f"refs/heads/{entry['branch']}"
    if event == "closure":
        if "pr_exemption" not in entry:
            return []
        head = _query(repository, "rev-parse", ref)
        if head is None:
            # The branch is gone (merged locally and deleted): the last round the worktree passed stands.
            latest = [item for item in round_documents(project) if item.key == ("diff", worktree)]
            if latest and latest[-1].outcome in PASSING:
                return []
            return [f"{DIFF_GATE}: branch {entry['branch']} no longer exists in {repository} and no round has passed"]
        return round_errors(project, "diff", worktree, head)
    if event == "merge":
        if request.get("merged_head") is None:
            return [f"{DIFF_GATE}: a merged record needs merged_head, the PR head commit that was merged"]
        try:
            merged = checked_commit(entry["repository"], request["merged_head"])
        except WorkspaceError as error:
            return [f"{DIFF_GATE}: {error}"]
        branch_head = _query(repository, "rev-parse", ref)
        if branch_head is not None and branch_head != merged:
            return [
                f"{DIFF_GATE}: merged_head {merged[:12]} is not the head of branch {entry['branch']} "
                f"({branch_head[:12]})"
            ]
        return round_errors(project, "diff", worktree, merged)
    try:
        head = diff_version(entry)
    except WorkspaceError as error:
        return [f"{DIFF_GATE}: {error}"]
    errors = upstream_errors(entry, head) if "pull_request" in entry else []
    return errors + round_errors(project, "diff", worktree, head)


# --- reporting -----------------------------------------------------------------------------------


def _at_or_after_instant(value: object) -> bool:
    """Whether a recorded time is at or after the Goldfish instant; a missing or unreadable time is not."""
    return at_or_after(value, workspace_lib.GOLDFISH_FROM)


def goldfish_findings(project: Path) -> "list[str]":
    """Warnings from the round documents alone, for the latest round of each target; no Git is read."""
    latest: "dict[tuple[str, str | None], Round]" = {}
    for item in round_documents(project):
        latest[item.key] = item
    found: "list[str]" = []
    for (target, _), item in latest.items():
        if item.outcome == "open":
            found.append(f"Goldfish round {item.number:02d} ({target}) is open; close it or open a new round")
        elif item.outcome == "reaudit-required":
            found.append(f"Goldfish round {item.number:02d} ({target}) needs its re-audit; open a round")
        elif target in ("spec", "planning"):
            path = project / "spec.md" if target == "spec" else planning_path(project)
            kind = "requirements" if target == "spec" else "planning"
            if path.is_file() and item.resolved != proposal_digest(kind, read_text(path)):
                found.append(
                    f"Goldfish round {item.number:02d} covers an earlier version of {SUBJECT[target]}; "
                    "the next confirmation needs a delta round"
                )
    return found


def _object(value: object) -> "dict[str, Any]":
    return value if isinstance(value, dict) else {}


def _worktree_event_warnings(entry: "dict[str, Any]", passed: "set[Any]") -> "list[str]":
    """Release, merge and closure events of one worktree at or after the instant with no diff round.

    A release or merge record stores no commit, so the check is that the worktree has a passing diff round
    at all, not that the round covers the commit that shipped.
    """
    path = entry.get("path")
    merge = _object(entry.get("merge"))
    release = _object(_object(entry.get("version_line")).get("release"))
    events = [
        ("release", release.get("recorded_at")),
        ("merge", merge.get("recorded_at") if merge.get("state") == "merged" else None),
    ]
    found = [
        f"the {name} of {path} at {stamp} has no Goldfish diff round"
        for name, stamp in events
        if _at_or_after_instant(stamp) and not any(item[0] == "diff" and item[1] == path for item in passed)
    ]
    closed = _object(entry.get("closure")).get("observed_at")
    if "pull_request" in entry and not merge and _at_or_after_instant(closed):
        found.append(f"worktree {path} was closed at {closed} with no merge recorded")
    return found


def goldfish_event_warnings(project: Path, state: "dict[str, Any]") -> "list[str]":
    """Gate events at or after the instant that no round covers: what an older launcher leaves behind.

    Confirmations are matched to a round by digest. A task start and a release or merge are matched only to
    the presence of a passing design or diff round, because the project records no version for them.
    """
    if not workspace_lib.is_goldfish_gated():
        return []
    rounds = round_documents(project)
    passed = {(item.target, item.worktree, item.resolved) for item in rounds if item.outcome in PASSING}
    found: "list[str]" = []
    spec_path = project / "spec.md"
    spec_text = read_text(spec_path) if spec_path.is_file() else ""
    headings = [(match.start(), match.group("stamp")) for match in ENTRY_HEADING.finditer(spec_text)]
    for marker in PROPOSAL_MARKER.finditer(spec_text):
        stamp = next((value for position, value in reversed(headings) if position < marker.start()), None)
        target = "spec" if marker.group("kind") == "requirements" else "planning"
        if _at_or_after_instant(stamp) and not any(
            item[0] == target and item[2] == marker.group("sha") for item in passed
        ):
            found.append(
                f"the {target} confirmation {marker.group('review_id')} was recorded at {stamp} with no Goldfish "
                "round for that version"
            )
    if has_code_task(state):
        designed = any(item[0] == "design" for item in passed)
        for task in _objects(state.get("tasks")):
            if is_code_task(task) and _at_or_after_instant(task.get("started_at")) and not designed:
                found.append(f"code task {task.get('id')} started at {task['started_at']} with no design round")
        for entry in _objects(state.get("worktrees")):
            found += _worktree_event_warnings(entry, passed)
    return found


def goldfish_warnings(project: Path, state: "dict[str, Any]") -> "list[str]":
    return goldfish_findings(project) + goldfish_event_warnings(project, state)
