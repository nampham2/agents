"""Tests for the project skill manage/validate entrypoint scripts — achieves 100% line coverage."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
import validate_workspace
from workspace_lib import WorkspaceError, allocate_project

from tests.conftest import MANAGER

TIMESTAMP = "2026-08-28T10:00:00+02:00"


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


def _call_validate(args: list[str]) -> int:
    with patch.object(sys, "argv", ["validate", *args]):
        return validate_workspace.main()


class ManageCLIInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def test_init_creates_project(self) -> None:
        result = _call_manage(
            [
                "init",
                str(self.workspace),
                "--title",
                "Test Project",
                "--working-directory",
                str(self.target),
            ]
        )
        self.assertEqual(0, result)
        dirs = [d for d in self.workspace.iterdir() if d.is_dir() and not d.name.startswith(".")]
        self.assertEqual(1, len(dirs))

    def test_init_workspace_error_returns_1(self) -> None:
        with patch("manage_workspace.allocate_project", side_effect=WorkspaceError("bad title")):
            result = _call_manage(
                [
                    "init",
                    str(self.workspace),
                    "--title",
                    "",
                    "--working-directory",
                    str(self.target),
                ]
            )
        self.assertEqual(1, result)


class ManageCLICommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.project_dir = allocate_project(self.workspace, title="T", working_directory=self.target)

    def test_commit_advances_revision(self) -> None:
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        candidate = copy.deepcopy(state)
        candidate["title"] = "Updated"
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        result = _call_manage(
            [
                "commit",
                str(self.project_dir),
                str(candidate_path),
                "--expected-revision",
                "0",
            ]
        )
        self.assertEqual(0, result)

    def test_commit_workspace_error_returns_1(self) -> None:
        with patch("manage_workspace.commit_candidate", side_effect=WorkspaceError("conflict")):
            result = _call_manage(
                [
                    "commit",
                    str(self.project_dir),
                    str(self.root / "c.json"),
                    "--expected-revision",
                    "0",
                ]
            )
        self.assertEqual(1, result)


class ManageCLIRebuildIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir()

    def test_rebuild_index_returns_0(self) -> None:
        result = _call_manage(["rebuild-index", str(self.workspace)])
        self.assertEqual(0, result)

    def test_rebuild_index_workspace_error_returns_1(self) -> None:
        with patch("manage_workspace.rebuild_index", side_effect=WorkspaceError("locked")):
            result = _call_manage(["rebuild-index", str(self.workspace)])
        self.assertEqual(1, result)


class ManageCLIMigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()

    def _write_v2_project(self, status: str = "PLANNING") -> Path:
        project_dir = self.root / "ws" / "project"
        project_dir.mkdir(parents=True)
        state = {
            "schema_version": 2,
            "project": "project",
            "title": "T",
            "status": status,
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.target),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [],
        }
        (project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        return project_dir

    def test_migrate_preview_returns_0_for_valid_candidate(self) -> None:
        project_dir = self._write_v2_project()
        result = _call_manage(["migrate", str(project_dir)])
        self.assertEqual(0, result)

    def test_migrate_preview_returns_1_when_candidate_has_errors(self) -> None:
        # DONE v2 with no tasks → migrated to v3 DONE with no tasks → validation error
        project_dir = self._write_v2_project(status="DONE")
        result = _call_manage(["migrate", str(project_dir)])
        self.assertEqual(1, result)

    def test_migrate_apply_returns_0(self) -> None:
        project_dir = self._write_v2_project()
        result = _call_manage(["migrate", str(project_dir), "--apply"])
        self.assertEqual(0, result)

    def test_migrate_workspace_error_returns_1(self) -> None:
        project_dir = self._write_v2_project()
        with patch("manage_workspace.apply_migration", side_effect=WorkspaceError("locked")):
            result = _call_manage(["migrate", str(project_dir), "--apply"])
        self.assertEqual(1, result)

    def test_migrate_candidate_workspace_error_returns_1(self) -> None:
        project_dir = self._write_v2_project()
        with patch("manage_workspace.migration_candidate", side_effect=WorkspaceError("no v2")):
            result = _call_manage(["migrate", str(project_dir)])
        self.assertEqual(1, result)


class ValidateCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def test_valid_project_returns_0(self) -> None:
        project_dir = allocate_project(self.workspace, title="Test", working_directory=self.target)
        result = _call_validate([str(project_dir)])
        self.assertEqual(0, result)

    def test_invalid_project_returns_1(self) -> None:
        invalid = self.root / "invalid"
        invalid.mkdir()
        result = _call_validate([str(invalid)])
        self.assertEqual(1, result)

    def test_validate_with_warnings_returns_0(self) -> None:
        project_dir = self.root / "v2proj"
        project_dir.mkdir()
        state = {
            "schema_version": 2,
            "project": "v2proj",
            "title": "T",
            "status": "PLANNING",
            "created": "2026-01-01",
            "updated": TIMESTAMP,
            "working_directory": str(self.target),
            "current_task": None,
            "review_cycle": 0,
            "tasks": [],
        }
        (project_dir / "project.json").write_text(json.dumps(state), encoding="utf-8")
        result = _call_validate([str(project_dir)])
        self.assertEqual(0, result)

    def test_validate_check_index_flag(self) -> None:
        project_dir = allocate_project(self.workspace, title="Test", working_directory=self.target)
        result = _call_validate([str(project_dir), "--check-index"])
        self.assertEqual(0, result)

    def test_validate_close_flag_fails_aligning_project(self) -> None:
        project_dir = allocate_project(self.workspace, title="Test", working_directory=self.target)
        result = _call_validate([str(project_dir), "--close"])
        self.assertEqual(1, result)

    def test_validate_allow_legacy_close_flag(self) -> None:
        project_dir = self.root / "legacy"
        project_dir.mkdir()
        (project_dir / "00_meta.yaml").write_text("schema: 1\n", encoding="utf-8")
        (project_dir / "02_task_plan.md").write_text("# Plan\n", encoding="utf-8")
        (project_dir / "reflection.md").write_text("# Reflection\n\nDone.\n", encoding="utf-8")
        result = _call_validate([str(project_dir), "--close", "--allow-legacy-close"])
        self.assertEqual(0, result)


class CLIMainBlockTest(unittest.TestCase):
    """The script runs standalone the way SKILL.md invokes it: `python3 manage_workspace.py ...`."""

    def test_script_runs_as_main_via_subprocess(self) -> None:
        workspace = Path(tempfile.mkdtemp())
        target = Path(tempfile.mkdtemp())
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(MANAGER),
                    "init",
                    str(workspace),
                    "--title",
                    "SubprocessTest",
                    "--working-directory",
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode)
        finally:
            import shutil

            shutil.rmtree(str(workspace), ignore_errors=True)
            shutil.rmtree(str(target), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
