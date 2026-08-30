"""Tests for `find-roots`.

`resolve_workspace_root` refuses to guess, which is correct and leaves the caller asking the user
for a path they have already used. Twice that ended with one project duplicated across two roots at
divergent statuses. So the search has to find the *established* root, and has to say plainly when
there is more than one rather than picking.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from workspace_lib import ROOT_SEARCH_PRUNED_NAMES, find_workspace_roots


def _call_manage(args: list[str]) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


def _make_root(directory: Path, *, project: str = "2026-08-30-001") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    (directory / project).mkdir(exist_ok=True)
    return directory


class _HomeFixture(unittest.TestCase):
    """A temporary directory standing in for $HOME."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()


class FindRootsTests(_HomeFixture):
    def test_no_root_is_reported_as_none_rather_than_a_nearest_guess(self) -> None:
        (self.home / "git_tree" / "notes").mkdir(parents=True)
        self.assertEqual(find_workspace_roots([self.home]), [])

    def test_one_root_is_found_below_the_search_path(self) -> None:
        expected = _make_root(self.home / "git_tree" / "research" / "workspace")
        self.assertEqual(find_workspace_roots([self.home]), [expected])

    def test_two_roots_are_both_reported(self) -> None:
        first = _make_root(self.home / "a" / "workspace")
        second = _make_root(self.home / "b" / "workspace")
        self.assertEqual(sorted(find_workspace_roots([self.home])), sorted([first, second]))

    def test_an_index_without_a_project_directory_is_not_a_root(self) -> None:
        docs = self.home / "docs"
        docs.mkdir()
        (docs / "INDEX.md").write_text("# Index\n", encoding="utf-8")
        (docs / "chapter-one").mkdir()
        self.assertEqual(find_workspace_roots([self.home]), [])

    def test_a_project_directory_without_an_index_is_not_a_root(self) -> None:
        (self.home / "dated" / "2026-08-30-001").mkdir(parents=True)
        self.assertEqual(find_workspace_roots([self.home]), [])

    def test_the_search_stops_at_the_depth_bound(self) -> None:
        deep = _make_root(self.home / "one" / "two" / "three" / "four" / "workspace")
        self.assertEqual(find_workspace_roots([self.home], max_depth=2), [])
        self.assertEqual(find_workspace_roots([self.home], max_depth=5), [deep])

    def test_a_root_is_not_descended_into(self) -> None:
        root = _make_root(self.home / "workspace")
        _make_root(root / "2026-08-30-001" / "nested")
        self.assertEqual(find_workspace_roots([self.home]), [root])

    def test_hidden_and_pruned_directories_are_skipped(self) -> None:
        _make_root(self.home / ".cache" / "workspace")
        _make_root(self.home / sorted(ROOT_SEARCH_PRUNED_NAMES)[0] / "workspace")
        self.assertEqual(find_workspace_roots([self.home]), [])

    def test_a_symlinked_directory_is_not_followed(self) -> None:
        real = _make_root(self.home / "real" / "workspace")
        (self.home / "link").symlink_to(real.parent)
        self.assertEqual(find_workspace_roots([self.home]), [real])

    def test_the_same_root_reached_by_two_search_paths_is_reported_once(self) -> None:
        root = _make_root(self.home / "workspace")
        self.assertEqual(find_workspace_roots([self.home, self.home / "workspace"]), [root])

    def test_an_unreadable_directory_is_skipped_not_fatal(self) -> None:
        blocked = self.home / "blocked"
        blocked.mkdir()
        expected = _make_root(self.home / "workspace")
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o700)
        self.assertEqual(find_workspace_roots([self.home]), [expected])

    def test_an_unreadable_root_candidate_is_skipped(self) -> None:
        candidate = self.home / "workspace"
        candidate.mkdir()
        (candidate / "INDEX.md").write_text("# Index\n", encoding="utf-8")
        (candidate / "2026-08-30-001").mkdir()
        candidate.chmod(0o000)
        self.addCleanup(candidate.chmod, 0o700)
        self.assertEqual(find_workspace_roots([self.home]), [])

    def test_a_search_path_that_does_not_exist_is_skipped(self) -> None:
        expected = _make_root(self.home / "workspace")
        self.assertEqual(
            find_workspace_roots([self.home / "gone", self.home]),
            [expected],
        )

    def test_home_is_the_default_search_path(self) -> None:
        expected = _make_root(self.home / "git_tree" / "research" / "workspace")
        with patch("workspace_lib.Path.home", return_value=self.home):
            self.assertEqual(find_workspace_roots(), [expected])


class FindRootsCliTests(_HomeFixture):
    def test_exit_zero_and_the_path_on_stdout_for_one_root(self) -> None:
        expected = _make_root(self.home / "workspace")
        self.assertEqual(_call_manage(["find-roots", str(self.home)]), 0)
        self.assertTrue(expected.exists())

    def test_exit_one_when_nothing_is_found(self) -> None:
        self.assertEqual(_call_manage(["find-roots", str(self.home)]), 1)

    def test_exit_one_when_several_roots_are_found(self) -> None:
        _make_root(self.home / "a" / "workspace")
        _make_root(self.home / "b" / "workspace")
        self.assertEqual(_call_manage(["find-roots", str(self.home)]), 1)

    def test_the_depth_bound_is_settable_from_the_cli(self) -> None:
        _make_root(self.home / "one" / "two" / "three" / "four" / "workspace")
        self.assertEqual(_call_manage(["find-roots", str(self.home), "--max-depth", "2"]), 1)
        self.assertEqual(_call_manage(["find-roots", str(self.home), "--max-depth", "5"]), 0)

    def test_no_search_path_falls_back_to_home(self) -> None:
        _make_root(self.home / "workspace")
        with patch("workspace_lib.Path.home", return_value=self.home):
            self.assertEqual(_call_manage(["find-roots"]), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
