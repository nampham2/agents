"""Marker-based root detection, `render_views` and `ensure_views` (workspace records layout)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

import pytest
import workspace_lib
from workspace_lib import (
    WorkspaceConflict,
    WorkspaceError,
    allocate_project,
    ensure_views,
    find_workspace_roots,
    memory_findings,
    rebuild_index,
    render_views,
    validate_project,
)

TOPIC = (
    "---\nname: uv-toolchain\ndescription: d\nkind: method\nscope: s\nsources: \nupdated: 2026-01-01\n---\n\nbody\n"
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    allocate_project(root, title="One", working_directory=target)
    # Allocation builds views and a memory directory; start from the bare fresh-clone state.
    for view in ("INDEX.md", "MEMORY.md", "POSTMORTEMS.md"):
        (root / view).unlink(missing_ok=True)
    memory = root / "memory"
    if memory.is_dir():
        memory.rmdir()
    return root


def test_marker_alone_makes_a_root_on_a_fresh_clone(workspace: Path, tmp_path: Path) -> None:
    (workspace / "INDEX.md").unlink(missing_ok=True)
    assert find_workspace_roots([tmp_path]) == []
    (workspace / ".workspace-root").write_text("anything, ignored", encoding="utf-8")
    assert find_workspace_roots([tmp_path]) == [workspace]


def test_marker_without_project_directory_is_not_a_root(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / ".workspace-root").write_text("{}", encoding="utf-8")
    assert find_workspace_roots([tmp_path]) == []


def test_rebuild_index_writes_retro_only_when_retro_exists(workspace: Path) -> None:
    rebuild_index(workspace)
    assert not (workspace / "RETRO.md").exists()
    (workspace / "retro").mkdir()
    rebuild_index(workspace)
    assert (workspace / "RETRO.md").read_text(encoding="utf-8").startswith("# Retro ledger")


def test_render_views_selects_views_by_source_directory(workspace: Path) -> None:
    assert set(render_views(workspace)) == {"INDEX.md"}
    (workspace / "memory").mkdir()
    (workspace / "retro").mkdir()
    assert set(render_views(workspace)) == {"INDEX.md", "MEMORY.md", "POSTMORTEMS.md", "RETRO.md"}


def test_render_views_is_deterministic_and_matches_rebuild(workspace: Path) -> None:
    (workspace / "memory").mkdir()
    (workspace / "memory" / "uv-toolchain.md").write_text(TOPIC, encoding="utf-8")
    (workspace / "retro").mkdir()
    first = render_views(workspace)
    assert first == render_views(workspace)
    rebuild_index(workspace)
    for name, text in first.items():
        assert (workspace / name).read_text(encoding="utf-8") == text


def test_ensure_views_repairs_missing_then_is_a_no_op(workspace: Path) -> None:
    result = ensure_views(workspace)
    assert result.ok and result.written == (workspace / "INDEX.md",)
    assert ensure_views(workspace) == workspace_lib.EnsureResult((), True)


def test_ensure_views_repairs_a_stale_view_and_names_only_changed_files(workspace: Path) -> None:
    (workspace / "memory").mkdir()
    rebuild_index(workspace)
    (workspace / "INDEX.md").write_text("stale\n", encoding="utf-8")
    result = ensure_views(workspace)
    assert result.ok and result.written == (workspace / "INDEX.md",)


def test_ensure_views_leaves_a_view_whose_source_disappeared(workspace: Path) -> None:
    (workspace / "retro").mkdir()
    rebuild_index(workspace)
    (workspace / "retro").rmdir()
    assert ensure_views(workspace) == workspace_lib.EnsureResult((), True)
    assert (workspace / "RETRO.md").exists()


def test_ensure_views_reports_not_ok_on_a_lock_failure(workspace: Path) -> None:
    with mock.patch.object(workspace_lib, "rebuild_index", side_effect=WorkspaceConflict("busy")):
        assert ensure_views(workspace) == workspace_lib.EnsureResult((), False)


def test_ensure_views_reports_partial_writes_when_a_later_write_fails(workspace: Path) -> None:
    (workspace / "memory").mkdir()
    real = workspace_lib.atomic_write_text

    def flaky(path: Path, content: str) -> None:
        if path.name == "MEMORY.md":
            raise OSError("disk full")
        real(path, content)

    with mock.patch.object(workspace_lib, "atomic_write_text", flaky):
        result = ensure_views(workspace)
    assert not result.ok
    assert result.written == (workspace / "INDEX.md",)


def test_ensure_views_on_a_read_only_root_is_not_ok(workspace: Path) -> None:
    workspace.chmod(0o555)
    try:
        if os.access(workspace, os.W_OK):
            pytest.skip("root ignores permissions")
        result = ensure_views(workspace)
    finally:
        workspace.chmod(0o755)
    assert not result.ok and result.written == ()


def test_ensure_views_survives_unrenderable_state(workspace: Path) -> None:
    with mock.patch.object(workspace_lib, "render_views", side_effect=WorkspaceError("bad")):
        assert ensure_views(workspace) == workspace_lib.EnsureResult((), False)


def test_ensure_views_sweeps_only_old_view_temporaries(workspace: Path) -> None:
    rebuild_index(workspace)
    old = workspace / ".INDEX.md.abc.tmp"
    fresh = workspace / ".INDEX.md.def.tmp"
    other = workspace / ".unrelated.abc.tmp"
    for path in (old, fresh, other):
        path.write_text("x", encoding="utf-8")
    ancient = time.time() - 3600
    os.utime(old, (ancient, ancient))
    os.utime(other, (ancient, ancient))
    assert ensure_views(workspace).ok
    assert not old.exists() and fresh.exists() and other.exists()


def test_sweep_ignores_unlink_and_stat_failures(workspace: Path) -> None:
    rebuild_index(workspace)
    old = workspace / ".INDEX.md.abc.tmp"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (1, 1))
    with mock.patch.object(Path, "unlink", side_effect=OSError("no")):
        assert ensure_views(workspace).ok
    with mock.patch.object(Path, "glob", side_effect=OSError("no")):
        assert ensure_views(workspace).ok


def _stale(workspace: Path) -> Path:
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "uv-toolchain.md").write_text(TOPIC, encoding="utf-8")
    (workspace / "INDEX.md").write_text("stale\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("stale\n", encoding="utf-8")
    return next(child for child in workspace.iterdir() if child.name[:2] == "20")


def test_stale_views_are_errors_without_the_marker(workspace: Path) -> None:
    project = _stale(workspace)
    assert any("derived index is stale" in e for e in validate_project(project, check_index=True).errors)
    assert any("is stale" in e for e in memory_findings(workspace, check_index=True).errors)


def test_stale_views_are_not_errors_with_the_marker(workspace: Path) -> None:
    project = _stale(workspace)
    (workspace / ".workspace-root").write_text('{"layout":1}', encoding="utf-8")
    assert [e for e in validate_project(project, check_index=True).errors if "stale" in e] == []
    assert not any("stale" in e for e in memory_findings(workspace, check_index=True).errors)
