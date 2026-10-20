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
WRAPPERS = {
    "research-project": "manage_workspace.py",
    "research-validate": "validate_workspace.py",
    "research-dashboard": "workspace_dashboard.py",
}
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
        # The wrappers are additive. The python-floor job and the suite call the scripts directly.
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

    def test_the_wrapper_follows_the_floor_when_python3_is_the_system_interpreter(self) -> None:
        # The scenario is a PATH whose python3 is the platform interpreter rather than the dev
        # virtualenv's, which is how a Claude session can reach these scripts.
        #
        # The system interpreter's version is not fixed: stock macOS has 3.9, which is below the floor,
        # and a Linux runner has a newer one. So the expectation follows the version the machine has:
        # a qualifying Python runs the script; a lower one is refused with the fix named. The branches
        # of the choice itself are tested with fake interpreters in test_launcher_python.py.
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():  # pragma: no cover - platform without a system python
            self.skipTest("no /usr/bin/python3")
        path_directory = self.root / "bin"
        path_directory.mkdir()
        (path_directory / "python3").symlink_to(system_python)
        environment = dict(os.environ, PATH=f"{path_directory}:{os.defpath}")
        probe = "import sys; raise SystemExit(sys.version_info < (3, 11))"
        qualifies = _run([str(system_python), "-c", probe]).returncode == 0
        result = _run([str(SKILL_SCRIPTS / "research-project"), "--help"], env=environment)
        if qualifies:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)
        elif shutil.which("uv", path=os.defpath):  # pragma: no cover - uv would fetch a Python
            self.skipTest("uv on the default path would fetch a Python")
        else:
            self.assertEqual(result.returncode, 1)
            self.assertIn("needs Python 3.11 or later", result.stderr)
