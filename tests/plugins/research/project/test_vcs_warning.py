"""Tests for the version-control warning on a workspace root.

The workspace is the record of what was decided and verified, and the real one has no history at
all: a mistaken delete, or a hand-edit of project.json that bypasses the transactional commit, is
unrecoverable. The tools report that and nothing more — they must never create a repository, which
would be a write outside the project directory that nobody authorized.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from workspace_lib import VCS_MARKERS, allocate_project, validate_project, vcs_warnings


class VcsWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_a_root_outside_any_repository_warns(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        warnings = vcs_warnings(workspace)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not under version control", warnings[0])
        self.assertIn("git init", warnings[0])

    def test_a_root_inside_a_repository_does_not_warn(self) -> None:
        repository = self.root / "repo"
        workspace = repository / "nested" / "workspace"
        workspace.mkdir(parents=True)
        (repository / ".git").mkdir()
        self.assertEqual(vcs_warnings(workspace), [])

    def test_a_root_that_is_itself_a_repository_does_not_warn(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / ".git").mkdir()
        self.assertEqual(vcs_warnings(workspace), [])

    def test_a_worktree_or_submodule_marker_file_counts(self) -> None:
        # Inside a worktree or submodule, `.git` is a file pointing elsewhere, not a directory.
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        self.assertEqual(vcs_warnings(workspace), [])

    def test_every_supported_marker_is_recognised(self) -> None:
        for marker in VCS_MARKERS:
            workspace = self.root / marker.lstrip(".")
            workspace.mkdir()
            (workspace / marker).mkdir()
            self.assertEqual(vcs_warnings(workspace), [], marker)

    def test_a_root_that_does_not_exist_is_not_warned_about(self) -> None:
        self.assertEqual(vcs_warnings(self.root / "gone"), [])

    def test_a_path_that_is_a_file_is_not_warned_about(self) -> None:
        target = self.root / "file"
        target.write_text("x\n", encoding="utf-8")
        self.assertEqual(vcs_warnings(target), [])

    def test_an_unresolvable_path_is_not_warned_about(self) -> None:
        with patch.object(Path, "resolve", side_effect=OSError("no")):
            self.assertEqual(vcs_warnings(self.root), [])

    def test_nothing_is_ever_created_by_the_check(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.assertTrue(vcs_warnings(workspace))
        self.assertEqual([child.name for child in workspace.iterdir()], [])
        for marker in VCS_MARKERS:
            self.assertFalse((workspace / marker).exists())


class VcsWarningReachesTheToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "target"
        self.target.mkdir()

    def _init(self, workspace: Path) -> subprocess.CompletedProcess[str]:
        from tests.conftest import MANAGER

        return subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "init",
                str(workspace),
                "--title",
                "VCS",
                "--working-directory",
                str(self.target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_init_warns_on_stderr_and_still_prints_the_project_path(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        result = self._init(workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not under version control", result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_dir())
        self.assertFalse((workspace / ".git").exists())

    def test_init_does_not_warn_inside_a_repository(self) -> None:
        repository = self.root / "repo"
        repository.mkdir()
        (repository / ".git").mkdir()
        workspace = repository / "workspace"
        workspace.mkdir()
        result = self._init(workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("not under version control", result.stderr)

    def test_init_warns_about_a_root_it_had_to_create(self) -> None:
        # --create-root means the directory did not exist a moment earlier, which is exactly when
        # the absence of history matters most.
        workspace = self.root / "brand-new"
        with patch.object(
            sys,
            "argv",
            [
                "manage",
                "init",
                str(workspace),
                "--title",
                "VCS",
                "--working-directory",
                str(self.target),
                "--create-root",
            ],
        ):
            self.assertEqual(manage_workspace.main(), 0)
        self.assertTrue(vcs_warnings(workspace))

    def test_validation_surfaces_the_warning_without_failing(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        project_dir = allocate_project(workspace, title="VCS", working_directory=self.target)
        report = validate_project(project_dir)
        self.assertTrue(report.valid, report.errors)
        self.assertTrue([w for w in report.warnings if "not under version control" in w])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
