"""A whole post-cutoff lifecycle, through each real launcher, as the subprocesses a host runs.

Most of the suite holds the gate cutoff in 2099, so its projects are legacy and the current rules
never run end to end. A launcher process reads the production cutoff, so a project created now is a
current project there: this drives one from init to a validated close, hitting each refusal the
review found with the real command, and executing the repair each message advises.
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

from tests.conftest import REPO_ROOT, backdate_project
from tests.plugins.research.project.test_worktrees import REFLECTION, git, make_repository

SURFACES = ["bin", "skills/project/scripts"]
PLANNING = "# A1\n\nStatus: draft\n\n## Modules\n\nOne parser, one test file.\n"
TASK = {
    "id": "T01", "name": "Do the work", "success_criteria": "The check passes", "verification": "Run the check",
    "effect": {"kind": "none"}, "outputs": [],
}


# The lifecycle owes a design round before its code task runs and a diff round before the worktree closes.
CODE_TASK = {
    **TASK, "effect": {"kind": "local_write", "description": "write the module"},
    "outputs": [{"root": "target", "path": "src/app.py", "required": True}],
}
DESIGN = "# Design\n\n## 1. Purpose\n\nA parser.\n\n## 13. Implementation status\n\n| T01 | todo |\n"


class Cli:
    def __init__(self, surface: str) -> None:
        base = REPO_ROOT / "plugins/research" / surface
        self.project_tool = str(base / "research-project")
        self.validator = str(base / "research-validate")

    def run(self, *args: object, stdin: str | None = None, tool: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tool or self.project_tool, *map(str, args)], input=stdin, capture_output=True, text=True, check=False
        )

    def ok(self, *args: object, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        completed = self.run(*args, stdin=stdin)
        assert completed.returncode == 0, f"{args}: {completed.stderr}"
        return completed

    def flow(self, project: Path, action: str, identity: str, **fields: Any) -> subprocess.CompletedProcess[str]:
        state = json.loads((project / "project.json").read_text(encoding="utf-8"))
        tokens = {
            name: snapshot(project / f"{name}.md")[1]
            for name in ("spec", "planning", "handoff", "reflection")
            if (project / f"{name}.md").exists()
        }
        payload = {"id": identity, "expected_revision": state["revision"], "tokens": tokens, **fields}
        return self.run("workflow", project, action, "-", stdin=json.dumps(payload))


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def review(cli: Cli, project: Path, target: str, identity: str, **fields: Any) -> None:
    """Open and close a clean one-reviewer round through the launcher, as a coordinator does."""
    opened = json.loads(cli.ok("workflow", project, "goldfish", "-", stdin=json.dumps({
        "id": f"{identity}-open", "expected_revision": state_of(project)["revision"], "operation": "open",
        "target": target, "stages": ["combined"], **fields})).stdout)
    cli.ok("workflow", project, "goldfish", "-", stdin=json.dumps({
        "id": f"{identity}-close", "expected_revision": state_of(project)["revision"], "operation": "close",
        "round": opened["round"], "tokens": {opened["document"]: opened["token"]},
        "reports": [{"stage": "combined", "summary": "read the target"}], "findings": []}))


def run_lifecycle(tmp_path: Path, surface: str) -> list[tuple[str, bool]]:
    """Drive one current project to DONE; return each step with whether it was refused."""
    cli = Cli(surface)
    repository = make_repository(tmp_path)
    created = json.loads(cli.ok(
        "init", tmp_path / "workspace", "--create-root", "--title", "Current lifecycle",
        "--working-directory", repository, "--json",
    ).stdout)
    project = Path(created["project_directory"])
    steps: list[tuple[str, bool]] = []

    def step(
        name: str, completed: subprocess.CompletedProcess[str], *, refused: bool
    ) -> subprocess.CompletedProcess[str]:
        assert (completed.returncode != 0) is refused, f"{name}: {completed.stdout}{completed.stderr}"
        steps.append((name, refused))
        return completed

    plan = {"status": "PLANNING", "tasks": [CODE_TASK]}
    # F1 gate: a current project cannot plan before it is aligned.
    unaligned = step("plan-before-alignment", cli.run(
        "update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=True)
    assert "may not leave ALIGNING for PLANNING" in unaligned.stderr

    sections = {heading: f"Agreed content for {heading}." for heading, _ in lib.SPEC_CANONICAL_SECTIONS}
    step("spec", cli.run("edit", project, "spec", "--sections-json", "-", "--expected-sha256",
                         snapshot(project / "spec.md")[1], stdin=json.dumps(sections)), refused=False)
    step("planning", cli.run("edit", project, "planning", "--body-file", "-", "--expected-sha256",
                             "missing", stdin=PLANNING), refused=False)
    confirmation = {
        "kind": "alignment", "review_id": "A1", "response": "Yes, go ahead", "source": "user reply in the test",
        "scope": "requirements and design",
        "requirements_sha256": snapshot(project / "spec.md")[1],
        "planning_sha256": snapshot(project / "planning.md")[1],
    }
    # The Goldfish gates apply to a confirmation after the 0.31.0 instant: no round, no confirmation.
    ungated = step("confirm-without-round", cli.flow(project, "confirm", "align-0", **confirmation), refused=True)
    assert "goldfish gate 'spec'" in ungated.stderr and "goldfish gate 'planning'" in ungated.stderr
    no_agent = {"source": "user reply in the test", "response": "no fresh subagent here; proceed"}

    def waive(target: str, identity: str) -> None:
        step(f"waive-{target}", cli.flow(
            project, "goldfish", identity, operation="waive", target=target,
            reason="the test host has no fresh subagent", confirmation=no_agent), refused=False)

    waive("spec", "waive-spec-1")
    waive("planning", "waive-planning-1")
    step("confirm-alignment", cli.flow(project, "confirm", "align-1", **confirmation), refused=False)

    # F1: rewriting the agreed design makes the confirmation stale, and the real gate says so.
    planning = (project / "planning.md").read_text(encoding="utf-8")
    step("rewrite-design", cli.run(
        "edit", project, "planning", "--body-file", "-", "--expected-sha256",
        snapshot(project / "planning.md")[1], stdin=planning.replace("One parser", "Three parsers")),
        refused=False)
    stale = step("plan-with-stale-agreement", cli.run(
        "update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=True)
    assert "changed after its planning confirmation" in stale.stderr
    # The repair the message names: review the current text (here, a waiver), then agree to it again.
    waive("planning", "waive-planning-2")
    reconfirm = dict(confirmation, planning_sha256=snapshot(project / "planning.md")[1],
                     requirements_sha256=snapshot(project / "spec.md")[1])
    step("reconfirm", cli.flow(project, "confirm", "align-2", **reconfirm), refused=False)
    step("plan", cli.run("update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=False)

    worktree = (repository.parent / f"{repository.name}.worktrees" / "p1").resolve()
    git(repository, "worktree", "add", "-q", "-b", "feat/p1-current", str(worktree))
    step("record-worktree", cli.flow(
        project, "worktree", "wt-record", operation="record", path=str(worktree), branch="feat/p1-current",
        confirmation={"source": "user reply in the test", "response": "yes"}), refused=False)
    assert state_of(project)["working_directory"] == str(worktree)
    # A current project also owes the delivery records. The repository has no remote and no version
    # file, so both are exemptions; test_delivery.py covers the refusals and the real records.
    user = {"source": "user reply in the test", "response": "no remote, no version"}
    step("record-pull-request", cli.flow(
        project, "worktree", "wt-pr", operation="pull_request", path=str(worktree), exempt="no_remote",
        confirmation=user), refused=False)
    step("record-alpha-exemption", cli.flow(
        project, "worktree", "wt-alpha", operation="alpha", path=str(worktree), exempt=True,
        reason="the repository has no version file", confirmation=user), refused=False)

    (project / "artifacts").mkdir(exist_ok=True)
    (project / "artifacts" / "architecture.md").write_text(DESIGN, encoding="utf-8")
    start = ("task", project, "start", "T01")
    unreviewed = step("start-without-design-round", cli.run(
        *start, "--expected-revision", state_of(project)["revision"]), refused=True)
    assert "goldfish gate 'design'" in unreviewed.stderr and state_of(project)["tasks"][0]["status"] == "TODO"
    review(cli, project, "design", "design-1")
    step("start", cli.run(*start, "--expected-revision", state_of(project)["revision"]), refused=False)
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    # F2: the real recorder keeps a failing command's exit code, and `update` may not accept it.
    failing = step("failing-check", cli.run(
        "record-evidence", project, "--task", "T01", "--json", "--", sys.executable, "-c", "raise SystemExit(7)"),
        refused=True)
    failed = json.loads(failing.stdout)
    accept_failed = {"tasks": [{"id": "T01", "status": "DONE", "evidence": [
        {"root": "workspace", "path": "evidence.md", "anchor": f"evidence-{failed['record_id'][3:]}"}]}]}
    refusal = step("update-done-on-failed-record", cli.run(
        "update", project, "-", "--expected-revision", state_of(project)["revision"], stdin=json.dumps(accept_failed)),
        refused=True)
    assert "did not pass" in refusal.stderr and state_of(project)["tasks"][0]["status"] == "RUNNING"
    passing = json.loads(step("passing-check", cli.run(
        "record-evidence", project, "--task", "T01", "--json", "--", sys.executable, "-c", "pass"),
        refused=False).stdout)
    step("finish", cli.run("task", project, "finish", "T01", "--evidence", passing["record_id"],
                           "--expected-revision", state_of(project)["revision"]), refused=False)

    # F3: `close` may not commit DONE over a worktree nobody decided about; the message names the repair.
    (project / "reflection.md").write_text(REFLECTION, encoding="utf-8")
    blocked = step("close-with-undecided-worktree", cli.run(
        "close", project, "--expected-revision", state_of(project)["revision"]), refused=True)
    assert "no closure decision" in blocked.stderr and "worktree" in blocked.stderr
    assert state_of(project)["status"] != "DONE"
    keep = {"operation": "close", "path": str(worktree), "decision": "keep",
            "confirmation": {"source": "user", "response": "keep it"}}
    # The repository has no remote, so closing the worktree is a diff gate: the branch needs a round first.
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "the module")
    unreviewed_diff = step("keep-without-diff-round", cli.flow(project, "worktree", "wt-keep-0", **keep), refused=True)
    assert "goldfish gate 'diff'" in unreviewed_diff.stderr and state_of(project)["worktrees"][0]["status"] == "active"
    review(cli, project, "diff", "diff-1", worktree=str(worktree))
    step("keep-worktree", cli.flow(project, "worktree", "wt-keep", **keep), refused=False)
    step("finalize", cli.flow(project, "finalize", "final-1", reflection=REFLECTION,
                              continuation={"next": "none"}), refused=False)
    assert state_of(project)["status"] == "DONE"
    validated = cli.run(project, "--close", "--check-index", tool=cli.validator)
    step("validate-close", validated, refused=False)
    return steps


@pytest.mark.parametrize("surface", SURFACES)
def test_a_current_project_runs_from_init_to_a_validated_close(tmp_path: Path, surface: str) -> None:
    steps = run_lifecycle(tmp_path, surface)
    assert [name for name, refused in steps if refused] == [
        "plan-before-alignment", "confirm-without-round", "plan-with-stale-agreement", "start-without-design-round",
        "failing-check", "update-done-on-failed-record", "close-with-undecided-worktree", "keep-without-diff-round",
    ]


def test_both_launcher_surfaces_agree_step_for_step(tmp_path: Path) -> None:
    outcomes = [run_lifecycle(tmp_path / label, surface) for label, surface in zip("ab", SURFACES, strict=True)]
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0]) == 26, "a shortened run would agree trivially"


@pytest.mark.parametrize("surface", SURFACES)
def test_a_legacy_project_keeps_the_old_permissive_behaviour(tmp_path: Path, surface: str) -> None:
    """The same `update` that a current project refuses is accepted for one created before the cutoff."""
    cli = Cli(surface)
    target = tmp_path / "target"
    target.mkdir()
    project = Path(json.loads(cli.ok(
        "init", tmp_path / "workspace", "--create-root", "--title", "Legacy", "--working-directory", target, "--json",
    ).stdout)["project_directory"])
    backdate_project(project)
    sections = {heading: f"Content for {heading}." for heading, _ in lib.SPEC_CANONICAL_SECTIONS}
    cli.ok("edit", project, "spec", "--sections-json", "-", "--expected-sha256", snapshot(project / "spec.md")[1],
           stdin=json.dumps(sections))
    cli.ok("update", project, "-", "--expected-revision", 0,
           stdin=json.dumps({"status": "PLANNING", "tasks": [TASK]}))
    cli.ok("task", project, "start", "T01", "--expected-revision", 1)
    done = {"tasks": [{"id": "T01", "status": "DONE", "evidence": [
        {"root": "workspace", "path": "evidence.md", "anchor": "T01"}]}]}
    cli.ok("update", project, "-", "--expected-revision", 2, stdin=json.dumps(done))
    assert state_of(project)["tasks"][0]["status"] == "DONE"
