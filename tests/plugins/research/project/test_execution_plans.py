"""Tests for durable task-specific execution plans."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from execution_plans import PlanError, build_plan, load_plan, publish_plan, validate_plan


@pytest.fixture
def plan_context(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    project_dir = tmp_path / "workspace" / "2026-09-16-001"
    target = tmp_path / "target"
    project_dir.mkdir(parents=True)
    target.mkdir()
    state = {
        "revision": 7,
        "working_directory": str(target),
        "tasks": [
            {
                "id": "T01",
                "name": "Write output",
                "verification": "test -f out.txt",
                "outputs": [{"root": "target", "path": "out.txt", "required": True}],
                "authorization": {"required": False, "scope": None},
                "effect": {"kind": "local_write"},
                "depends_on": [],
                "success_criteria": "out.txt exists",
            }
        ],
    }
    check = {
        "check_id": "output-exists",
        "kind": "command",
        "argv": ["test", "-f", "out.txt"],
        "criteria": None,
        "cwd": str(target),
        "expect_exit": 0,
        "subjects": ["out.txt"],
        "reads": ["out.txt"],
        "writes": [],
        "executor": "coordinator",
    }
    return project_dir, target, state, check


def _build(context: tuple[Path, Path, dict[str, Any], dict[str, Any]], **changes: Any) -> dict[str, Any]:
    project_dir, _target, state, check = context
    arguments: dict[str, Any] = {
        "project_dir": project_dir,
        "instruction": "Create out.txt with the requested result.",
        "checks": [check],
        "reads": [],
        "worker_kind": "codex",
        "worker_arguments": ["--model", "gpt-5"],
    }
    arguments.update(changes)
    return build_plan(state, "T01", **arguments)


def test_plan_build_publish_load_and_idempotence(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    project_dir, target, state, _check = plan_context
    plan = _build(plan_context)

    first = publish_plan(project_dir, state, plan)
    second = publish_plan(project_dir, state, plan)
    loaded = load_plan(project_dir, state, "T01", plan["plan_hash"])

    assert first == second
    assert loaded == plan
    assert plan["writes"] == [str(target / "out.txt")]
    assert plan["checks"][0]["subjects"] == [str(target / "out.txt")]
    assert len(plan["definition_hash"]) == 64
    assert len(plan["plan_hash"]) == 64


def test_worker_and_instruction_are_hash_bound(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    codex = _build(plan_context)
    claude = _build(plan_context, worker_kind="claude", worker_arguments=[])
    changed_instruction = _build(plan_context, instruction="Create out.txt with different requested content.")

    assert codex["definition_hash"] != claude["definition_hash"]
    assert codex["plan_hash"] != claude["plan_hash"]
    assert codex["definition_hash"] != changed_instruction["definition_hash"]


@pytest.mark.parametrize("instruction", ["", "pass", "noop", "no-op", "do nothing"])
def test_generic_or_empty_instruction_is_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]], instruction: str
) -> None:
    with pytest.raises(PlanError):
        _build(plan_context, instruction=instruction)


def test_missing_check_and_noop_check_are_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    with pytest.raises(PlanError, match="at least one check"):
        _build(plan_context, checks=[])

    noop = copy.deepcopy(plan_context[3])
    noop["argv"] = ["python3", "-c", "pass"]
    with pytest.raises(PlanError, match="no-op"):
        _build(plan_context, checks=[noop])

    noop["argv"] = ["true"]
    with pytest.raises(PlanError, match="no-op"):
        _build(plan_context, checks=[noop])


def test_required_output_must_be_covered(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    check = copy.deepcopy(plan_context[3])
    check["subjects"] = []

    with pytest.raises(PlanError, match="not covered"):
        _build(plan_context, checks=[check])


def test_criteria_check_is_canonical(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    check = copy.deepcopy(plan_context[3])
    check.update(kind="criteria", argv=None, criteria="The output explains the result.", expect_exit=None)

    plan = _build(plan_context, checks=[check], worker_kind="claude", worker_arguments=[])

    assert plan["checks"][0]["kind"] == "criteria"
    assert plan["checks"][0]["criteria"] == "The output explains the result."


def test_stale_or_mutated_plan_is_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    project_dir, _target, state, _check = plan_context
    plan = _build(plan_context)
    mutations = [
        ("source_revision", 6, "stale"),
        ("verification_text", "changed", "verification"),
        ("enumerable", False, "R-UNENUMERABLE"),
        ("definition_hash", "0" * 64, "definition hash"),
        ("plan_hash", "0" * 64, "plan hash"),
    ]

    for field, value, message in mutations:
        changed = copy.deepcopy(plan)
        changed[field] = value
        with pytest.raises(PlanError, match=message):
            validate_plan(changed, state, project_dir=project_dir)

    with pytest.raises(PlanError, match="protocol-owned"):
        validate_plan(plan, state, project_dir=Path(state["working_directory"]))


def test_invalid_worker_outputs_paths_and_checks_are_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    project_dir, target, state, check = plan_context
    with pytest.raises(PlanError, match="claude or codex"):
        _build(plan_context, worker_kind="generic")
    with pytest.raises(PlanError, match="worker arguments"):
        _build(plan_context, worker_arguments=[""])

    state["tasks"][0]["outputs"][0]["root"] = "workspace"
    with pytest.raises(PlanError, match="target root"):
        _build(plan_context)
    state["tasks"][0]["outputs"][0].update(root="target", path="../escape.txt")
    with pytest.raises(PlanError, match="escapes"):
        _build(plan_context, instruction="Create ../escape.txt")
    state["tasks"][0]["outputs"][0]["path"] = "directory/"
    (target / "directory").mkdir()
    with pytest.raises(PlanError, match="directory"):
        _build(plan_context, instruction="Create directory/")

    state["tasks"][0]["outputs"][0]["path"] = "out.txt"
    bad_check = copy.deepcopy(check)
    bad_check["check_id"] = "bad id"
    with pytest.raises(ValueError, match="invalid"):
        _build(plan_context, checks=[bad_check])

    state["working_directory"] = str(project_dir)
    state["tasks"][0]["outputs"][0]["path"] = "project.json"
    protocol_check = copy.deepcopy(check)
    protocol_check.update(cwd=str(project_dir), subjects=["project.json"], reads=["project.json"])
    with pytest.raises(PlanError, match="protocol-owned"):
        _build(plan_context, instruction="Create project.json", checks=[protocol_check])


def test_symlink_and_duplicate_paths_are_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    _project_dir, target, _state, check = plan_context
    real = target / "real.txt"
    real.write_text("source\n", encoding="utf-8")
    (target / "linked.txt").symlink_to(real)
    with pytest.raises(PlanError, match="symlink"):
        _build(plan_context, reads=["linked.txt"])

    duplicate = copy.deepcopy(check)
    duplicate["reads"] = ["out.txt", str(target / "out.txt")]
    with pytest.raises(PlanError, match="duplicate"):
        _build(plan_context, checks=[duplicate])


def test_conflicting_publication_and_load_failures_are_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    project_dir, _target, state, _check = plan_context
    plan = _build(plan_context)
    path = publish_plan(project_dir, state, plan)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(PlanError, match="conflicting"):
        publish_plan(project_dir, state, plan)
    with pytest.raises(PlanError, match="lowercase sha256"):
        load_plan(project_dir, state, "T01", "bad")
    path.unlink()
    with pytest.raises(PlanError, match="cannot load"):
        load_plan(project_dir, state, "T01", plan["plan_hash"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(PlanError, match="object"):
        load_plan(project_dir, state, "T01", plan["plan_hash"])


def test_unknown_task_and_missing_working_directory_are_rejected(
    plan_context: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    project_dir, _target, state, check = plan_context
    with pytest.raises(PlanError, match="unknown task"):
        build_plan(
            state,
            "T99",
            project_dir=project_dir,
            instruction="Create out.txt",
            checks=[check],
            reads=[],
            worker_kind="codex",
        )
    state["working_directory"] = str(project_dir / "missing")
    with pytest.raises(PlanError, match="working_directory"):
        _build(plan_context)
