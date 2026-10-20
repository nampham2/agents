"""The launchers choose their Python: the newest qualifying one on PATH, then uv, else an error.

Every test runs a launcher as a process on a PATH that holds only fake interpreters and the few shell
tools the launcher needs, so the machine's own Python cannot decide the result. A fake interpreter
runs the launcher's real version probe against a spoofed `sys.version_info`, so the floor is tested
on the expression the launcher sends and not on a canned answer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

BIN = REPO_ROOT / "plugins/research/bin"
SKILL_SCRIPTS = (REPO_ROOT / "plugins/research/skills/project/scripts").resolve()
SELECTOR = SKILL_SCRIPTS / "python_select.sh"
SCRIPTS = {
    "research-project": "manage_workspace.py",
    "research-validate": "validate_workspace.py",
    "research-dashboard": "workspace_dashboard.py",
}
TOOLS = ("bash", "dirname", "readlink", "env")


class Sandbox:
    """A PATH of fake interpreters and the shell tools a launcher needs, and nothing else."""

    def __init__(self, root: Path) -> None:
        self.fakes = root / "fakes"
        self.tools = root / "tools"
        self.fakes.mkdir()
        self.tools.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found is not None, f"the suite needs {tool} on PATH"
            (self.tools / tool).symlink_to(found)

    def python(self, name: str, minor: int, *, starts: bool = True) -> None:
        """A fake `name` that reports Python 3.`minor`; with starts false it cannot run at all."""
        if starts:
            body = (
                "#!/usr/bin/env bash\n"
                'if [[ ${1:-} == -c ]]; then\n'
                f'  exec "{sys.executable}" -c "import sys; sys.version_info = (3, {minor}, 0); '
                'exec(sys.argv[1])" "$2"\n'
                "fi\n"
                f"echo 'ran:{name}'\n"
                "printf 'arg:%s\\n' \"$@\"\n"
            )
        else:
            body = "#!/usr/bin/env bash\nexit 127\n"
        path = self.fakes / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def broken(self, name: str) -> None:
        """A dangling symlink, as a removed interpreter leaves behind."""
        (self.fakes / name).symlink_to(self.fakes / "gone")

    def uv(self, *, exit_code: int = 0) -> None:
        body = (
            "#!/usr/bin/env bash\n"
            + ("echo 'uv failed: no network' >&2\n" if exit_code else "echo 'ran:uv'\n")
            + "printf 'arg:%s\\n' \"$@\"\n"
            + f"exit {exit_code}\n"
        )
        path = self.fakes / "uv"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run(self, launcher: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {"PATH": f"{self.fakes}:{self.tools}"}
        return subprocess.run(
            [str(launcher), *arguments], capture_output=True, text=True, env=environment, check=False,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _ran(result: subprocess.CompletedProcess[str]) -> str:
    """The name the fake reported, or an empty string when no interpreter ran the script."""
    return next((line.split(":", 1)[1] for line in result.stdout.splitlines() if line.startswith("ran:")), "")


def _arguments(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.split(":", 1)[1] for line in result.stdout.splitlines() if line.startswith("arg:")]


@pytest.mark.parametrize("launcher", sorted(SCRIPTS))
def test_each_launcher_runs_its_script_on_a_qualifying_python(sandbox: Sandbox, launcher: str) -> None:
    sandbox.python("python3", minor=12)
    result = sandbox.run(SKILL_SCRIPTS / launcher, "alpha", "two words")
    assert result.returncode == 0, result.stderr
    assert _ran(result) == "python3"
    assert _arguments(result) == [str(SKILL_SCRIPTS / SCRIPTS[launcher]), "alpha", "two words"]


@pytest.mark.parametrize("surface", [BIN, SKILL_SCRIPTS])
def test_both_entry_surfaces_choose_the_same_interpreter(sandbox: Sandbox, surface: Path) -> None:
    sandbox.python("python3", minor=9)
    sandbox.python("python3.12", minor=12)
    result = sandbox.run(surface / "research-project", "--help")
    assert result.returncode == 0, result.stderr
    assert _ran(result) == "python3.12"


def test_the_newest_qualifying_python_wins(sandbox: Sandbox) -> None:
    for name, minor in (("python3.11", 11), ("python3.12", 12), ("python3.13", 13), ("python3", 12)):
        sandbox.python(name, minor)
    assert _ran(sandbox.run(SKILL_SCRIPTS / "research-project")) == "python3.13"


@pytest.mark.parametrize("minor", [9, 10])
def test_a_python_below_the_floor_is_never_used(sandbox: Sandbox, minor: int) -> None:
    sandbox.python("python3", minor=minor)
    result = sandbox.run(SKILL_SCRIPTS / "research-project", "--help")
    assert result.returncode == 1
    assert _ran(result) == ""
    assert result.stdout == ""


def test_the_floor_itself_qualifies(sandbox: Sandbox) -> None:
    sandbox.python("python3", minor=11)
    assert _ran(sandbox.run(SKILL_SCRIPTS / "research-project")) == "python3"


def test_the_refusal_names_both_fixes(sandbox: Sandbox) -> None:
    sandbox.python("python3", minor=9)
    result = sandbox.run(SKILL_SCRIPTS / "research-project", "--help")
    assert "manage_workspace.py needs Python 3.11 or later and none was found" in result.stderr
    assert "Install Python 3.11 or later" in result.stderr
    assert "install uv" in result.stderr


def test_the_refusal_says_so_when_there_is_no_python_at_all(sandbox: Sandbox) -> None:
    result = sandbox.run(SKILL_SCRIPTS / "research-validate")
    assert result.returncode == 1
    assert "no python3 on PATH" in result.stderr


def test_a_removed_interpreter_is_skipped(sandbox: Sandbox) -> None:
    sandbox.broken("python3.13")
    sandbox.python("python3.12", minor=12)
    assert _ran(sandbox.run(SKILL_SCRIPTS / "research-project")) == "python3.12"


def test_an_interpreter_that_cannot_start_is_skipped(sandbox: Sandbox) -> None:
    sandbox.python("python3.13", minor=13, starts=False)
    sandbox.python("python3.12", minor=12)
    assert _ran(sandbox.run(SKILL_SCRIPTS / "research-project")) == "python3.12"


def test_uv_runs_the_script_when_no_python_qualifies(sandbox: Sandbox) -> None:
    sandbox.python("python3", minor=9)
    sandbox.uv()
    result = sandbox.run(SKILL_SCRIPTS / "research-dashboard", "--no-browser")
    assert result.returncode == 0, result.stderr
    assert _ran(result) == "uv"
    assert _arguments(result) == [
        "run", "--quiet", "--no-project", "--python", ">=3.11",
        str(SKILL_SCRIPTS / "workspace_dashboard.py"), "--no-browser",
    ]


def test_a_qualifying_python_is_preferred_to_uv(sandbox: Sandbox) -> None:
    sandbox.python("python3", minor=12)
    sandbox.uv()
    assert _ran(sandbox.run(SKILL_SCRIPTS / "research-project")) == "python3"


def test_a_failed_uv_run_is_not_replaced_by_an_old_python(sandbox: Sandbox) -> None:
    sandbox.python("python3", minor=9)
    sandbox.uv(exit_code=3)
    result = sandbox.run(SKILL_SCRIPTS / "research-project", "--help")
    assert result.returncode == 3
    assert "uv failed" in result.stderr
    assert "ran:python3" not in result.stdout


def test_a_symlinked_launcher_still_finds_the_selector(sandbox: Sandbox, tmp_path: Path) -> None:
    sandbox.python("python3", minor=12)
    link = tmp_path / "linked" / "research-project"
    link.parent.mkdir()
    link.symlink_to(SKILL_SCRIPTS / "research-project")
    assert _ran(sandbox.run(link)) == "python3"


def test_a_launcher_without_its_selector_says_so(sandbox: Sandbox, tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    shutil.copy(SKILL_SCRIPTS / "research-project", lonely / "research-project")
    (lonely / "manage_workspace.py").write_text("", encoding="utf-8")
    result = sandbox.run(lonely / "research-project")
    assert result.returncode == 1
    assert "python_select.sh is missing" in result.stderr


def _number(selector: str, name: str) -> int:
    found = re.search(rf"^RESEARCH_PYTHON_FLOOR_{name}=(\d+)$", selector, re.MULTILINE)
    assert found is not None, f"python_select.sh has no RESEARCH_PYTHON_FLOOR_{name}"
    return int(found.group(1))


def test_the_floor_matches_the_development_toolchain() -> None:
    selector = SELECTOR.read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^requires-python = ">=(\d+)\.(\d+)', pyproject, re.MULTILINE)
    assert declared is not None
    assert (_number(selector, "MAJOR"), _number(selector, "MINOR")) == (int(declared.group(1)), int(declared.group(2)))


def test_the_launchers_are_executable_and_the_selector_is_only_sourced() -> None:
    for name in SCRIPTS:
        assert os.access(SKILL_SCRIPTS / name, os.X_OK)
    assert SELECTOR.is_file()
    assert not os.access(SELECTOR, os.X_OK)
