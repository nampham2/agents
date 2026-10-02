"""A stale launcher says so.

An update applies when the next session starts, so a long-lived shell keeps running the old copy
while two versions write the same shared files. The failures read as data problems (a size budget,
an unknown field) and cost a project each before anyone compared versions. These tests pin that the
line appears exactly when it is true, and that it is silent in every case where it cannot be sure.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import manage_workspace
import validate_workspace
import workspace_lib
from workspace_lib import MEMORY_INDEX_MAX_BYTES, ValidationReport, _unexpected_fields, memory_findings

from tests.conftest import REPO_ROOT

PLUGIN = REPO_ROOT / "plugins/research"

# Resolved when used, so a build without the feature fails each test instead of failing to import.
NEWER_PLUGIN_HINT = getattr(workspace_lib, "NEWER_PLUGIN_HINT", "<absent in this build>")


def launcher_version_warning(config_dir: "Path | None" = None, running_root: "Path | None" = None) -> str:
    return workspace_lib.launcher_version_warning(config_dir, running_root)


def _record(config: Path, plugins: object) -> None:
    (config / "plugins").mkdir(parents=True, exist_ok=True)
    (config / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )


class _CacheFixture(unittest.TestCase):
    """A fake host config directory holding a cached copy of the plugin and an install record."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name).resolve() / "claude"
        self.cache = self.config / "plugins" / "cache" / "agents" / "research"
        self.running = self.cache / "0.1.0"
        self.installed = self.cache / "0.2.0"
        shutil.copytree(PLUGIN, self.running, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.installed.mkdir(parents=True)

    def record(self, **overrides: object) -> None:
        install = {"scope": "user", "installPath": str(self.installed), "version": "0.2.0", **overrides}
        _record(self.config, {"research@agents": [install]})


class LauncherVersionWarningTests(_CacheFixture):
    def warn(self, root: "Path | None" = None) -> str:
        return launcher_version_warning(self.config, root or self.running)

    def test_a_cached_copy_other_than_the_installed_one_is_named_with_both_versions(self) -> None:
        self.record()
        message = self.warn()
        self.assertIn("0.1.0", message)
        self.assertIn("0.2.0", message)
        self.assertIn(str(self.installed), message)
        self.assertIn("restarts", message)

    def test_the_installed_copy_itself_is_silent(self) -> None:
        self.record()
        self.assertEqual(self.warn(self.installed), "")

    def test_a_working_tree_is_silent_even_when_a_record_exists(self) -> None:
        self.record()
        self.assertEqual(self.warn(REPO_ROOT / "plugins/research"), "")

    def test_a_host_without_the_record_is_silent(self) -> None:
        # Codex has no installed_plugins.json; neither does a fresh config directory.
        self.assertEqual(self.warn(), "")

    def test_an_install_in_a_different_cache_directory_is_not_a_version_of_this_plugin(self) -> None:
        other = self.config / "plugins" / "cache" / "elsewhere" / "research" / "9.9.9"
        other.mkdir(parents=True)
        self.record(installPath=str(other))
        self.assertEqual(self.warn(), "")

    def test_only_research_entries_are_considered(self) -> None:
        _record(
            self.config,
            {
                "metasearch@agents": [{"installPath": str(self.installed)}],
                "research@agents": "not a list",
            },
        )
        self.assertEqual(self.warn(), "")

    def test_an_unreadable_or_unfamiliar_record_is_silent_not_an_error(self) -> None:
        path = self.config / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ("{not json", "[]", '{"plugins": {"research@agents": [{"scope": "user"}]}}'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(self.warn(), "")

    def test_the_default_config_directory_honours_claude_config_dir(self) -> None:
        self.record()
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.config)}):
            self.assertIn("0.2.0", launcher_version_warning(None, self.running))
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ""}), patch.object(
            workspace_lib.Path, "home", return_value=self.config.parent / "nowhere"
        ):
            self.assertEqual(launcher_version_warning(None, self.running), "")

    def test_the_default_root_is_this_checkout_and_is_silent(self) -> None:
        self.assertEqual(launcher_version_warning(self.config), "")


class LauncherProcessTests(_CacheFixture):
    """Run both real entry points as a cached, out-of-date copy would be run."""

    def run_tool(self, tool: str, *arguments: str, env_extra: "dict[str, str] | None" = None):
        environment = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "CLAUDE_CONFIG_DIR": str(self.config)}
        environment.update(env_extra or {})
        return subprocess.run(
            [str(self.running / "bin" / tool), *arguments], capture_output=True, text=True, env=environment
        )

    def init(self, name: str):
        base = Path(self.temporary.name)
        target = base / f"target-{name}"
        target.mkdir()
        return self.run_tool(
            "research-project", "init", str(base / f"workspace-{name}"), "--title", "t",
            "--working-directory", str(target), "--create-root", "--json",
        )

    def test_both_commands_warn_on_stderr_and_keep_stdout_clean(self) -> None:
        self.record()
        created = self.init("warned")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("WARNING: this command is running research plugin 0.1.0", created.stderr)
        self.assertEqual(json.loads(created.stdout)["revision"], 0, "stdout must stay pure JSON")
        validated = self.run_tool("research-validate", json.loads(created.stdout)["project_directory"])
        self.assertIn("WARNING: this command is running research plugin 0.1.0", validated.stderr)
        self.assertNotIn("WARNING: this command", validated.stdout)

    def test_no_warning_when_the_running_copy_is_the_installed_one(self) -> None:
        self.record(installPath=str(self.running))
        result = self.init("current")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING: this command", result.stderr)

    def test_no_warning_without_a_host_record(self) -> None:
        result = self.init("unrecorded")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING: this command", result.stderr)


class EntryPointWarningTests(unittest.TestCase):
    """The same line, reached in-process so that the entry points' own branches are measured."""

    def stderr_of(self, module: ModuleType, argv: list[str], warning: str) -> str:
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(module, "launcher_version_warning", return_value=warning),
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(io.StringIO()),
            tempfile.TemporaryDirectory() as directory,
        ):
            argv.append(directory)
            module.main()
        return stderr.getvalue()

    def test_research_project_prints_the_warning_before_running(self) -> None:
        text = self.stderr_of(manage_workspace, ["research-project", "find-roots"], "running 0.1.0, installed 0.2.0")
        self.assertIn("WARNING: running 0.1.0, installed 0.2.0", text)

    def test_research_validate_prints_the_warning_before_validating(self) -> None:
        text = self.stderr_of(validate_workspace, ["research-validate"], "running 0.1.0, installed 0.2.0")
        self.assertIn("WARNING: running 0.1.0, installed 0.2.0", text)

    def test_neither_prints_anything_when_there_is_nothing_to_say(self) -> None:
        for module, argv in (
            (manage_workspace, ["research-project", "find-roots"]),
            (validate_workspace, ["research-validate"]),
        ):
            with self.subTest(module=module.__name__):
                self.assertNotIn("WARNING: running", self.stderr_of(module, argv, ""))


class VersionSkewInErrorsTests(unittest.TestCase):
    def test_an_unknown_field_error_says_a_newer_plugin_may_have_written_it(self) -> None:
        report = ValidationReport()
        _unexpected_fields({"started_at": 1, "id": "T01"}, {"id"}, "task T01", report)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("task T01: unexpected fields: started_at", report.errors[0])
        self.assertIn(NEWER_PLUGIN_HINT, report.errors[0])

    def test_the_hint_names_both_remedies(self) -> None:
        self.assertIn("update research@agents", NEWER_PLUGIN_HINT)
        self.assertIn("working-tree launcher", NEWER_PLUGIN_HINT)

    def test_an_unsupported_schema_version_carries_the_hint_too(self) -> None:
        self.assertIn(NEWER_PLUGIN_HINT, workspace_lib.SCHEMA_UNSUPPORTED)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "project.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            report = workspace_lib.validate_project(project)
        self.assertTrue(any("unsupported schema_version: 99" in e and NEWER_PLUGIN_HINT in e for e in report.errors))


class BudgetErrorNamesSkewFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "MEMORY.md").write_text("x" * (MEMORY_INDEX_MAX_BYTES + 1), encoding="utf-8")

    def errors(self) -> list[str]:
        return [error for error in memory_findings(self.workspace).errors if "above the" in error]

    def test_a_stale_launcher_is_named_in_the_budget_error(self) -> None:
        with patch.object(workspace_lib, "launcher_version_warning", return_value="running 0.1.0, installed 0.2.0"):
            (error,) = self.errors()
        self.assertIn("merge or retire topics", error)
        self.assertIn("may be version skew", error)
        self.assertIn("running 0.1.0, installed 0.2.0", error)

    def test_without_skew_the_budget_error_is_unchanged(self) -> None:
        with patch.object(workspace_lib, "launcher_version_warning", return_value=""):
            (error,) = self.errors()
        self.assertNotIn("version skew", error)
        self.assertTrue(error.endswith("rather than appending pointers"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
