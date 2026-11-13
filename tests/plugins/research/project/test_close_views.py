"""Read paths repair untracked views: `context`, `workflow resume` and `rebuild-index`."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import manage_workspace
import pytest
from workspace_lib import allocate_project, ensure_project_views


def manage(*arguments: object) -> int:
    with patch.object(sys, "argv", ["research-project", *[str(item) for item in arguments]]):
        return manage_workspace.main()


@pytest.fixture
def cloned(tmp_path: Path) -> tuple[Path, Path]:
    """A fresh-clone workspace: the marker and a project, no derived views."""
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    project = allocate_project(root, title="One", working_directory=target)
    for view in ("INDEX.md", "MEMORY.md", "POSTMORTEMS.md"):
        (root / view).unlink(missing_ok=True)
    (root / ".workspace-root").write_text('{"layout":1}', encoding="utf-8")
    return root, project


def test_context_builds_the_missing_views(cloned: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, project = cloned
    assert manage("context", project) == 0
    capsys.readouterr()
    assert (root / "INDEX.md").is_file()


def test_workflow_resume_builds_the_missing_views(cloned: tuple[Path, Path], tmp_path: Path) -> None:
    root, project = cloned
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    manage("workflow", project, "resume", request)
    assert (root / "INDEX.md").is_file()


def test_other_workflow_actions_do_not_touch_views(cloned: tuple[Path, Path], tmp_path: Path) -> None:
    root, project = cloned
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    manage("workflow", project, "packet", request)
    assert not (root / "INDEX.md").exists()


def test_rebuild_index_writes_every_view_including_retro(cloned: tuple[Path, Path]) -> None:
    root, _ = cloned
    (root / "retro").mkdir()
    assert manage("rebuild-index", root) == 0
    assert (root / "INDEX.md").is_file() and (root / "RETRO.md").is_file()


def test_a_project_outside_a_workspace_root_is_left_alone(tmp_path: Path) -> None:
    orphan = tmp_path / "loose" / "2026-11-13-001"
    orphan.mkdir(parents=True)
    assert ensure_project_views(orphan) is None
    assert not (tmp_path / "loose" / "INDEX.md").exists()


def test_a_failed_repair_does_not_fail_context(cloned: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, project = cloned
    root.chmod(0o555)
    try:
        assert manage("context", project) == 0
    finally:
        root.chmod(0o755)
    capsys.readouterr()
