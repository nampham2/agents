"""Tests for the project skill manage/validate entrypoint scripts — achieves 100% line coverage."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
import validate_workspace
from workspace_lib import WorkspaceError, allocate_project, is_canonical_project_id

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
        # `memory/` is scaffolded beside the project, so count project directories by their canonical
        # YYYY-MM-DD-NNN name rather than by "every directory that is not hidden".
        dirs = [d for d in self.workspace.iterdir() if d.is_dir() and is_canonical_project_id(d.name)]
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

    def _candidate(self, **changes: object) -> Path:
        state = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        state.update(changes)
        path = self.root / "dry-candidate.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_a_dry_run_reports_a_clean_candidate_and_writes_nothing(self) -> None:
        before = (self.project_dir / "project.json").read_bytes()
        result = _call_manage(
            ["commit", str(self.project_dir), str(self._candidate(status="PLANNING")), "--dry-run"]
        )
        self.assertEqual(0, result)
        self.assertEqual(before, (self.project_dir / "project.json").read_bytes())

    def test_a_dry_run_returns_one_when_the_commit_would_fail(self) -> None:
        result = _call_manage(
            [
                "commit",
                str(self.project_dir),
                str(self._candidate(status="REVIEW")),
                "--dry-run",
                "--expected-revision",
                "0",
            ]
        )
        self.assertEqual(1, result)

    def test_a_commit_without_an_expected_revision_is_refused(self) -> None:
        result = _call_manage(["commit", str(self.project_dir), str(self._candidate())])
        self.assertEqual(1, result)

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


class ManageCLIMemoryTests(unittest.TestCase):
    """The two memory subcommands, whose exit codes are what a caller reads before deciding.

    `search-memory` exits 0 on no hits on purpose: "nothing is recorded about this" is an answer, and
    a non-zero exit would make it indistinguishable from a broken search.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        (self.workspace / "memory").mkdir()

    def _promote(self, *args: str) -> int:
        return _call_manage(["promote-memory", *args, "--workspace-root", str(self.workspace)])

    def test_promoting_a_new_topic_creates_it_and_regenerates_the_index(self) -> None:
        result = self._promote(
            "uv-toolchain",
            "--body",
            "Bare pytest picks up the system interpreter.",
            "--description",
            "Run everything through uv",
            "--kind",
            "environment",
            "--scope",
            "any repo with a uv.lock",
            "--source",
            "2026-09-09-002",
        )
        self.assertEqual(0, result)
        self.assertTrue((self.workspace / "memory" / "uv-toolchain.md").is_file())
        self.assertIn("uv-toolchain", (self.workspace / "MEMORY.md").read_text(encoding="utf-8"))

    def test_a_body_can_come_from_a_file(self) -> None:
        body_file = self.root / "lesson.txt"
        body_file.write_text("The lesson, from a file.\n", encoding="utf-8")
        result = self._promote(
            "lock-discipline",
            "--body-file",
            str(body_file),
            "--description",
            "How the workspace locks compose",
            "--kind",
            "method",
            "--scope",
            "workspace_lib writers",
        )
        self.assertEqual(0, result)
        content = (self.workspace / "memory" / "lock-discipline.md").read_text(encoding="utf-8")
        self.assertIn("The lesson, from a file.", content)

    def test_a_body_can_come_from_stdin(self) -> None:
        with patch.object(sys, "stdin", io.StringIO("The lesson, piped in.\n")):
            result = self._promote(
                "terse-prose",
                "--body-file",
                "-",
                "--description",
                "Prefer plain sentences",
                "--kind",
                "preference",
                "--scope",
                "everything written for this user",
            )
        self.assertEqual(0, result)
        content = (self.workspace / "memory" / "terse-prose.md").read_text(encoding="utf-8")
        self.assertIn("The lesson, piped in.", content)

    def test_a_refused_promotion_returns_1(self) -> None:
        result = self._promote("bare-topic", "--body", "Something.")
        self.assertEqual(1, result)
        self.assertFalse((self.workspace / "memory" / "bare-topic.md").exists())

    def test_read_and_compact_memory_round_trip_through_the_cli(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO):
            self._promote("tiered", "--body", "Rule text.", "--description", "d", "--kind", "method", "--scope", "s")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(0, _call_manage(["read-memory", "tiered", "--workspace-root", str(self.workspace)]))
        before = json.loads(out.getvalue())
        self.assertEqual((before["tiered"], before["rule"], before["incident_count"]), (True, "Rule text.", 1))
        self.assertNotIn("incidents", before)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            result = _call_manage([
                "compact-memory", "tiered", "--rule", "Shorter.", "--expected-sha256", before["sha256"],
                "--keywords", "k1, k2", "--workspace-root", str(self.workspace),
            ])
        self.assertEqual(0, result)
        outcome = json.loads(out.getvalue())
        self.assertEqual(outcome["incident_count"], 2)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            _call_manage(["read-memory", "tiered", "--full", "--workspace-root", str(self.workspace)])
        after = json.loads(out.getvalue())
        self.assertEqual((after["rule"], after["keywords"], after["sha256"]), ("Shorter.", "k1, k2", outcome["sha256"]))
        self.assertIn("compaction (previous rule)", after["incidents"])
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            stale = _call_manage([
                "compact-memory", "tiered", "--rule", "Again.", "--expected-sha256", before["sha256"],
                "--workspace-root", str(self.workspace),
            ])
        self.assertEqual(1, stale)
        self.assertIn("changed since it was read", err.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO) as err, patch("sys.stdout", new_callable=io.StringIO):
            over = _call_manage([
                "compact-memory", "tiered", "--rule", "y" * 2100, "--expected-sha256", outcome["sha256"],
                "--workspace-root", str(self.workspace),
            ])
        self.assertEqual(0, over)
        self.assertIn("rule is", err.getvalue())

    def test_promotion_warns_when_a_legacy_topic_is_due_for_compaction(self) -> None:
        (self.workspace / "memory" / "big.md").write_text(
            "---\nname: big\ndescription: d\nkind: method\nscope: s\nsources: \nupdated: 2026-09-09\n---\n\n"
            + "x" * (8 * 1024 + 1) + "\n",
            encoding="utf-8",
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, self._promote("big", "--body", "More."))
        self.assertIn("compaction is due", err.getvalue())

    def test_search_prints_ranked_json_relative_to_the_root_and_returns_0(self) -> None:
        self._promote(
            "uv-toolchain",
            "--body",
            "Body text.",
            "--description",
            "Run everything through uv",
            "--kind",
            "environment",
            "--scope",
            "any repo with a uv.lock",
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            result = _call_manage(["search-memory", "everything through uv", "--workspace-root", str(self.workspace)])
        self.assertEqual(0, result)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["matches"][0]["topic"], "uv-toolchain")
        self.assertEqual(payload["matches"][0]["path"], "memory/uv-toolchain.md")
        self.assertNotIn("postmortems", payload)

    def test_search_with_no_hits_still_returns_0(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            with patch("sys.stdout", new_callable=io.StringIO):
                result = _call_manage(["search-memory", "nothing at all", "--workspace-root", str(self.workspace)])
        self.assertEqual(0, result)
        self.assertIn("No memory matches", err.getvalue())

    def test_search_with_an_empty_query_returns_1(self) -> None:
        result = _call_manage(["search-memory", "  ", "--workspace-root", str(self.workspace)])
        self.assertEqual(1, result)

    def test_rebuilding_reports_a_skipped_topic_without_failing(self) -> None:
        (self.workspace / "memory" / "half-written.md").write_text("---\nname: half-written\n", encoding="utf-8")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            result = _call_manage(["rebuild-index", str(self.workspace)])
        self.assertEqual(0, result)
        self.assertIn("memory topic skipped", err.getvalue())


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
