"""Tests for the plugin's host-specific command entry points.

Claude Code invokes the top-level `bin/` wrappers from PATH. Codex resolves the launchers from the
exact loaded skill directory. Both surfaces must reach the same scripts without consulting cwd.

These run every entry point as a process, which is the only way to test shell launchers honestly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.conftest import REPO_ROOT

BIN = REPO_ROOT / "plugins/research/bin"
SKILL_SCRIPTS = REPO_ROOT / "plugins/research/skills/project/scripts"
WRAPPERS = {"research-project": "manage_workspace.py", "research-validate": "validate_workspace.py"}
ENTRY_SURFACES = (BIN, SKILL_SCRIPTS)


def _run(
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env, check=False)


class WrapperShapeTests(unittest.TestCase):
    def test_both_entry_surfaces_exist_and_are_executable(self) -> None:
        for surface in ENTRY_SURFACES:
            for name in WRAPPERS:
                launcher = surface / name
                self.assertTrue(launcher.is_file(), f"{launcher} is missing")
                self.assertTrue(os.access(launcher, os.X_OK), f"{launcher} is not executable")

    def test_top_level_wrappers_delegate_to_the_skill_launchers(self) -> None:
        for name in WRAPPERS:
            self.assertIn(f"scripts/{name}", (BIN / name).read_text(encoding="utf-8"))

    def test_each_skill_launcher_delegates_to_its_python_script(self) -> None:
        for name, script in WRAPPERS.items():
            self.assertIn(script, (SKILL_SCRIPTS / name).read_text(encoding="utf-8"))

    def test_the_underlying_scripts_stay_directly_invocable(self) -> None:
        # The wrappers are additive. The Python 3.9 job and the suite call the scripts directly.
        for script in WRAPPERS.values():
            self.assertTrue((SKILL_SCRIPTS / script).is_file())


class WrapperExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_help_exits_zero_for_both_entry_surfaces(self) -> None:
        for surface in ENTRY_SURFACES:
            for name in WRAPPERS:
                result = _run([str(surface / name), "--help"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_manage_exposes_the_subcommands_added_for_this_work(self) -> None:
        result = _run([str(SKILL_SCRIPTS / "research-project"), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for subcommand in ("init", "commit", "rebuild-index", "record-evidence", "find-roots", "migrate"):
            self.assertIn(subcommand, result.stdout)

    def test_a_real_subcommand_runs_through_the_codex_surface(self) -> None:
        workspace = self.root / "ws"
        workspace.mkdir()
        target = self.root / "target"
        target.mkdir()
        initialized = _run(
            [
                str(SKILL_SCRIPTS / "research-project"),
                "init",
                str(workspace),
                "--title",
                "Wrapper project",
                "--working-directory",
                str(target),
            ]
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        project_dir = Path(initialized.stdout.strip())
        self.assertEqual(
            json.loads((project_dir / "project.json").read_text(encoding="utf-8"))["status"],
            "ALIGNING",
        )
        validated = _run([str(SKILL_SCRIPTS / "research-validate"), str(project_dir)])
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_the_caller_s_directory_does_not_decide_which_script_runs(self) -> None:
        decoy = self.root / "decoy" / "skills" / "project" / "scripts"
        decoy.mkdir(parents=True)
        (decoy / "manage_workspace.py").write_text("raise SystemExit('decoy ran')\n", encoding="utf-8")
        result = _run([str(SKILL_SCRIPTS / "research-project"), "--help"], cwd=str(decoy.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("decoy ran", result.stderr)

    def test_a_symlinked_launcher_still_finds_its_script(self) -> None:
        link = self.root / "research-project"
        link.symlink_to(SKILL_SCRIPTS / "research-project")
        result = _run([str(link), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_the_wrapper_runs_when_python3_is_the_system_interpreter(self) -> None:
        # The scenario is a PATH whose python3 is the platform interpreter rather than the dev
        # virtualenv's, which is how a Claude session actually reaches these scripts.
        #
        # The interpreter's version is deliberately not asserted here. It used to require "Python
        # 3.9" — true of /usr/bin/python3 on macOS, but ubuntu-latest ships 3.12, so the assertion
        # described the machine the suite happened to run on rather than anything the wrapper does.
        # The 3.9 floor for the shipped scripts is enforced where it can be enforced honestly: the
        # python39-compat CI job, which runs them under a real 3.9.
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():  # pragma: no cover - platform without a system python
            self.skipTest("no /usr/bin/python3")
        path_directory = self.root / "bin"
        path_directory.mkdir()
        (path_directory / "python3").symlink_to(system_python)
        environment = dict(os.environ, PATH=f"{path_directory}:{os.defpath}")
        result = _run([str(SKILL_SCRIPTS / "research-project"), "--help"], env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_a_wrapper_without_its_script_fails_with_a_clear_message(self) -> None:
        orphan_bin = self.root / "plugin" / "bin"
        orphan_bin.mkdir(parents=True)
        copied = orphan_bin / "research-project"
        shutil.copy2(BIN / "research-project", copied)
        result = _run([str(copied), "--help"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("is missing", result.stderr)

    def test_a_skill_launcher_without_its_script_fails_with_a_clear_message(self) -> None:
        orphan_scripts = self.root / "plugin" / "skills" / "project" / "scripts"
        orphan_scripts.mkdir(parents=True)
        copied = orphan_scripts / "research-project"
        shutil.copy2(SKILL_SCRIPTS / "research-project", copied)
        result = _run([str(copied), "--help"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("manage_workspace.py is missing", result.stderr)

    def test_a_wrapper_without_python3_says_so(self) -> None:
        # A PATH holding everything the wrapper itself needs, and no python3. An empty PATH would
        # only prove that `env` cannot find bash (exit 127), which says nothing about the wrapper.
        without_python = self.root / "no-python"
        without_python.mkdir()
        for tool in ("bash", "dirname", "readlink"):
            located = shutil.which(tool)
            if located is None:  # pragma: no cover - a POSIX box without these does not exist
                self.skipTest(f"{tool} is not available")
            (without_python / tool).symlink_to(located)
        environment = dict(os.environ, PATH=str(without_python))
        result = _run([str(SKILL_SCRIPTS / "research-project"), "--help"], env=environment)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("python3", result.stderr)

    def test_the_exit_code_of_the_script_reaches_the_caller(self) -> None:
        # `exec` matters: a wrapper that swallowed a non-zero exit would defeat record-evidence.
        result = _run([str(SKILL_SCRIPTS / "research-validate"), str(self.root / "nonexistent")])
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
