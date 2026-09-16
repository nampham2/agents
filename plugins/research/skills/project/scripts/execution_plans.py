"""Immutable task-specific plans for the parallel execution protocol."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast

from execution_serialization import canonical_json, validate_id
from execution_store import publish_if_absent

PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "source_revision",
        "cwd",
        "verification_text",
        "instruction",
        "checks",
        "reads",
        "writes",
        "outputs",
        "enumerable",
        "worker",
        "definition_hash",
        "plan_hash",
    }
)
CHECK_FIELDS = frozenset(
    {
        "check_id",
        "kind",
        "argv",
        "criteria",
        "cwd",
        "expect_exit",
        "subjects",
        "reads",
        "writes",
        "executor",
    }
)
WORKER_FIELDS = frozenset({"kind", "arguments"})
WORKER_KINDS = frozenset({"claude", "codex"})
CHECK_KINDS = frozenset({"command", "shell", "criteria"})
EXECUTORS = frozenset({"worker", "coordinator"})
HASH_LENGTH = 64


class PlanError(ValueError):
    """Raised when an execution plan is incomplete, unsafe, or inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _task_by_id(state: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    for task in state.get("tasks", []):
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    raise PlanError(f"unknown task: {task_id}")


def _resolve_file_path(cwd: Path, value: object, field: str, *, require_relative: bool = False) -> str:
    _require(_non_empty(value), f"{field} must be a non-empty path")
    raw = str(value)
    path = Path(raw).expanduser()
    _require(not (require_relative and path.is_absolute()), f"{field} must be relative to the target")
    candidate = path if path.is_absolute() else cwd / path
    unresolved = candidate.absolute()
    for parent in (unresolved, *unresolved.parents):
        if parent.exists():
            _require(not parent.is_symlink(), f"{field} crosses a symlink: {raw}")
        if parent == cwd.parent:
            break
    resolved = candidate.resolve()
    if require_relative:
        try:
            resolved.relative_to(cwd)
        except ValueError:
            _require(False, f"{field} escapes the target working directory")
    _require(not (resolved.exists() and resolved.is_dir()), f"{field} names a directory")
    _require(not raw.endswith(os.sep), f"{field} names a directory")
    return str(resolved)


def _normalise_paths(cwd: Path, values: object, field: str) -> List[str]:
    _require(isinstance(values, list), f"{field} must be an array")
    paths = [_resolve_file_path(cwd, value, field) for value in cast(List[object], values)]
    _require(len(paths) == len(set(paths)), f"{field} contains duplicate paths")
    return paths


def _resolve_directory(default_cwd: Path, value: object, field: str) -> Path:
    _require(_non_empty(value), f"{field} must be a non-empty path")
    path = Path(str(value)).expanduser()
    resolved = (path if path.is_absolute() else default_cwd / path).resolve()
    _require(resolved.is_dir(), f"{field} must be an existing directory")
    return resolved


def _is_noop_argv(argv: Sequence[str]) -> bool:
    executable = Path(argv[0]).name.lower()
    if executable in {"true", ":"}:
        return True
    return (
        len(argv) >= 3
        and executable.startswith("python")
        and argv[1] == "-c"
        and argv[2].strip().rstrip(";") in {"pass", "None", "0"}
    )


def _normalise_check(raw: object, default_cwd: Path) -> Dict[str, Any]:
    _require(isinstance(raw, dict), "each check must be an object")
    check = dict(cast(Dict[str, Any], raw))
    _require(set(check) == CHECK_FIELDS, "check fields do not match the plan schema")
    check_id = validate_id(check.get("check_id"), "check_id")
    kind = check.get("kind")
    _require(kind in CHECK_KINDS, f"check {check_id}: unknown kind")
    cwd = _resolve_directory(default_cwd, check.get("cwd"), f"check {check_id} cwd")
    executor = check.get("executor")
    _require(executor in EXECUTORS, f"check {check_id}: invalid executor")
    argv = check.get("argv")
    criteria = check.get("criteria")
    expect_exit = check.get("expect_exit")
    if kind == "criteria":
        _require(argv is None, f"check {check_id}: criteria checks cannot have argv")
        _require(_non_empty(criteria), f"check {check_id}: criteria must be non-empty")
        _require(expect_exit is None, f"check {check_id}: criteria checks cannot expect an exit")
    else:
        _require(isinstance(argv, list) and bool(argv), f"check {check_id}: argv must be non-empty")
        argv_list = cast(List[str], argv)
        _require(all(_non_empty(arg) for arg in argv_list), f"check {check_id}: argv contains an empty value")
        _require(criteria is None, f"check {check_id}: command checks cannot have criteria")
        _require(
            isinstance(expect_exit, int) and not isinstance(expect_exit, bool) and 0 <= expect_exit <= 255,
            f"check {check_id}: expect_exit must be between 0 and 255",
        )
        _require(not _is_noop_argv(argv_list), f"check {check_id}: generic no-op commands are forbidden")
    check["check_id"] = check_id
    check["cwd"] = str(cwd)
    check["subjects"] = _normalise_paths(cwd, check.get("subjects"), f"check {check_id} subjects")
    check["reads"] = _normalise_paths(cwd, check.get("reads"), f"check {check_id} reads")
    check["writes"] = _normalise_paths(cwd, check.get("writes"), f"check {check_id} writes")
    return check


def _definition_payload(
    task: Dict[str, Any],
    cwd: str,
    instruction: str,
    checks: List[Dict[str, Any]],
    reads: List[str],
    writes: List[str],
    worker: Dict[str, Any],
) -> Dict[str, Any]:
    authorization = task.get("authorization", {})
    effect = task.get("effect", {})
    return {
        "authorization": {"required": authorization.get("required"), "scope": authorization.get("scope")},
        "checks": checks,
        "cwd": cwd,
        "depends_on": task.get("depends_on"),
        "effect": {"kind": effect.get("kind")},
        "instruction": instruction,
        "outputs": task.get("outputs"),
        "reads": reads,
        "success_criteria": task.get("success_criteria"),
        "task_id": task.get("id"),
        "verification_text": task.get("verification"),
        "worker": worker,
        "writes": writes,
    }


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _hash_without(plan: Dict[str, Any], field: str) -> str:
    return _sha256({key: value for key, value in plan.items() if key != field})


def build_plan(
    state: Dict[str, Any],
    task_id: str,
    *,
    project_dir: Path,
    instruction: str,
    checks: Sequence[Dict[str, Any]],
    reads: Sequence[str],
    worker_kind: str,
    worker_arguments: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build and validate one immutable execution plan from a canonical task definition."""
    task = _task_by_id(state, validate_id(task_id, "task_id"))
    cwd = Path(str(state.get("working_directory", ""))).expanduser().resolve()
    _require(cwd.is_dir(), "working_directory must be an existing directory")
    _require(_non_empty(instruction), "instruction must be non-empty")
    _require(instruction.strip().lower() not in {"pass", "noop", "no-op", "do nothing"}, "generic no-op instruction")
    _require(worker_kind in WORKER_KINDS, "worker kind must be claude or codex")
    arguments = list(worker_arguments or [])
    _require(all(_non_empty(argument) for argument in arguments), "worker arguments must be non-empty strings")
    worker = {"kind": worker_kind, "arguments": arguments}
    outputs = copy.deepcopy(task.get("outputs"))
    _require(isinstance(outputs, list) and bool(outputs), "task must declare at least one output")
    outputs = cast(List[Dict[str, Any]], outputs)
    writes: List[str] = []
    for index, output in enumerate(outputs):
        _require(isinstance(output, dict), f"output {index} must be an object")
        _require(output.get("root") == "target", f"output {index} must use the target root")
        output_path = _resolve_file_path(cwd, output.get("path"), f"output {index}", require_relative=True)
        _require(output_path not in writes, "task outputs resolve to duplicate paths")
        _require(str(output.get("path")) in instruction, f"instruction must name output {output.get('path')}")
        writes.append(output_path)
    resolved_project = project_dir.resolve()
    for path in writes:
        try:
            Path(path).relative_to(resolved_project)
        except ValueError:
            continue
        _require(False, "worker output claims a protocol-owned project path")
    normalised_checks = [_normalise_check(check, cwd) for check in checks]
    _require(bool(normalised_checks), "a plan must contain at least one check")
    check_ids = [check["check_id"] for check in normalised_checks]
    _require(len(check_ids) == len(set(check_ids)), "check ids must be unique")
    covered = {subject for check in normalised_checks for subject in check["subjects"]}
    for index, output in enumerate(outputs):
        write = writes[index]
        if output.get("required") is True:
            _require(write in covered, f"required output is not covered by a check: {output.get('path')}")
    normalised_reads = _normalise_paths(cwd, list(reads), "reads")
    revision = state.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0, "invalid source revision")
    plan: Dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "source_revision": revision,
        "cwd": str(cwd),
        "verification_text": task.get("verification"),
        "instruction": instruction,
        "checks": normalised_checks,
        "reads": normalised_reads,
        "writes": writes,
        "outputs": outputs,
        "enumerable": True,
        "worker": worker,
    }
    plan["definition_hash"] = _sha256(
        _definition_payload(task, str(cwd), instruction, normalised_checks, normalised_reads, writes, worker)
    )
    plan["plan_hash"] = _hash_without(plan, "plan_hash")
    validate_plan(plan, state, project_dir=project_dir)
    return plan


def validate_plan(plan: Dict[str, Any], state: Dict[str, Any], *, project_dir: Path) -> None:
    """Validate a plan against the exact canonical project revision it interprets."""
    _require(isinstance(plan, dict), "plan must be an object")
    _require(set(plan) == PLAN_FIELDS, "plan fields do not match the schema")
    _require(plan.get("schema_version") == 1, "plan schema_version must be 1")
    task_id = validate_id(plan.get("task_id"), "task_id")
    task = _task_by_id(state, task_id)
    _require(plan.get("source_revision") == state.get("revision"), "plan source revision is stale")
    cwd = Path(str(plan.get("cwd", "")))
    _require(cwd.is_absolute() and cwd.is_dir(), "plan cwd must be an existing absolute directory")
    _require(str(cwd) == str(Path(str(state.get("working_directory", ""))).expanduser().resolve()), "plan cwd changed")
    _require(plan.get("verification_text") == task.get("verification"), "verification text changed")
    instruction = plan.get("instruction")
    _require(_non_empty(instruction), "instruction must be non-empty")
    worker = plan.get("worker")
    _require(isinstance(worker, dict) and set(worker) == WORKER_FIELDS, "worker fields are invalid")
    worker = cast(Dict[str, Any], worker)
    _require(worker.get("kind") in WORKER_KINDS, "worker kind must be claude or codex")
    arguments = worker.get("arguments")
    _require(isinstance(arguments, list) and all(_non_empty(arg) for arg in arguments), "worker arguments are invalid")
    outputs = plan.get("outputs")
    _require(outputs == task.get("outputs"), "plan output contract changed")
    outputs = cast(List[Dict[str, Any]], outputs)
    expected_writes = [
        _resolve_file_path(cwd, output.get("path"), f"output {index}", require_relative=True)
        for index, output in enumerate(outputs)
    ]
    writes = _normalise_paths(cwd, plan.get("writes"), "writes")
    _require(writes == expected_writes, "writes must exactly equal resolved task outputs")
    for write in writes:
        try:
            Path(write).relative_to(project_dir.resolve())
        except ValueError:
            continue
        _require(False, "worker output claims a protocol-owned project path")
    reads = _normalise_paths(cwd, plan.get("reads"), "reads")
    checks_raw = plan.get("checks")
    _require(isinstance(checks_raw, list) and bool(checks_raw), "checks must be a non-empty array")
    checks_raw = cast(List[Dict[str, Any]], checks_raw)
    checks = [_normalise_check(check, cwd) for check in checks_raw]
    _require(checks == checks_raw, "checks are not canonically resolved")
    check_ids = [check["check_id"] for check in checks]
    _require(len(check_ids) == len(set(check_ids)), "check ids must be unique")
    _require(plan.get("enumerable") is True, "R-UNENUMERABLE: plan is not enumerable")
    covered = {subject for check in checks for subject in check["subjects"]}
    for index, output in enumerate(outputs):
        write = writes[index]
        if output.get("required") is True:
            _require(write in covered, f"R-CHECK-UNCOVERED: {output.get('path')}")
    expected_definition = _sha256(_definition_payload(task, str(cwd), str(instruction), checks, reads, writes, worker))
    _require(plan.get("definition_hash") == expected_definition, "definition hash mismatch")
    plan_hash = plan.get("plan_hash")
    _require(
        isinstance(plan_hash, str)
        and len(plan_hash) == HASH_LENGTH
        and all(c in "0123456789abcdef" for c in plan_hash),
        "plan hash must be lowercase sha256",
    )
    _require(plan_hash == _hash_without(plan, "plan_hash"), "plan hash mismatch")


def publish_plan(project_dir: Path, state: Dict[str, Any], plan: Dict[str, Any]) -> Path:
    """Validate and immutably publish a plan, returning its durable path."""
    validate_plan(plan, state, project_dir=project_dir)
    plans_dir = project_dir / "execution" / "plans"
    final = plans_dir / f"{plan['task_id']}.{plan['plan_hash']}.json"
    tmp_dir = project_dir / "execution" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    outcome = publish_if_absent(final, canonical_json(plan), tmp_dir)
    _require(outcome != "conflict", f"conflicting immutable plan publication: {final.name}")
    return final


def load_plan(project_dir: Path, state: Dict[str, Any], task_id: str, plan_hash: str) -> Dict[str, Any]:
    """Load and validate an immutable plan by task and content hash."""
    validate_id(task_id, "task_id")
    _require(
        len(plan_hash) == HASH_LENGTH and all(c in "0123456789abcdef" for c in plan_hash),
        "plan hash must be lowercase sha256",
    )
    path = project_dir / "execution" / "plans" / f"{task_id}.{plan_hash}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanError(f"cannot load plan {path}: {error}") from error
    _require(isinstance(raw, dict), "plan file must contain an object")
    validate_plan(raw, state, project_dir=project_dir)
    return raw
