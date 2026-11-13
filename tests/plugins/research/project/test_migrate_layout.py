"""`migrate --layout WORKSPACE_DIR [--apply]`: stateless, idempotent, never commits."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_layout
from workspace_doctor import attributes_line, ignore_lines, run_doctor
from workspace_lib import WorkspaceError

from tests.plugins.research.project.test_doctor import GIT_ENV, MIB, PROJECT, fake_lfs, git, write

STEP_IDS = ["S1", "S2", "S3", "S4", "S5", "S6"]
VIEWS = ("INDEX.md", "MEMORY.md", "POSTMORTEMS.md", "RETRO.md")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", f"{fake_lfs(tmp_path / 'lfs-bin')}{os.pathsep}{os.environ['PATH']}")


def manage(*arguments: object) -> int:
    with patch.object(sys, "argv", ["research-project", *[str(item) for item in arguments]]):
        return manage_workspace.main()


def make_repo(top: Path, *, prefix: str = "workspace") -> Path:
    """A scratch repo whose committed workspace tracks all four views, as the live one does today."""
    top.mkdir(parents=True)
    git(top, "init", "-q")
    workspace = top / prefix if prefix else top
    write(workspace / PROJECT / "project.json", 20)
    for view in VIEWS:
        (workspace / view).write_text(f"# {view}\n", encoding="utf-8")
    (top / ".gitignore").write_text("node_modules/\n.venv", encoding="utf-8")  # no trailing newline
    git(top, "add", "-A")
    git(top, "commit", "-qm", "before")
    return workspace


def snapshot(top: Path) -> dict[str, object]:
    files = {
        path.relative_to(top).as_posix(): path.read_bytes()
        for path in sorted(top.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(top).parts
    }
    return {
        "files": files,
        "index": git(top, "ls-files", "-s"),
        "status": git(top, "status", "--porcelain"),
        "config": sorted(line for line in git(top, "config", "--local", "--list").splitlines() if "filter.lfs" in line),
    }


def commits(top: Path) -> int:
    return len(git(top, "log", "--oneline").splitlines())


def test_dry_run_prints_the_steps_and_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    before = snapshot(top)
    assert manage("migrate", "--layout", workspace) == 0
    out = capsys.readouterr().out
    for step in STEP_IDS:
        assert f"{step} would apply" in out
    assert "S7 would run doctor" in out
    assert snapshot(top) == before


def test_apply_builds_the_layout_stages_it_and_never_commits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    assert manage("migrate", "--layout", workspace, "--apply") == 0
    out = capsys.readouterr().out
    assert all(f"{step} applied" in out for step in STEP_IDS) and "S7 doctor: 0 finding(s)" in out
    assert commits(top) == 1
    assert json.loads((workspace / ".workspace-root").read_text(encoding="utf-8")) == {"layout": 1}
    ignore = (top / ".gitignore").read_text(encoding="utf-8")
    assert ignore.startswith("node_modules/\n.venv\n"), "existing content is preserved"
    assert ignore.splitlines()[2:] == ignore_lines("workspace")
    assert (top / ".gitattributes").read_text(encoding="utf-8") == attributes_line("workspace") + "\n"
    staged = git(top, "diff", "--cached", "--name-status").splitlines()
    assert sorted(staged) == sorted(
        [
            "A\t.gitattributes",
            "M\t.gitignore",
            "A\tworkspace/.workspace-root",
            *[f"D\tworkspace/{view}" for view in VIEWS],
        ]
    )
    assert all((workspace / view).is_file() for view in VIEWS), "views stay on disk"
    assert run_doctor(workspace) == []


def test_a_second_apply_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    assert manage("migrate", "--layout", workspace, "--apply") == 0
    first = snapshot(top)
    capsys.readouterr()
    assert manage("migrate", "--layout", workspace, "--apply") == 0
    out = capsys.readouterr().out
    assert snapshot(top) == first
    assert all(f"{step} skip" in out for step in STEP_IDS)


@pytest.mark.parametrize("interrupted_after", STEP_IDS)
def test_an_interruption_after_any_step_resumes_to_the_same_result(tmp_path: Path, interrupted_after: str) -> None:
    clean_top = tmp_path / "clean"
    clean = make_repo(clean_top)
    assert manage("migrate", "--layout", clean, "--apply") == 0

    top = tmp_path / "interrupted"
    workspace = make_repo(top)

    def crash(step_id: str) -> None:
        if step_id == interrupted_after:
            raise KeyboardInterrupt

    with patch.object(workspace_layout, "_after_step", crash), pytest.raises(KeyboardInterrupt):
        manage("migrate", "--layout", workspace, "--apply")
    assert commits(top) == 1
    assert manage("migrate", "--layout", workspace, "--apply") == 0

    assert snapshot(top) == snapshot(clean_top)


def test_a_change_after_staging_is_restaged_on_the_next_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    manage("migrate", "--layout", workspace, "--apply")
    with (top / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("extra\n")
    capsys.readouterr()
    assert manage("migrate", "--layout", workspace, "--apply") == 0
    out = capsys.readouterr().out
    assert "S6 applied" in out and "S3 skip" in out
    assert git(top, "diff", "--name-only") == ""


def test_a_workspace_at_the_repository_root_uses_unprefixed_lines(tmp_path: Path) -> None:
    top = tmp_path / "flat"
    workspace = make_repo(top, prefix="")
    assert manage("migrate", "--layout", workspace, "--apply") == 0
    assert "/INDEX.md" in (top / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert (top / ".workspace-root").is_file()


def test_missing_git_lfs_fails_the_apply_with_a_workspace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    monkeypatch.setenv("PATH", f"{fake_lfs(tmp_path / 'broken', works=False)}{os.pathsep}{os.environ['PATH']}")
    assert manage("migrate", "--layout", workspace, "--apply") == 1
    assert "git lfs install --local" in capsys.readouterr().err
    assert not (workspace / ".workspace-root").exists()


def test_remaining_doctor_errors_fail_the_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    write(workspace / PROJECT / "big.bin", 5 * MIB)
    assert manage("migrate", "--layout", workspace, "--apply") == 1
    assert f"error R1 {PROJECT}/big.bin" in capsys.readouterr().out


def test_refuses_a_missing_directory_and_a_non_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert manage("migrate", "--layout", tmp_path / "missing") == 1
    assert "does not exist" in capsys.readouterr().err
    plain = tmp_path / "plain"
    plain.mkdir()
    assert manage("migrate", "--layout", plain, "--apply") == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_an_unreadable_gitignore_is_a_workspace_error(tmp_path: Path) -> None:
    top = tmp_path / "repo"
    workspace = make_repo(top)
    (top / ".gitignore").write_bytes(b"\xff\xfe")
    with pytest.raises(WorkspaceError, match="cannot read"):
        workspace_layout.migrate_layout(workspace, apply=True)


def test_without_the_flag_the_positional_is_still_a_project_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "ws" / "project"
    project.mkdir(parents=True)
    state = {
        "schema_version": 2,
        "project": "project",
        "title": "T",
        "status": "PLANNING",
        "created": "2026-01-01",
        "updated": "2026-08-28T10:00:00+02:00",
        "working_directory": str(tmp_path),
        "current_task": None,
        "review_cycle": 0,
        "tasks": [],
    }
    (project / "project.json").write_text(json.dumps(state), encoding="utf-8")
    assert manage("migrate", project) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 3
    # A workspace directory given without --layout is the old command on a non-project: it fails.
    assert manage("migrate", tmp_path / "ws") == 1
    assert not (tmp_path / "ws" / ".workspace-root").exists()
