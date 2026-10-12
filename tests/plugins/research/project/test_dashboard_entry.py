"""The dashboard reached the way a user reaches it: the launcher as a process, and the skill that starts it."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest
import workspace_dashboard as dash

from tests.conftest import REPO_ROOT, SKILL_SCRIPTS
from tests.plugins.research.project.test_dashboard_model import ID, write_project

BIN = REPO_ROOT / "plugins/research/bin"
SKILL = REPO_ROOT / "plugins/research/skills/dashboard/SKILL.md"
SURFACES = [pytest.param(BIN, id="bin"), pytest.param(SKILL_SCRIPTS, id="skills/project/scripts")]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    write_project(root, ID, title="Launched")
    return root


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)


@pytest.mark.parametrize("surface", SURFACES)
def test_the_launcher_serves_and_stops_on_ctrl_c(surface: Path, workspace: Path, tmp_path: Path) -> None:
    command = [str(surface / "research-dashboard"), str(workspace), "--no-browser", "--port", "0"]
    server = subprocess.Popen(
        [*command, "--claude-dir", str(tmp_path / "claude")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    watchdog = threading.Timer(60, server.kill)
    watchdog.start()
    try:
        line = (server.stdout.readline() if server.stdout else "").strip()
        match = re.fullmatch(r"Dashboard: (http://127\.0\.0\.1:\d+)", line)
        assert match, (line, server.poll())
        with urllib.request.urlopen(match[1] + "/api/projects", timeout=30) as response:
            projects = json.loads(response.read())["projects"]
        assert [project["id"] for project in projects] == [ID]
        server.send_signal(signal.SIGINT)
        assert server.wait(timeout=30) == 0
    finally:
        watchdog.cancel()
        if server.poll() is None:
            server.kill()
            server.wait()
        for stream in (server.stdout, server.stderr):
            if stream:
                stream.close()


@pytest.mark.parametrize("surface", SURFACES)
def test_the_exit_code_of_the_dashboard_reaches_the_caller(surface: Path, tmp_path: Path) -> None:
    result = run([str(surface / "research-dashboard"), str(tmp_path / "missing"), "--no-browser"])
    assert result.returncode == 2
    assert "is not a directory" in result.stderr
    assert result.stdout == ""


def test_the_skill_names_the_launcher_it_starts() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "`research-dashboard`" in text
    relative = re.search(r"`(\.\./project/scripts/research-dashboard)`", text)
    assert relative, "the skill must name the launcher beside it for hosts without PATH entries"
    assert (SKILL.parent / relative[1]).resolve() == (SKILL_SCRIPTS / "research-dashboard").resolve()


def test_every_flag_the_skill_documents_is_one_the_command_accepts() -> None:
    text = SKILL.read_text(encoding="utf-8")
    start = re.search(r"`research-dashboard \[ROOT\] ([^`]+)`", text)
    assert start, "the skill must show the background command line"
    arguments = start[1].split()
    parsed = dash._parser().parse_args(arguments)
    assert parsed.no_browser is True and parsed.port == 0
    # Only flags of this command: `claude --resume` in the same file belongs to another program.
    documented = {
        flag for span in re.findall(r"`research-dashboard([^`]*)`", text) for flag in re.findall(r"--[a-z-]+", span)
    }
    accepted = {option for action in dash._parser()._actions for option in action.option_strings}
    assert documented <= accepted, sorted(documented - accepted)


def test_the_skill_names_the_environment_variable_the_command_reads() -> None:
    assert "$RESEARCH_WORKSPACE" in SKILL.read_text(encoding="utf-8")
