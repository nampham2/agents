"""Goldfish rounds: recording a fresh-reader review of a target, and the rules that decide its outcome.

A round is a managed document with a marker the tool writes. The tool computes what a round covers
and how it ends; the coordinator supplies reports, findings and the user's words. These tests drive
`workflow goldfish` the way a coordinator does, then check the pure helpers underneath it.
"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path
from typing import Any

import pytest
import workspace_goldfish as goldfish
from workspace_goldfish import Round
from workspace_journal import snapshot
from workspace_lib import WorkspaceError
from workspace_session import update_project_data
from workspace_workflows import input_schema, workflow

from tests.plugins.research.project.test_worktrees import (
    add_worktree,
    git,
    load_state,
    make_project,
    make_repository,
    record,
)

OPERATION_IDS = itertools.count(1)
CONFIRMATION = {"source": "user reply, 2026-10-27", "response": "accept"}
CODE_TASK = {
    "id": "T01", "name": "Code", "success_criteria": "ok", "verification": "check",
    "effect": {"kind": "local_write", "description": "edit"},
    "outputs": [{"root": "target", "path": "src/app.py", "required": True}],
}
SPEC_BODY = "# Spec\n\n## Current specification\n\n### Objective and audience\n\nA test objective.\n"
PLANNING_BODY = "# Plan\n\nStatus: draft\n\nThe plan.\n"
DESIGN_BODY = "# Design\n\n## 1. Purpose\n\nGoal.\n\n## 13. Implementation status\n\n| T01 | todo |\n\nTail.\n"


def gold(project: Path, **fields: Any) -> dict[str, Any]:
    request = {"id": f"g-{next(OPERATION_IDS)}", "expected_revision": load_state(project)["revision"], **fields}
    return workflow(project, "goldfish", request)


def open_round(project: Path, target: str = "planning", stages: Any = ("comprehension", "combined"), **fields: Any):
    return gold(project, operation="open", target=target, stages=list(stages), **fields)


def report(stages: Any = ("comprehension", "combined")) -> list[dict[str, str]]:
    return [{"stage": stage, "summary": f"{stage} read the target"} for stage in stages]


def close(project: Path, opened: dict[str, Any], findings: Any = (), reports: Any = None, **fields: Any):
    return gold(
        project, operation="close", round=opened["round"], tokens={opened["document"]: opened["token"]},
        reports=reports if reports is not None else report(), findings=list(findings), **fields,
    )


def finding(identifier: str = "F1", severity: str = "Major", triage: str = "FIX", **fields: Any) -> dict[str, Any]:
    base = {
        "id": identifier, "reviewer": "critic", "severity": severity, "scope": "section 2", "summary": "A gap.",
        "triage": triage,
    }
    if triage == "FIX":
        base["resolution"] = "rewrote section 2"
    if triage == "CONTEST":
        base["evidence"] = "scripts/lib.py:10"
    return {**base, **fields}


def rounds(project: Path) -> list[Round]:
    return goldfish.round_documents(project)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path)


@pytest.fixture
def project(tmp_path: Path, repository: Path) -> Path:
    linked = add_worktree(repository)
    path = make_project(tmp_path, repository)
    record(path, linked)
    update_project_data(path, {"tasks": [CODE_TASK]}, expected_revision=load_state(path)["revision"])
    (path / "planning.md").write_text(PLANNING_BODY, encoding="utf-8")
    (path / "artifacts").mkdir(exist_ok=True)
    (path / "artifacts" / "architecture.md").write_text(DESIGN_BODY, encoding="utf-8")
    spec = (path / "spec.md").read_text(encoding="utf-8")
    (path / "spec.md").write_text(spec + "\n" + SPEC_BODY, encoding="utf-8")
    return path


@pytest.fixture
def worktree(project: Path) -> Path:
    return Path(load_state(project)["worktrees"][0]["path"])


def refuses(project: Path, fragment: str, **fields: Any) -> None:
    before = sorted(path.name for path in (project / "reviews").glob("*")) if (project / "reviews").exists() else []
    with pytest.raises(WorkspaceError, match=fragment):
        gold(project, **fields)
    after = sorted(path.name for path in (project / "reviews").glob("*")) if (project / "reviews").exists() else []
    assert after == before


def edit_planning(project: Path, text: str = "Changed plan.\n") -> None:
    path = project / "planning.md"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# --- the marker ---------------------------------------------------------------------------------


def a_round(**fields: Any) -> Round:
    base: dict[str, Any] = {
        "number": 3, "target": "diff", "worktree": "/w", "pinned": True, "reviewed": "a" * 64, "resolved": None,
        "outcome": "open", "stages": ("combined",), "reaudit_of": None, "delta_of": 2,
    }
    return Round(**{**base, **fields})


def test_a_marker_round_trips() -> None:
    original = a_round()
    assert goldfish.parse_round(3, goldfish.render_marker(original) + "\n\nbody") == original
    assert original.name == "reviews/goldfish_03"
    assert original.key == ("diff", "/w")


def test_a_marker_with_an_unknown_value_is_not_a_round() -> None:
    good = goldfish.render_marker(a_round())
    assert goldfish.parse_round(3, "no marker") is None
    assert goldfish.parse_round(3, "<!-- research-goldfish {not json} -->") is None
    assert goldfish.parse_round(3, "<!-- research-goldfish [1] -->") is None
    assert goldfish.parse_round(3, good.replace(',"delta_of":2', "")) is None
    for before, after in (
        ('"target":"diff"', '"target":"other"'), ('"outcome":"open"', '"outcome":"done"'),
        ('"pinned":true', '"pinned":1'), ('"reviewed":"' + "a" * 64 + '"', '"reviewed":5'),
        ('"resolved":null', '"resolved":5'), ('"worktree":"/w"', '"worktree":5'),
        ('"stages":["combined"]', '"stages":"combined"'), ('"stages":["combined"]', '"stages":["nothing"]'),
        ('"reaudit_of":null', '"reaudit_of":0'), ('"delta_of":2', '"delta_of":"2"'),
    ):
        assert goldfish.parse_round(3, good.replace(before, after)) is None, after


def test_only_canonical_round_files_count(project: Path) -> None:
    opened = open_round(project)
    reviews = project / "reviews"
    marker = (reviews / "goldfish_01.md").read_text(encoding="utf-8")
    for name in ("goldfish_1.md", "goldfish_007.md", "notes.md"):
        (reviews / name).write_text(marker, encoding="utf-8")
    (reviews / "goldfish_09.md").write_text("no marker here\n", encoding="utf-8")
    assert [item.number for item in rounds(project)] == [opened["round"]]
    assert open_round(project)["round"] == 10  # the unparseable file still holds its number
    assert goldfish._round_numbers(project / "elsewhere") == []


# --- what a round covers ----------------------------------------------------------------------


def test_the_design_digest_ignores_the_status_table_and_reconciled_line() -> None:
    base = DESIGN_BODY + "Reconciled through: T01\n"
    digest = goldfish.design_digest(base)
    changed_status = base.replace("| T01 | todo |", "| T01 | done | deviation |")
    assert goldfish.design_digest(changed_status) == digest
    assert goldfish.design_digest(base.replace("T01\n", "T09\n")) == digest
    assert goldfish.design_digest(base.replace("Goal.", "A different goal.")) != digest
    assert goldfish.design_digest("# No status\n\ntext\n") == goldfish.design_digest("# No status\n\ntext  \n\n")


def test_the_design_digest_keeps_a_later_section_at_the_same_level() -> None:
    text = "# D\n\n## 13. Implementation status\n\nrow\n\n## 14. Appendix\n\nkeep\n"
    assert goldfish.design_digest(text) != goldfish.design_digest(text.replace("keep", "changed"))
    assert goldfish.design_digest(text) == goldfish.design_digest(text.replace("row", "other row"))


@pytest.mark.parametrize(
    ("tasks", "expected"),
    [
        ([CODE_TASK], True),
        ([{**CODE_TASK, "status": "SKIPPED"}], False),
        ([{**CODE_TASK, "outputs": [{"root": "target", "path": "notes.md", "required": True}]}], False),
        ([], False),
    ],
)
def test_a_code_task_that_is_not_skipped(tasks: list[dict[str, Any]], expected: bool) -> None:
    assert goldfish.has_code_task({"tasks": tasks}) is expected
    assert goldfish.has_code_task({}) is False


def test_each_document_target_covers_its_own_text(project: Path) -> None:
    state = load_state(project)
    versions = {target: goldfish.version_for(project, state, target) for target in ("spec", "planning", "design")}
    assert len(set(versions.values())) == 3
    edit_planning(project)
    assert goldfish.version_for(project, state, "planning") != versions["planning"]
    (project / "planning.md").unlink()
    with pytest.raises(WorkspaceError, match="the planning document is missing"):
        goldfish.version_for(project, state, "planning")


# --- opening a round --------------------------------------------------------------------------


def test_a_round_opens_with_a_marker_and_a_token(project: Path) -> None:
    opened = open_round(project)
    assert opened["round"] == 1 and opened["document"] == "reviews/goldfish_01"
    text, token = snapshot(project / "reviews" / "goldfish_01.md")
    assert token == opened["token"]
    assert text.startswith("<!-- research-goldfish ")
    assert [(item.target, item.outcome, item.reviewed) for item in rounds(project)] == [
        ("planning", "open", opened["reviewed"]),
    ]
    assert open_round(project)["round"] == 2


def test_opening_changes_no_project_state(project: Path) -> None:
    before = (project / "project.json").read_bytes()
    open_round(project)
    assert (project / "project.json").read_bytes() == before


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"target": "nothing"}, "target must be one of"),
        ({"target": "planning", "stages": []}, "stages must list each stage once"),
        ({"target": "planning", "stages": ["combined", "combined"]}, "stages must list each stage once"),
        ({"target": "planning", "stages": ["combined", "unknown"]}, "stage must be one of"),
        ({"target": "planning", "stages": ["comprehension"]}, "combined stage or the design-critic"),
        ({"target": "spec", "stages": ["design-critic"]}, "comprehension and combined"),
        ({"target": "planning", "stages": ["combined"], "worktree": "/w"}, "diff target only"),
        ({"target": "planning", "stages": ["combined"], "commit": "a" * 40}, "diff target only"),
    ],
)
def test_a_round_that_breaks_a_rule_is_refused(project: Path, fields: dict[str, Any], fragment: str) -> None:
    fields = {"stages": ["combined"], **fields}
    refuses(project, fragment, operation="open", **fields)


def test_a_spec_round_needs_comprehension_and_the_combined_critic(project: Path) -> None:
    refuses(project, "comprehension and combined", operation="open", target="spec",
            stages=["comprehension", "design-critic"])
    refuses(project, "comprehension and combined", operation="open", target="spec", stages=["combined"])
    assert open_round(project, "spec", ("comprehension", "combined", "design-critic"))["round"] == 1


def test_design_and_diff_need_a_code_task(project: Path, worktree: Path) -> None:
    tasks = [{**CODE_TASK, "outputs": [{"root": "target", "path": "notes.md", "required": True}]}]
    update_project_data(project, {"tasks": tasks}, expected_revision=load_state(project)["revision"])
    refuses(project, "no code task, so it has no design gate", operation="open", target="design", stages=["combined"])
    refuses(
        project, "no code task, so it has no diff gate", operation="open", target="diff", stages=["combined"],
        worktree=str(worktree),
    )
    assert open_round(project, "planning", ("combined",))["round"] == 1


def test_a_missing_document_has_no_round(project: Path) -> None:
    (project / "artifacts" / "architecture.md").unlink()
    refuses(project, "artifacts/architecture.md is missing", operation="open", target="design", stages=["combined"])


# --- closing a round --------------------------------------------------------------------------


def test_a_clean_round_is_accepted(project: Path) -> None:
    opened = open_round(project)
    closed = close(project, opened)
    assert closed["outcome"] == "accepted" and closed["resolved"] == opened["reviewed"]
    text = (project / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")
    assert "Outcome: accepted" in text and "None." in text
    assert "- **comprehension**: comprehension read the target" in text
    (current,) = rounds(project)
    assert (current.outcome, current.resolved) == ("accepted", opened["reviewed"])


def test_findings_are_rendered_into_the_body(project: Path) -> None:
    opened = open_round(project)
    nit = finding("N1", "Nit", "DECLINE", summary="a   wording\nnit")
    close(project, opened, [nit, finding("F2", "Nit", "CONTEST", evidence="x.py:1")], confirmation=CONFIRMATION)
    text = (project / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")
    assert "- N1 (critic, Nit, section 2): a wording nit Triage: DECLINE." in text
    assert "Evidence: x.py:1." in text and "## Confirmation" in text and "Response: accept" in text


def test_a_blocker_fixed_by_a_change_needs_a_re_audit(project: Path) -> None:
    opened = open_round(project)
    edit_planning(project)
    closed = close(project, opened, [finding("F1", "Blocker", "FIX")])
    assert closed["outcome"] == "reaudit-required"
    assert closed["resolved"] != opened["reviewed"]
    again = open_round(project)
    assert again["reaudit_of"] == 1 and again["delta_of"] is None and again["round"] == 2
    assert again["reviewed"] == closed["resolved"]


def test_a_re_audit_with_a_major_left_needs_the_users_words(project: Path) -> None:
    first = open_round(project)
    edit_planning(project)
    close(project, first, [finding("F1", "Major", "FIX")])
    second = open_round(project)
    edit_planning(project, "Fixed after the re-audit.\n")  # a fix after the one re-audit changes the target again
    with pytest.raises(WorkspaceError, match="re-audit that still holds a Blocker or Major"):
        close(project, second, [finding("F2", "Major", "FIX")])
    closed = close(project, second, [finding("F2", "Major", "FIX")], confirmation=CONFIRMATION)
    assert closed["outcome"] == "accepted-residual" and closed["resolved"] != second["reviewed"]
    assert open_round(project)["reaudit_of"] is None


def test_a_clean_re_audit_is_accepted(project: Path) -> None:
    first = open_round(project)
    edit_planning(project)
    close(project, first, [finding("F1", "Major", "FIX")])
    second = open_round(project)
    assert close(project, second, [finding("N1", "Nit", "DECLINE")])["outcome"] == "accepted"


def test_a_change_with_a_major_finding_needs_a_re_audit_even_when_contested(project: Path) -> None:
    opened = open_round(project)
    edit_planning(project)
    closed = close(
        project, opened, [finding("F1", "Major", "CONTEST"), finding("N1", "Nit", "FIX")], confirmation=CONFIRMATION,
    )
    assert closed["outcome"] == "reaudit-required"


def test_a_nit_fix_passes_without_a_re_audit(project: Path) -> None:
    opened = open_round(project)
    edit_planning(project)
    assert close(project, opened, [finding("N1", "Nit", "FIX")])["outcome"] == "accepted"


@pytest.mark.parametrize("triage", ["CONTEST", "ESCALATE"])
def test_a_contested_or_escalated_major_needs_the_users_words(project: Path, triage: str) -> None:
    opened = open_round(project)
    with pytest.raises(WorkspaceError, match="contested or escalated needs the user's confirmation"):
        close(project, opened, [finding("F1", "Major", triage)])
    assert close(project, opened, [finding("F1", "Major", triage)], confirmation=CONFIRMATION)["outcome"] == "accepted"


def test_a_delta_round_follows_a_change_after_acceptance(project: Path) -> None:
    close(project, open_round(project))
    edit_planning(project)
    delta = open_round(project)
    assert delta["delta_of"] == 1 and delta["reaudit_of"] is None
    refuses(project, "comprehension and combined", operation="open", target="planning", stages=["combined"])
    assert close(project, delta)["outcome"] == "accepted"


def test_a_round_on_the_same_version_is_not_a_delta(project: Path) -> None:
    close(project, open_round(project))
    assert open_round(project)["delta_of"] is None


@pytest.mark.parametrize(
    ("findings", "fragment"),
    [
        ([finding("F1", "Major", "DECLINE")], "only a Nit may be declined"),
        ([finding("F1", "Blocker", "DECLINE")], "only a Nit may be declined"),
        ([finding("F1", "Nit", "CONTEST", evidence="")], "needs evidence the reviewer could read"),
        ([finding("F1", "Nit", "FIX", resolution="")], "a FIX needs a resolution"),
        ([finding("F1", "Nit", "DECLINE"), finding("F1", "Nit", "DECLINE")], "duplicate finding id"),
        ([finding("F1", "Severe", "DECLINE")], "severity must be one of"),
        ([finding("F1", "Nit", "MAYBE")], "triage must be one of"),
        ([finding("F1", "Nit", "DECLINE", artifact="missing.md")], "artifact must be an existing"),
        ([finding("F1", "Nit", "DECLINE", artifact="../outside.md")], "artifact must be an existing"),
        ([finding("F1", "Nit", "DECLINE", artifact=5)], "artifact must be an existing"),
        ([{"id": "F1"}], "missing required field"),
        ([finding("F1", "Nit", "DECLINE", extra="x")], "unknown field"),
        ([finding("", "Nit", "DECLINE")], "finding id must be non-empty text"),
    ],
)
def test_findings_that_break_a_rule_are_refused(project: Path, findings: list[Any], fragment: str) -> None:
    opened = open_round(project)
    with pytest.raises(WorkspaceError, match=fragment):
        close(project, opened, findings)
    assert rounds(project)[0].outcome == "open"


def test_a_change_without_a_fix_is_refused(project: Path) -> None:
    opened = open_round(project)
    edit_planning(project)
    with pytest.raises(WorkspaceError, match="at least one finding must be a FIX"):
        close(project, opened, [finding("N1", "Nit", "DECLINE")])
    with pytest.raises(WorkspaceError, match="at least one finding must be a FIX"):
        close(project, opened, [])


def test_a_major_fix_on_an_unchanged_target_must_name_an_artifact(project: Path) -> None:
    opened = open_round(project)
    with pytest.raises(WorkspaceError, match="must name an artifact"):
        close(project, opened, [finding("F1", "Major", "FIX")])
    (project / "artifacts" / "decision.md").write_text("A decision.\n", encoding="utf-8")
    closed = close(project, opened, [finding("F1", "Major", "FIX", artifact="artifacts/decision.md")])
    assert closed["outcome"] == "reaudit-required"
    assert "Artifact: artifacts/decision.md." in (project / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"round": "1"}, "round must be a round number"),
        ({"round": 0}, "round must be a round number"),
        ({"round": 9}, "is not a Goldfish round"),
    ],
)
def test_a_close_names_a_real_round(project: Path, fields: dict[str, Any], fragment: str) -> None:
    opened = open_round(project)
    request = {"operation": "close", "tokens": {opened["document"]: opened["token"]}, "reports": report(),
               "findings": [], **fields}
    with pytest.raises(WorkspaceError, match=fragment):
        gold(project, **request)


def test_a_closed_round_cannot_be_closed_again(project: Path) -> None:
    opened = open_round(project)
    close(project, opened)
    token = snapshot(project / "reviews" / "goldfish_01.md")[1]
    with pytest.raises(WorkspaceError, match="already closed"):
        gold(project, operation="close", round=1, tokens={opened["document"]: token}, reports=report(), findings=[])


def test_only_the_latest_round_for_a_target_closes(project: Path) -> None:
    first = open_round(project)
    second = open_round(project)
    with pytest.raises(WorkspaceError, match="is not the latest round for its target"):
        close(project, first)
    assert close(project, second)["outcome"] == "accepted"
    other = open_round(project, "spec")  # another target has its own series
    assert close(project, other)["outcome"] == "accepted"


def test_the_reports_must_match_the_opened_stages(project: Path) -> None:
    opened = open_round(project)
    for reports in (report(("comprehension",)), report(("comprehension", "comprehension")), [], report(("combined",))):
        with pytest.raises(WorkspaceError, match="exactly one report for each opened stage"):
            close(project, opened, reports=reports)
    with pytest.raises(WorkspaceError, match="summary must be non-empty"):
        close(project, opened, reports=[{"stage": "comprehension", "summary": ""}, report()[1]])
    with pytest.raises(WorkspaceError, match="report: missing required"):
        close(project, opened, reports=[{"stage": "comprehension"}, report()[1]])


def test_closing_needs_the_documents_token(project: Path) -> None:
    opened = open_round(project)
    with pytest.raises(WorkspaceError, match="goldfish_01"):
        gold(project, operation="close", round=1, reports=report(), findings=[])
    with pytest.raises(WorkspaceError, match="goldfish_01"):
        gold(project, operation="close", round=1, tokens={opened["document"]: "0" * 64}, reports=report(), findings=[])


# --- a diff round ------------------------------------------------------------------------------


def open_diff(project: Path, worktree: Path, **fields: Any) -> dict[str, Any]:
    return open_round(project, "diff", ("combined",), worktree=str(worktree), **fields)


def test_a_diff_round_covers_the_worktree_head(project: Path, worktree: Path) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    opened = open_diff(project, worktree)
    assert opened["reviewed"] == head
    (current,) = rounds(project)
    assert (current.worktree, current.pinned) == (str(worktree.resolve()), False)
    assert close(project, opened, reports=report(("combined",)))["resolved"] == head


def test_a_diff_round_follows_the_head_to_a_new_commit(project: Path, worktree: Path) -> None:
    opened = open_diff(project, worktree)
    (worktree / "change.txt").write_text("x\n", encoding="utf-8")
    git(worktree, "add", "change.txt")
    git(worktree, "commit", "-q", "-m", "fix")
    closed = close(project, opened, [finding("F1", "Major", "FIX")], reports=report(("combined",)))
    assert closed["outcome"] == "reaudit-required" and closed["resolved"] == git(worktree, "rev-parse", "HEAD")


def test_a_dirty_worktree_cannot_be_reviewed(project: Path, worktree: Path) -> None:
    opened = open_diff(project, worktree)
    (worktree / "scratch.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"uncommitted changes \(\?\? scratch.txt\)"):
        close(project, opened, reports=report(("combined",)))
    refuses(
        project, "uncommitted changes", operation="open", target="diff", stages=["combined"], worktree=str(worktree),
    )


def test_a_worktree_off_its_branch_cannot_be_reviewed(project: Path, worktree: Path) -> None:
    git(worktree, "checkout", "-q", "--detach")
    refuses(
        project, "is on branch '\\(detached\\)', but records", operation="open", target="diff", stages=["combined"],
        worktree=str(worktree),
    )
    git(worktree, "checkout", "-q", "-b", "other")
    refuses(
        project, "is on branch 'other'", operation="open", target="diff", stages=["combined"], worktree=str(worktree),
    )


def test_a_diff_round_names_a_recorded_worktree(project: Path, tmp_path: Path) -> None:
    refuses(project, "is not a recorded repository worktree", operation="open", target="diff", stages=["combined"],
            worktree=str(tmp_path / "nowhere"))
    refuses(project, "worktree must be non-empty text", operation="open", target="diff", stages=["combined"],
            worktree="")


def test_a_pinned_commit_is_reviewed_without_the_checkout(project: Path, worktree: Path, repository: Path) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    (worktree / "scratch.txt").write_text("x\n", encoding="utf-8")  # dirty, and the commit is still reviewable
    git(worktree, "checkout", "-q", "--detach")
    opened = open_diff(project, worktree, commit=head)
    assert opened["reviewed"] == head
    (current,) = rounds(project)
    assert current.pinned is True
    assert close(project, opened, reports=report(("combined",)))["resolved"] == head


@pytest.mark.parametrize(
    ("commit", "fragment"),
    [
        ("abc123", "full lowercase hex object name"),
        ("A" * 40, "full lowercase hex object name"),
        ("f" * 40, "is not in repository"),
        ("", "commit must be non-empty text"),
    ],
)
def test_a_pinned_commit_must_be_a_real_full_commit(project: Path, worktree: Path, commit: str, fragment: str) -> None:
    refuses(
        project, fragment, operation="open", target="diff", stages=["combined"], worktree=str(worktree), commit=commit,
    )


def test_a_worktree_that_does_not_exist_is_reviewed_by_commit(project: Path, worktree: Path, repository: Path) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    git(repository, "worktree", "remove", "--force", str(worktree))
    refuses(project, "has no head; review a commit", operation="open", target="diff", stages=["combined"],
            worktree=str(worktree))
    assert open_diff(project, worktree, commit=head)["reviewed"] == head


def test_git_that_cannot_be_consulted_is_an_error(
    project: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(goldfish, "worktree_observation", lambda path: {"available": False})
    refuses(project, "Git could not be consulted", operation="open", target="diff", stages=["combined"],
            worktree=str(worktree))


@pytest.mark.parametrize("failure", [OSError("no git"), subprocess.TimeoutExpired("git", 5)])
def test_a_git_failure_names_the_command(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*arguments: Any) -> str:
        raise failure

    monkeypatch.setattr(goldfish, "_git", broken)
    with pytest.raises(WorkspaceError, match="git cat-file -e abc failed in /repo"):
        goldfish.repository_git("/repo", "cat-file", "-e", "abc")


def test_a_worktree_that_is_not_a_repository_checkout_is_refused(project: Path) -> None:
    state = load_state(project)
    state["worktrees"] = [{**state["worktrees"][0], "repository": None}]
    with pytest.raises(WorkspaceError, match="is not a recorded repository worktree"):
        goldfish.worktree_entry(state, state["worktrees"][0]["path"])


# --- a waiver ----------------------------------------------------------------------------------


def test_a_waiver_is_a_round_with_the_users_words(project: Path) -> None:
    waived = gold(
        project, operation="waive", target="spec", reason="no subagent in this host", confirmation=CONFIRMATION,
    )
    assert waived["outcome"] == "waived" and waived["round"] == 1
    (current,) = rounds(project)
    assert (current.outcome, current.stages, current.reviewed, current.resolved) == (
        "waived", (), waived["resolved"], waived["resolved"],
    )
    text = (project / "reviews" / "goldfish_01.md").read_text(encoding="utf-8")
    assert "Reason: no subagent in this host" in text and "Response: accept" in text
    assert open_round(project, "spec")["delta_of"] is None  # a waiver of this version is not a delta


def test_a_waiver_needs_a_reason_and_the_users_words(project: Path) -> None:
    refuses(project, "reason must be non-empty text", operation="waive", target="spec", reason="",
            confirmation=CONFIRMATION)
    refuses(project, "confirmation: missing required", operation="waive", target="spec", reason="why",
            confirmation={"source": "x"})
    refuses(project, "expected a JSON object", operation="waive", target="spec", reason="why", confirmation="yes")
    refuses(project, "confirmation.response must be non-empty text", operation="waive", target="spec", reason="why",
            confirmation={"source": "x", "response": " "})


def test_a_diff_waiver_can_pin_a_commit(project: Path, worktree: Path) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    waived = gold(project, operation="waive", target="diff", worktree=str(worktree), commit=head, reason="host merged",
                  confirmation=CONFIRMATION)
    assert waived["resolved"] == head and rounds(project)[0].pinned is True


# --- the action --------------------------------------------------------------------------------


def test_an_unknown_operation_or_field_is_refused(project: Path) -> None:
    refuses(project, "operation must be one of open, close, waive", operation="remove")
    refuses(project, "operation must be one of", target="spec")
    refuses(project, "unknown field", operation="open", target="spec", stages=["comprehension", "combined"], round=1)
    refuses(project, "missing required field", operation="open", target="spec")


def test_the_schema_names_each_operations_keys() -> None:
    schema = input_schema("goldfish")
    assert schema["required"] == ["expected_revision", "id", "operation"]
    assert set(schema["required_by"]) == {"operation=open", "operation=close", "operation=waive"}
    assert "stages" in schema["required_by"]["operation=open"]
    assert "reports" in schema["required_by"]["operation=close"]
    assert "confirmation" in schema["required_by"]["operation=waive"]


def test_a_retry_with_the_same_id_returns_the_same_round(project: Path) -> None:
    request = {"id": "retry-1", "expected_revision": load_state(project)["revision"], "operation": "open",
               "target": "planning", "stages": ["comprehension", "combined"]}
    first = workflow(project, "goldfish", request)
    again = workflow(project, "goldfish", request)
    assert again["round"] == first["round"] == 1
    assert len(rounds(project)) == 1
