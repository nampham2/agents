"""Tests for workspace-root resolution, root-creation policy, and project-ID shape.

These three behaviours exist because of one incident: two workspace roots on the same machine each
held a project with the same ID and a different status, and one project directory had a malformed
name that no commit could rename. Resolution refuses to guess, creation refuses to be implicit, and
validation reports a name that cannot be canonical.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from workspace_lib import (
    WORKSPACE_ROOT_ENV_VAR,
    WorkspaceError,
    allocate_project,
    is_canonical_project_id,
    resolve_workspace_root,
    validate_project,
)


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


class ResolveWorkspaceRootTests(unittest.TestCase):
    def test_explicit_path_wins_over_the_environment(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: "/from/env"}):
            self.assertEqual(Path("/explicit"), resolve_workspace_root(Path("/explicit")))

    def test_environment_is_used_when_no_path_is_given(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: "/from/env"}):
            self.assertEqual(Path("/from/env"), resolve_workspace_root(None))

    def test_blank_environment_value_is_not_a_root(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: "   "}):
            with self.assertRaises(WorkspaceError) as caught:
                resolve_workspace_root(None)
        self.assertIn(WORKSPACE_ROOT_ENV_VAR, str(caught.exception))

    def test_missing_root_and_missing_variable_is_an_actionable_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WorkspaceError) as caught:
                resolve_workspace_root(None)
        message = str(caught.exception)
        self.assertIn(WORKSPACE_ROOT_ENV_VAR, message)
        # The absence of a cwd fallback is the point of the function, so the message says so.
        self.assertIn("working directory is never assumed", message)

    def test_user_home_is_expanded_from_both_sources(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: "~/ws"}):
            self.assertEqual(Path.home() / "ws", resolve_workspace_root(None))
        self.assertEqual(Path.home() / "ws", resolve_workspace_root(Path("~/ws")))


class RootCreationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()

    def test_missing_root_is_refused_without_the_flag(self) -> None:
        missing = self.root / "typo"
        with self.assertRaises(WorkspaceError) as caught:
            allocate_project(missing, title="T", working_directory=self.target)
        self.assertIn("--create-root", str(caught.exception))
        self.assertFalse(missing.exists(), "a refused allocation must not leave a root behind")

    def test_missing_root_is_created_with_the_flag(self) -> None:
        missing = self.root / "deliberate"
        project_dir = allocate_project(missing, title="T", working_directory=self.target, create_root=True)
        self.assertTrue(project_dir.is_dir())
        self.assertTrue((missing / "INDEX.md").is_file())

    def test_existing_root_needs_no_flag(self) -> None:
        existing = self.root / "ws"
        existing.mkdir()
        self.assertTrue(allocate_project(existing, title="T", working_directory=self.target).is_dir())

    def test_a_file_in_place_of_a_root_is_refused_even_with_the_flag(self) -> None:
        not_a_dir = self.root / "file"
        not_a_dir.write_text("not a workspace\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError) as caught:
            allocate_project(not_a_dir, title="T", working_directory=self.target, create_root=True)
        self.assertIn("not a directory", str(caught.exception))

    def test_title_is_still_validated_before_the_root(self) -> None:
        # Ordering matters for the error the caller sees: an empty title must not be reported as a
        # missing workspace.
        with self.assertRaises(WorkspaceError) as caught:
            allocate_project(self.root / "typo", title="", working_directory=self.target)
        self.assertIn("title", str(caught.exception))


class ManageCLIRootResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def _init(self, *args: str) -> int:
        return _call_manage(["init", *args, "--title", "T", "--working-directory", str(self.target)])

    def test_init_reads_the_environment_when_no_root_is_passed(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: str(self.workspace)}):
            self.assertEqual(0, self._init())
        self.assertEqual(1, len(list(self.workspace.glob("2*-*-*-0*"))))

    def test_init_without_a_root_or_a_variable_fails(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(1, self._init())

    def test_init_prefers_an_explicit_root_over_the_variable(self) -> None:
        explicit = self.root / "explicit"
        explicit.mkdir()
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: str(self.workspace)}):
            self.assertEqual(0, self._init(str(explicit)))
        self.assertTrue(any(explicit.glob("2*-*-*-0*")))
        self.assertFalse(any(self.workspace.glob("2*-*-*-0*")))

    def test_init_refuses_a_missing_root_without_the_flag(self) -> None:
        missing = self.root / "typo"
        self.assertEqual(1, self._init(str(missing)))
        self.assertFalse(missing.exists())

    def test_init_creates_a_missing_root_with_the_flag(self) -> None:
        missing = self.root / "deliberate"
        self.assertEqual(0, self._init(str(missing), "--create-root"))
        self.assertTrue(any(missing.glob("2*-*-*-0*")))

    def test_rebuild_index_reads_the_environment(self) -> None:
        with patch.dict(os.environ, {WORKSPACE_ROOT_ENV_VAR: str(self.workspace)}):
            self.assertEqual(0, _call_manage(["rebuild-index"]))
        self.assertTrue((self.workspace / "INDEX.md").is_file())

    def test_rebuild_index_refuses_a_missing_root_rather_than_creating_one(self) -> None:
        missing = self.root / "gone"
        self.assertEqual(1, _call_manage(["rebuild-index", str(missing)]))
        self.assertFalse(missing.exists())

    def test_rebuild_index_without_a_root_or_a_variable_fails(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(1, _call_manage(["rebuild-index"]))


class ProjectIdShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()

    def test_canonical_ids_are_recognised(self) -> None:
        self.assertTrue(is_canonical_project_id("2026-08-29-001"))

    def test_malformed_ids_are_rejected(self) -> None:
        for name in ("2026-08--001", "2026-8-29-001", "2026-08-29-1", "2026-08-29-0001", "project", ""):
            with self.subTest(name=name):
                self.assertFalse(is_canonical_project_id(name))

    def test_allocation_always_produces_a_canonical_id(self) -> None:
        project_dir = allocate_project(self.workspace, title="T", working_directory=self.target)
        self.assertTrue(is_canonical_project_id(project_dir.name))

    def test_allocation_refuses_to_create_a_non_canonical_id(self) -> None:
        # Defence in depth: the date prefix is generated, so this only fires if the clock or locale
        # hands back something unexpected. Better a refusal than an unrenameable directory.
        with patch("workspace_lib.is_canonical_project_id", return_value=False):
            with self.assertRaises(WorkspaceError) as caught:
                allocate_project(self.workspace, title="T", working_directory=self.target)
        self.assertIn("non-canonical project ID", str(caught.exception))

    def test_validation_warns_about_a_malformed_directory_name(self) -> None:
        project_dir = allocate_project(self.workspace, title="T", working_directory=self.target)
        renamed = self.workspace / "2026-08--001"
        project_dir.rename(renamed)
        state_path = renamed / "project.json"
        state_path.write_text(
            state_path.read_text(encoding="utf-8").replace(project_dir.name, renamed.name),
            encoding="utf-8",
        )
        report = validate_project(renamed)
        self.assertEqual([], report.errors, "a malformed name is reported, not treated as invalid state")
        self.assertTrue(any("canonical" in warning for warning in report.warnings))

    def test_validation_is_silent_for_a_canonical_directory_name(self) -> None:
        project_dir = allocate_project(self.workspace, title="T", working_directory=self.target)
        report = validate_project(project_dir)
        self.assertEqual([], [warning for warning in report.warnings if "canonical" in warning])


if __name__ == "__main__":
    unittest.main()
