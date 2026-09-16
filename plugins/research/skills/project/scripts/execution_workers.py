"""Claude and Codex task workers isolated in coordinator-owned Git worktrees."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from execution_adapter import SubprocessAdapter
from execution_serialization import validate_id

NESTING_ENV = "RESEARCH_PROJECT_WORKER"
SUPPORTED_WORKERS = frozenset({"claude", "codex"})
_ALLOWED_ARGUMENTS = {
    "claude": frozenset({"--effort", "--max-budget-usd", "--model"}),
    "codex": frozenset({"--model", "-m"}),
}


class WorkerError(RuntimeError):
    """Raised when a worker cannot be launched or safely accepted."""


@dataclass(frozen=True)
class WorkerCapability:
    """A locally available non-interactive worker host."""

    kind: str
    executable: str
    version: str


@dataclass(frozen=True)
class TargetSnapshot:
    """The target repository state that must remain unchanged by a worker."""

    root: Path
    head: str
    status: str


@dataclass(frozen=True)
class WorktreeSession:
    """Coordinator-owned isolated checkout for one task attempt."""

    project_dir: Path
    task_id: str
    attempt_id: str
    branch: str
    worktree: Path
    target: TargetSnapshot


@dataclass(frozen=True)
class WorkerLaunch:
    """A prepared worker adapter and its isolated checkout."""

    capability: WorkerCapability
    command: List[str]
    adapter: SubprocessAdapter
    session: WorktreeSession


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    try:
        result = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkerError(f"command failed to run: {argv[0]}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise WorkerError(f"command failed: {' '.join(argv)}: {detail}")
    return result


def discover_worker(kind: str, *, executable: Optional[str] = None) -> WorkerCapability:
    """Find a supported worker CLI and confirm that it can report its version."""
    if kind not in SUPPORTED_WORKERS:
        raise WorkerError(f"unsupported worker: {kind}")
    resolved = executable or shutil.which(kind)
    if not resolved:
        raise WorkerError(f"worker executable is unavailable: {kind}")
    path = str(Path(resolved).expanduser().resolve())
    result = _run([path, "--version"], cwd=Path.cwd(), timeout=10.0)
    version = (result.stdout.strip() or result.stderr.strip()).splitlines()
    if not version:
        raise WorkerError(f"worker did not report a version: {kind}")
    return WorkerCapability(kind=kind, executable=path, version=version[0])


def _validate_worker_arguments(kind: str, arguments: Sequence[str]) -> List[str]:
    safe = list(arguments)
    allowed = _ALLOWED_ARGUMENTS[kind]
    index = 0
    while index < len(safe):
        argument = safe[index]
        option, separator, value = argument.partition("=")
        if option not in allowed:
            raise WorkerError(f"worker argument may override isolation: {argument}")
        if separator:
            if not value:
                raise WorkerError(f"worker argument has no value: {argument}")
            index += 1
            continue
        if index + 1 >= len(safe) or safe[index + 1].startswith("-"):
            raise WorkerError(f"worker argument has no value: {argument}")
        index += 2
    return safe


def _relative_writes(plan: Dict[str, Any], repository: Path) -> List[str]:
    relative: List[str] = []
    resolved_repository = repository.resolve()
    for raw in plan.get("writes", []):
        try:
            path = Path(str(raw)).resolve().relative_to(resolved_repository)
        except ValueError as error:
            raise WorkerError(f"declared write is outside the target repository: {raw}") from error
        relative.append(path.as_posix())
    if not relative:
        raise WorkerError("worker plan has no declared writes")
    return relative


def _display_reads(plan: Dict[str, Any], repository: Path) -> List[str]:
    """Render declared reads relative to the target where possible."""
    displayed: List[str] = []
    resolved_repository = repository.resolve()
    for raw in plan.get("reads", []):
        path = Path(str(raw)).resolve()
        try:
            displayed.append(path.relative_to(resolved_repository).as_posix())
        except ValueError:
            displayed.append(str(path))
    return displayed


def build_worker_prompt(plan: Dict[str, Any], session: WorktreeSession) -> str:
    """Build the bounded task instruction passed to a worker host."""
    writes = _relative_writes(plan, session.target.root)
    listed = "\n".join(f"- {path}" for path in writes)
    reads = _display_reads(plan, session.target.root)
    read_list = "\n".join(f"- {path}" for path in reads) or "- none; use only the task text"
    return (
        f"Complete task {plan['task_id']} in this isolated Git worktree.\n\n"
        f"Task instruction:\n{plan['instruction']}\n\n"
        "You may read only these declared input files:\n"
        f"{read_list}\n\n"
        "You may modify only these repository-relative files:\n"
        f"{listed}\n\n"
        "Do not commit, create branches, add worktrees, start or resume a research project, "
        "or modify files outside this worktree. Do not ask questions; stop with a non-zero "
        "result if the task cannot be completed within these bounds."
    )


def build_worker_command(plan: Dict[str, Any], capability: WorkerCapability, session: WorktreeSession) -> List[str]:
    """Construct a non-interactive command whose safety flags cannot be overridden."""
    kind = capability.kind
    worker = plan.get("worker", {})
    if worker.get("kind") != kind:
        raise WorkerError("plan worker does not match the selected capability")
    arguments = _validate_worker_arguments(kind, worker.get("arguments", []))
    prompt = build_worker_prompt(plan, session)
    if kind == "claude":
        return [
            capability.executable,
            "--print",
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            *arguments,
            prompt,
        ]
    return [
        capability.executable,
        "exec",
        "--sandbox",
        "workspace-write",
        "--config",
        'approval_policy="never"',
        "--ephemeral",
        "--color",
        "never",
        "--cd",
        str(session.worktree),
        *arguments,
        prompt,
    ]


def snapshot_target(cwd: Path) -> TargetSnapshot:
    """Capture the clean repository baseline from which workers may branch."""
    candidate = cwd.expanduser().resolve()
    root_result = _run(["git", "rev-parse", "--show-toplevel"], cwd=candidate)
    root = Path(root_result.stdout.strip()).resolve()
    head = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root).stdout
    if status:
        raise WorkerError("target repository must be clean before parallel worktree execution")
    return TargetSnapshot(root=root, head=head, status=status)


def assert_target_unchanged(snapshot: TargetSnapshot) -> None:
    """Refuse acceptance if anything changed in the user's target checkout."""
    current_head = _run(["git", "rev-parse", "HEAD"], cwd=snapshot.root).stdout.strip()
    current_status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=snapshot.root).stdout
    if current_head != snapshot.head or current_status != snapshot.status:
        raise WorkerError("target repository changed while the worker was running")


def create_worktree(plan: Dict[str, Any], project_dir: Path, attempt_id: str) -> WorktreeSession:
    """Create one coordinator-owned branch and worktree from the target baseline."""
    task_id = validate_id(plan.get("task_id"), "task_id")
    attempt = validate_id(attempt_id, "attempt_id")
    project_name = validate_id(project_dir.name, "project")
    snapshot = snapshot_target(Path(str(plan.get("cwd", ""))))
    worktree = project_dir.resolve() / "execution" / "worktrees" / attempt
    if worktree.exists():
        raise WorkerError(f"worktree path already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = f"research/{project_name}/{task_id}/{attempt}"
    _run(
        ["git", "worktree", "add", "--no-track", "-b", branch, str(worktree), snapshot.head],
        cwd=snapshot.root,
    )
    return WorktreeSession(
        project_dir=project_dir.resolve(),
        task_id=task_id,
        attempt_id=attempt,
        branch=branch,
        worktree=worktree,
        target=snapshot,
    )


def prepare_worker(
    plan: Dict[str, Any],
    project_dir: Path,
    attempt_id: str,
    *,
    executable: Optional[str] = None,
) -> WorkerLaunch:
    """Create an isolated checkout and a subprocess-backed worker adapter."""
    if os.environ.get(NESTING_ENV):
        raise WorkerError("nested research project workers are forbidden")
    worker = plan.get("worker", {})
    kind = worker.get("kind")
    capability = discover_worker(str(kind), executable=executable)
    session = create_worktree(plan, project_dir, attempt_id)
    try:
        command = build_worker_command(plan, capability, session)
    except Exception:
        discard_worktree(session)
        raise
    adapter = SubprocessAdapter(
        command,
        cwd=str(session.worktree),
        env={NESTING_ENV: "1"},
    )
    return WorkerLaunch(capability=capability, command=command, adapter=adapter, session=session)


def changed_paths(session: WorktreeSession) -> Set[str]:
    """Return all tracked and untracked mutations, rejecting rename ambiguity."""
    result = _run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=session.worktree).stdout
    entries = result.split("\0")
    changed: Set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            raise WorkerError("worker renames and copies are not supported")
        changed.add(Path(path).as_posix())
    return changed


def inspect_worker_result(session: WorktreeSession, plan: Dict[str, Any]) -> List[str]:
    """Validate worker mutations and required outputs without touching the target tree."""
    assert_target_unchanged(session.target)
    declared = set(_relative_writes(plan, session.target.root))
    changed = changed_paths(session)
    undeclared = sorted(changed - declared)
    if undeclared:
        raise WorkerError(f"worker modified undeclared paths: {', '.join(undeclared)}")
    if not changed:
        raise WorkerError("worker produced no repository mutations")
    for output in plan.get("outputs", []):
        if output.get("required") is not True:
            continue
        relative = Path(str(output.get("path")))
        candidate = session.worktree / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise WorkerError(f"required worker output is missing or unsafe: {relative.as_posix()}")
    return sorted(changed)


def commit_worker_result(session: WorktreeSession, plan: Dict[str, Any]) -> str:
    """Commit an accepted mutation set on its disposable task branch."""
    paths = inspect_worker_result(session, plan)
    _run(["git", "add", "--", *paths], cwd=session.worktree)
    _run(
        [
            "git",
            "-c",
            "user.name=Research Project Worker",
            "-c",
            "user.email=research-project-worker@localhost",
            "commit",
            "-m",
            f"research task {session.task_id}",
        ],
        cwd=session.worktree,
    )
    commit = _run(["git", "rev-parse", "HEAD"], cwd=session.worktree).stdout.strip()
    assert_target_unchanged(session.target)
    return commit


def cleanup_worktree(session: WorktreeSession, accepted_commit: str) -> None:
    """Remove a clean accepted worktree and its disposable branch."""
    assert_target_unchanged(session.target)
    head = _run(["git", "rev-parse", "HEAD"], cwd=session.worktree).stdout.strip()
    status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=session.worktree).stdout
    if head != accepted_commit or status:
        raise WorkerError("refusing to clean an unaccepted or dirty worker worktree")
    _run(["git", "worktree", "remove", str(session.worktree)], cwd=session.target.root)
    _run(["git", "branch", "-D", session.branch], cwd=session.target.root)


def discard_worktree(session: WorktreeSession) -> None:
    """Discard a worktree only when setup failed before a worker could run."""
    _run(["git", "worktree", "remove", "--force", str(session.worktree)], cwd=session.target.root)
    _run(["git", "branch", "-D", session.branch], cwd=session.target.root)
