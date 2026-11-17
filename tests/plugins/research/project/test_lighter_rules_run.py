"""A clear, small implementation under the lighter policy-3 rules, driven through each real launcher (T07).

The run sets a `low` review risk, a standing review fallback and the default options with `settings`, covers
the spec with a standing waiver and the planning with one combined round, amends a typo instead of reviewing
again, reviews the design once at `low` (a Major that is fixed does not force a re-audit), writes one code
task and closes. It checks what the lighter rules removed and what they kept.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import workspace_lib as lib
from workspace_journal import snapshot

from tests.plugins.research.project.test_current_lifecycle import (
    CODE_TASK,
    DESIGN,
    PLANNING,
    SURFACES,
    Cli,
    review,
    state_of,
)
from tests.plugins.research.project.test_worktrees import REFLECTION, git, make_repository

USER = {"source": "user reply in the test", "response": "Use low risk and the standing fallback"}
STANDING = {"mode": "waive-recorded", "reason": "the test host has no fresh subagent"}


def run_light(tmp_path: Path, surface: str) -> dict[str, Any]:
    """Drive one policy-3 project to DONE under `low` risk; return what the run observed."""
    cli = Cli(surface)
    repository = make_repository(tmp_path)
    created = json.loads(cli.ok(
        "init", "--tag", "test", tmp_path / "workspace", "--create-root", "--title", "Lighter rules",
        "--working-directory", repository, "--json",
    ).stdout)
    project = Path(created["project_directory"])
    seen: dict[str, Any] = {"refused": []}

    def refused(name: str, completed: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        assert completed.returncode != 0, f"{name}: expected a refusal, got {completed.stdout}"
        seen["refused"].append(name)
        return completed

    sections = {heading: f"Agreed content for {heading}." for heading, _ in lib.SPEC_CANONICAL_SECTIONS}
    cli.ok("edit", project, "spec", "--sections-json", "-", "--expected-sha256", snapshot(project / "spec.md")[1],
           stdin=json.dumps(sections))
    cli.ok("edit", project, "planning", "--body-file", "-", "--expected-sha256", "missing", stdin=PLANNING)

    # The user's words come first: no settings without them.
    refused("settings-without-words", cli.flow(project, "settings", "set-0", review_risk="low"))
    settings = cli.flow(project, "settings", "set-1", review_risk="low", review_fallback=STANDING,
                        confirmation=USER)
    assert settings.returncode == 0, settings.stderr
    # A review ladder at low risk is refused: one combined reviewer reads every target.
    ladder = refused("ladder-at-low", cli.flow(
        project, "goldfish", "ladder", operation="open", target="planning",
        stages=["comprehension", "design-critic", "zoom-out", "readiness"]))
    assert "exactly the combined stage" in ladder.stderr

    # Spec: a standing waiver, with no reason and no confirmation of its own.
    waived = cli.flow(project, "goldfish", "waive-spec", operation="waive", target="spec")
    assert waived.returncode == 0, waived.stderr
    # Planning: one combined round, then a typo fixed by an amendment, not by a second round.
    review(cli, project, "planning", "planning-1")
    planning = (project / "planning.md").read_text(encoding="utf-8")
    cli.ok("edit", project, "planning", "--body-file", "-", "--expected-sha256", snapshot(project / "planning.md")[1],
           stdin=planning.replace("One parser", "One parser,"))
    unamended = refused("confirm-after-edit", cli.flow(project, "confirm", "align-0", **alignment(project)))
    assert "goldfish gate 'planning'" in unamended.stderr
    amended = cli.flow(project, "goldfish", "amend-planning", operation="amend", target="planning",
                       kind="editorial", reason="fixed a comma")
    assert amended.returncode == 0, amended.stderr
    aligned = cli.flow(project, "confirm", "align-1", **alignment(project))
    assert aligned.returncode == 0, aligned.stderr
    seen["amendments"] = json.loads(aligned.stdout).get("amendments")

    worktree = (repository.parent / f"{repository.name}.worktrees" / "light").resolve()
    git(repository, "worktree", "add", "-q", "-b", "feat/light", str(worktree))
    recorded = cli.flow(project, "worktree", "wt-record", operation="record", path=str(worktree),
                        branch="feat/light", confirmation={"source": "user reply in the test", "response": "yes"})
    assert recorded.returncode == 0, recorded.stderr
    plan = cli.run("update", project, "-", "--expected-revision", state_of(project)["revision"],
                   stdin=json.dumps({"status": "PLANNING", "tasks": [CODE_TASK]}))
    assert plan.returncode == 0, plan.stderr

    (project / "artifacts").mkdir(exist_ok=True)
    (project / "artifacts" / "architecture.md").write_text(DESIGN, encoding="utf-8")
    # Design at low risk: one combined round; a fixed Major with no artifact closes accepted.
    opened = json.loads(cli.ok("workflow", project, "goldfish", "-", stdin=json.dumps({
        "id": "design-open", "expected_revision": state_of(project)["revision"], "operation": "open",
        "target": "design", "stages": ["combined"]})).stdout)
    closed = json.loads(cli.ok("workflow", project, "goldfish", "-", stdin=json.dumps({
        "id": "design-close", "expected_revision": state_of(project)["revision"], "operation": "close",
        "round": opened["round"], "tokens": {opened["document"]: opened["token"]},
        "reports": [{"stage": "combined", "summary": "read the design"}],
        "findings": [{"id": "F1", "reviewer": "combined", "severity": "Major", "scope": "section 1",
                      "summary": "A gap.", "triage": "FIX", "resolution": "noted"}]})).stdout)
    seen["design_outcome"] = closed["outcome"]
    cli.ok("task", project, "start", "T01", "--expected-revision", state_of(project)["revision"])
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    passing = json.loads(cli.ok(
        "record-evidence", project, "--task", "T01", "--json", "--", sys.executable, "-c", "pass").stdout)
    cli.ok("task", project, "finish", "T01", "--evidence", passing["record_id"],
           "--expected-revision", state_of(project)["revision"])

    # Reports are off by default at policy 3: the action says so and writes nothing.
    report = json.loads(cli.ok("workflow", project, "report", "-", stdin=json.dumps({
        "id": "report-1", "expected_revision": state_of(project)["revision"]})).stdout)
    seen["report_notice"] = report.get("notice")
    seen["report_written"] = (project / "artifacts" / "report.md").exists()

    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "the module")
    (project / "reflection.md").write_text(REFLECTION, encoding="utf-8")
    keep = {"operation": "close", "path": str(worktree), "decision": "keep",
            "confirmation": {"source": "user", "response": "keep it"}}
    kept = cli.flow(project, "worktree", "wt-keep", **keep)
    if kept.returncode != 0:
        seen["refused"].append("keep-without-diff-round")
        assert "goldfish gate 'diff'" in kept.stderr, kept.stderr
        review(cli, project, "diff", "diff-1", worktree=str(worktree))
        again = cli.flow(project, "worktree", "wt-keep-1", **keep)
        assert again.returncode == 0, again.stderr
    final = cli.flow(project, "finalize", "final-1", reflection=REFLECTION, continuation={"next": "none"})
    assert final.returncode == 0, final.stderr
    validated = cli.run(project, "--close", "--check-index", tool=cli.validator)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    seen["state"] = state_of(project)
    seen["project"] = project
    return seen


def alignment(project: Path) -> dict[str, Any]:
    return {
        "kind": "alignment", "review_id": "A1", "response": "Yes, go ahead", "source": "user reply in the test",
        "scope": "requirements and design", "delivery_target": "local",
        "requirements_sha256": snapshot(project / "spec.md")[1],
        "planning_sha256": snapshot(project / "planning.md")[1],
    }


@pytest.mark.parametrize("surface", SURFACES)
def test_a_small_implementation_closes_under_the_lighter_rules(tmp_path: Path, surface: str) -> None:
    seen = run_light(tmp_path, surface)
    state = seen["state"]
    assert state["status"] == "DONE" and state["policy_version"] == 3 and state["review_risk"] == "low"
    assert state["review_fallback"] == STANDING
    assert seen["design_outcome"] == "accepted", "a fixed Major at low risk does not force a re-audit"
    assert seen["amendments"] and seen["amendments"][0]["kind"] == "editorial"
    assert "options.reports" in seen["report_notice"] and seen["report_written"] is False
    assert "settings-without-words" in seen["refused"] and "ladder-at-low" in seen["refused"]
    assert "confirm-after-edit" in seen["refused"]


@pytest.mark.parametrize("surface", SURFACES)
def test_the_lighter_run_needs_fewer_review_rounds_than_the_ladder(tmp_path: Path, surface: str) -> None:
    seen = run_light(tmp_path, surface)
    rounds = sorted((seen["project"] / "reviews").glob("goldfish_*.md"))
    texts = [path.read_text(encoding="utf-8") for path in rounds]
    markers = [json.loads(text.split("<!-- research-goldfish ", 1)[1].split(" -->", 1)[0]) for text in texts]
    # Every reviewed round used one combined stage; the waiver and the amendment reviewed nothing.
    assert all(marker["stages"] in (["combined"], []) for marker in markers), markers
    assert sum("## Standing waiver" in text for text in texts) == 1
    assert sum("## Amendment" in text for text in texts) == 1
