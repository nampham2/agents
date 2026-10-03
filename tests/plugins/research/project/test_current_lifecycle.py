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
ARCHITECTURE = "# A1\n\nStatus: draft\n\n## Modules\n\nOne parser, one test file.\n"
TASK = {
    "id": "T01", "name": "Do the work", "success_criteria": "The check passes", "verification": "Run the check",
    "effect": {"kind": "none"}, "outputs": [],
}


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
            for name in ("spec", "architecture", "handoff", "reflection")
            if (project / f"{name}.md").exists()
        }
        payload = {"id": identity, "expected_revision": state["revision"], "tokens": tokens, **fields}
        return self.run("workflow", project, action, "-", stdin=json.dumps(payload))


def state_of(project: Path) -> dict[str, Any]:
    return json.loads((project / "project.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


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

    plan = {"status": "PLANNING", "tasks": [TASK]}
    # F1 gate: a current project cannot plan before it is aligned.
    unaligned = step("plan-before-alignment", cli.run(
        "update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=True)
    assert "may not leave ALIGNING for PLANNING" in unaligned.stderr

    sections = {heading: f"Agreed content for {heading}." for heading, _ in lib.SPEC_CANONICAL_SECTIONS}
    step("spec", cli.run("edit", project, "spec", "--sections-json", "-", "--expected-sha256",
                         snapshot(project / "spec.md")[1], stdin=json.dumps(sections)), refused=False)
    step("architecture", cli.run("edit", project, "architecture", "--body-file", "-", "--expected-sha256",
                                 "missing", stdin=ARCHITECTURE), refused=False)
    confirmation = {
        "kind": "alignment", "review_id": "A1", "response": "Yes, go ahead", "source": "user reply in the test",
        "scope": "requirements and design",
        "requirements_sha256": snapshot(project / "spec.md")[1],
        "architecture_sha256": snapshot(project / "architecture.md")[1],
    }
    step("confirm-alignment", cli.flow(project, "confirm", "align-1", **confirmation), refused=False)

    # F1: rewriting the agreed design makes the confirmation stale, and the real gate says so.
    architecture = (project / "architecture.md").read_text(encoding="utf-8")
    step("rewrite-design", cli.run(
        "edit", project, "architecture", "--body-file", "-", "--expected-sha256",
        snapshot(project / "architecture.md")[1], stdin=architecture.replace("One parser", "Three parsers")),
        refused=False)
    stale = step("plan-with-stale-agreement", cli.run(
        "update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=True)
    assert "changed after its architecture confirmation" in stale.stderr
    # The repair the message names: agree to the current text again.
    reconfirm = dict(confirmation, architecture_sha256=snapshot(project / "architecture.md")[1],
                     requirements_sha256=snapshot(project / "spec.md")[1])
    step("reconfirm", cli.flow(project, "confirm", "align-2", **reconfirm), refused=False)
    step("plan", cli.run("update", project, "-", "--expected-revision", 0, stdin=json.dumps(plan)), refused=False)

    worktree = (repository.parent / f"{repository.name}.worktrees" / "p1").resolve()
    git(repository, "worktree", "add", "-q", "-b", "feat/p1-current", str(worktree))
    step("record-worktree", cli.flow(
        project, "worktree", "wt-record", operation="record", path=str(worktree), branch="feat/p1-current",
        confirmation={"source": "user reply in the test", "response": "yes"}), refused=False)
    assert state_of(project)["working_directory"] == str(worktree)

    step("start", cli.run("task", project, "start", "T01", "--expected-revision", state_of(project)["revision"]),
         refused=False)

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
    step("keep-worktree", cli.flow(project, "worktree", "wt-keep", operation="close", path=str(worktree),
                                   decision="keep", confirmation={"source": "user", "response": "keep it"}),
         refused=False)
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
        "plan-before-alignment", "plan-with-stale-agreement", "failing-check", "update-done-on-failed-record",
        "close-with-undecided-worktree",
    ]


def test_both_launcher_surfaces_agree_step_for_step(tmp_path: Path) -> None:
    outcomes = [run_lifecycle(tmp_path / label, surface) for label, surface in zip("ab", SURFACES, strict=True)]
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0]) == 18, "a shortened run would agree trivially"


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
