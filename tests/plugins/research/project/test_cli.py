from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.conftest import MANAGER, VALIDATOR


class _CliFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def run_cli(self, *arguments: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, *[str(argument) for argument in arguments]]
        return subprocess.run(command, check=check, capture_output=True, text=True)

    @staticmethod
    def task(status: str) -> dict[str, Any]:
        return {
            "id": "T01",
            "name": "Create output",
            "status": status,
            "depends_on": [],
            "outputs": [{"root": "target", "path": "result.txt", "required": True}],
            "success_criteria": "result.txt exists.",
            "verification": "Inspect result.txt.",
            "evidence": [],
            "effect": {"kind": "local_write", "description": "Create result.txt"},
            "authorization": {
                "required": False,
                "status": "not_required",
                "scope": None,
                "source": None,
                "authorized_at": None,
            },
            "receipts": [],
            "skip_reason": None,
            "block_reason": None,
        }

    def commit(self, project_dir: Path, state: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        candidate = self.root / f"candidate-{expected_revision}.json"
        candidate.write_text(json.dumps(state), encoding="utf-8")
        self.run_cli(MANAGER, "commit", project_dir, candidate, "--expected-revision", expected_revision)
        return json.loads((project_dir / "project.json").read_text(encoding="utf-8"))


class CliIntegrationTests(_CliFixture):
    def test_initialize_execute_close_and_validate(self) -> None:
        initialized = self.run_cli(
            MANAGER, "init", self.workspace_root, "--title", "CLI project", "--working-directory", self.target
        )
        project_dir = Path(initialized.stdout.strip())
        state = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual("ALIGNING", state["status"])
        self.assertIn(project_dir.name, (self.workspace_root / "INDEX.md").read_text(encoding="utf-8"))

        state["status"] = "PLANNING"
        state["tasks"] = [self.task("TODO")]
        state = self.commit(project_dir, state, expected_revision=0)

        state["status"] = "EXECUTING"
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]
        state = self.commit(project_dir, state, expected_revision=1)

        (self.target / "result.txt").write_text("complete\n", encoding="utf-8")
        (project_dir / "evidence.md").write_text("# Evidence\n\n## T01\n\nVerified result.txt.\n", encoding="utf-8")
        (project_dir / "reflection.md").write_text("# Reflection\n\nTransactional flow passed.\n", encoding="utf-8")
        state["status"] = "DONE"
        state["tasks"][0]["status"] = "DONE"
        state["tasks"][0]["evidence"] = [{"root": "workspace", "path": "evidence.md", "anchor": "T01"}]
        state["current_tasks"] = []
        state = self.commit(project_dir, state, expected_revision=2)
        self.assertEqual(3, state["revision"])

        validation = self.run_cli(VALIDATOR, project_dir, "--close", "--check-index")
        self.assertIn("Workspace valid", validation.stdout)


class DryRunCommitTests(_CliFixture):
    """`commit --dry-run` exists so one bad candidate costs one round trip instead of four.

    A real commit raises on the first problem it finds, which is right for a transaction: it must
    not keep working on state it has already rejected. The cost lands on whoever is assembling the
    candidate, because a stale revision hides the transition error, which hides the validation
    errors. These tests pin the two halves of the fix: every problem is reported together, and
    nothing about the project changes while it happens.
    """

    def setUp(self) -> None:
        super().setUp()
        initialized = self.run_cli(
            MANAGER, "init", self.workspace_root, "--title", "Dry run", "--working-directory", self.target
        )
        self.project_dir = Path(initialized.stdout.strip())
        state = self.state()
        state["status"] = "PLANNING"
        state["tasks"] = [self.task("TODO")]
        self.commit(self.project_dir, state, expected_revision=0)

    def state(self) -> dict[str, Any]:
        return json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))

    def dry_run(self, state: dict[str, Any], *extra: object) -> subprocess.CompletedProcess[str]:
        candidate = self.root / "dry-candidate.json"
        candidate.write_text(json.dumps(state), encoding="utf-8")
        before = (self.project_dir / "project.json").read_bytes()
        result = self.run_cli(
            MANAGER, "commit", self.project_dir, candidate, "--dry-run", *extra, check=False
        )
        self.assertEqual(
            before,
            (self.project_dir / "project.json").read_bytes(),
            "a dry run must leave canonical state byte-identical",
        )
        return result

    def test_every_problem_is_reported_in_one_pass(self) -> None:
        state = self.state()
        state["revision"] = 0  # built from a revision that is no longer current
        state["created"] = "2020-01-01T00:00:00+00:00"  # an immutable identity field
        state["status"] = "REVIEW"  # not reachable from PLANNING
        state["tasks"][0]["success_criteria"] = ""  # and the candidate is invalid on its own terms

        result = self.dry_run(state, "--expected-revision", 0)

        self.assertEqual(1, result.returncode)
        report = result.stderr
        self.assertIn("revision conflict: expected 0, found 1", report)
        self.assertIn("immutable field: created", report)
        self.assertIn("invalid project transition: PLANNING -> REVIEW", report)
        self.assertIn("success_criteria", report)

    def test_a_clean_candidate_is_reported_clean_and_then_commits(self) -> None:
        state = self.state()
        state["status"] = "EXECUTING"
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]

        result = self.dry_run(state, "--expected-revision", 1)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("would commit cleanly", result.stdout)
        self.assertEqual(1, self.state()["revision"], "the dry run must not advance the revision")
        # The negative control: a dry run that reports clean is worthless unless the same candidate
        # really commits.
        self.assertEqual(2, self.commit(self.project_dir, state, expected_revision=1)["revision"])

    def test_the_expected_revision_defaults_to_the_recorded_one(self) -> None:
        state = self.state()
        state["status"] = "EXECUTING"
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]

        result = self.dry_run(state)

        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_held_lock_does_not_block_a_dry_run(self) -> None:
        # Checking a candidate while another process is mid-commit is exactly when a coordinator
        # wants to check one, so the dry run must not queue behind the lock.
        lock = self.project_dir / ".project.lock"
        lock.mkdir()
        (lock / "owner.json").write_text('{"pid": 1}', encoding="utf-8")
        state = self.state()
        state["status"] = "EXECUTING"
        state["tasks"][0]["status"] = "RUNNING"
        state["current_tasks"] = ["T01"]

        result = self.dry_run(state, "--expected-revision", 1)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(lock.is_dir(), "the dry run must not touch, steal, or release the lock")

    def test_a_real_commit_still_requires_the_expected_revision(self) -> None:
        candidate = self.root / "no-revision.json"
        candidate.write_text(json.dumps(self.state()), encoding="utf-8")

        result = self.run_cli(MANAGER, "commit", self.project_dir, candidate, check=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("--expected-revision", result.stderr)
        self.assertEqual(1, self.state()["revision"])


if __name__ == "__main__":
    unittest.main()
