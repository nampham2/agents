"""Validation sees arbitrary JSON: substituting any value at any leaf must yield findings, not exceptions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from workspace_lib import allocate_project, validate_project, validate_v3_state, validate_v4_state

SUBSTITUTES: list[object] = [None, [], {}, 0, True, "x", 1.5, [[]], {"a": []}]


def valid_state(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    root = tmp_path / "workspace"
    target = tmp_path / "target"
    target.mkdir()
    project = allocate_project(root, title="Malformed", working_directory=target, create_root=True)
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    state["worktrees"] = [
        {
            "repository": str(tmp_path / "repo"), "branch": "b", "base_commit": "abc", "path": str(tmp_path / "wt"),
            "kind": "created", "role": "additional", "status": "kept", "recorded_at": "2026-10-02T10:00:00+00:00",
            "confirmation": {"source": "user", "response": "yes"},
            "closure": {
                "decision": "accept_dirty", "dirty": [" M a.py"], "observed_at": "2026-10-02T10:00:00+00:00",
                "source": "user", "response": "ok",
            },
        }
    ]
    state["tasks"] = [
        {
            "id": "T01", "name": "n", "success_criteria": "s", "verification": "v", "status": "TODO",
            "depends_on": [], "outputs": [{"root": "target", "path": "out.txt", "required": True}],
            "evidence": [], "receipts": [], "block_reason": None, "skip_reason": None,
            "effect": {"kind": "none", "description": None},
            "authorization": {"required": False, "status": "not_required", "scope": None, "source": None,
                              "authorized_at": None},
        }
    ]
    return state, project


def leaf_paths(value: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    """Every path to a scalar or to an empty container, plus the containers themselves, as substitution sites."""
    paths: list[tuple[object, ...]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.append((*prefix, key))
            paths.extend(leaf_paths(child, (*prefix, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.append((*prefix, index))
            paths.extend(leaf_paths(child, (*prefix, index)))
    return paths


def substituted(state: dict[str, Any], path: tuple[object, ...], value: object) -> dict[str, Any]:
    candidate = copy.deepcopy(state)
    holder: Any = candidate
    for step in path[:-1]:
        holder = holder[step]
    holder[path[-1]] = copy.deepcopy(value)
    return candidate


def test_the_base_state_is_valid(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    assert validate_v4_state(state, project, check_files=False).errors == []


def test_worktree_leaves_never_raise(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    sites = [path for path in leaf_paths(state) if path[0] == "worktrees"]
    assert len(sites) > 15  # an empty site list would pass for the wrong reason
    for path in sites:
        for value in SUBSTITUTES:
            candidate = substituted(state, path, value)
            for close in (False, True):
                validate_v4_state(candidate, project, close=close, check_files=False)


def test_every_leaf_of_a_v4_state_never_raises(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    sites = leaf_paths(state)
    assert len(sites) > 60
    for path in sites:
        for value in SUBSTITUTES:
            validate_v4_state(substituted(state, path, value), project, check_files=False)


def test_the_original_f5_crash_is_a_finding(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    state["worktrees"][0]["kind"] = []
    errors = validate_v4_state(state, project, check_files=False).errors
    assert any("kind must be one of" in error for error in errors)


def test_v3_worktree_leaves_never_raise(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    state["schema_version"] = 3
    del state["execution"]
    for path in (p for p in leaf_paths(state) if p[0] == "worktrees"):
        for value in SUBSTITUTES:
            validate_v3_state(substituted(state, path, value), project, check_files=False)


def test_validate_project_reports_instead_of_raising(tmp_path: Path) -> None:
    state, project = valid_state(tmp_path)
    state["worktrees"][0]["kind"] = []
    (project / "project.json").write_text(json.dumps(state), encoding="utf-8")
    report = validate_project(project)
    assert any("kind must be one of" in error for error in report.errors)
