from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MANAGER = REPO_ROOT / "plugins/research/skills/workbench/scripts/manage_workspace.py"
VALIDATOR = REPO_ROOT / "plugins/research/skills/workbench/scripts/validate_workspace.py"


class CliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace_root = self.root / "workspace"
        self.target = self.root / "target"
        self.target.mkdir()

    def run_cli(self, *arguments: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, *[str(argument) for argument in arguments]]
        return subprocess.run(command, check=check, capture_output=True, text=True)

    @staticmethod
    def task(status: str) -> dict[str, object]:
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

    def commit(self, project_dir: Path, state: dict[str, object], expected_revision: int) -> dict[str, object]:
        candidate = self.root / f"candidate-{expected_revision}.json"
        candidate.write_text(json.dumps(state), encoding="utf-8")
        self.run_cli(MANAGER, "commit", project_dir, candidate, "--expected-revision", expected_revision)
        return json.loads((project_dir / "project.json").read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
