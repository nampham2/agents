"""The gate profile: discover, approve, run, scan, waive and the finish check (design section 4).

A gated project cannot finish a code task without a passing run of the commands the user approved. Everything
that touches the target repository runs with the repository's absolute path as its working directory, taken from
the project's recorded `target` worktree, never from `working_directory`, which may be the main checkout.
"""

from __future__ import annotations

import ast
import configparser
import copy
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import workspace_observe as observe
from workspace_checks import verify
from workspace_documents import append_record
from workspace_fields import GATE_NO_REPOSITORY, GATE_REQUIRED_KINDS, GATE_WAIVER_KINDS
from workspace_journal import list_input, object_input, schema_input, string_input
from workspace_lib import (
    DirectoryLock,
    ValidationReport,
    WorkspaceError,
    _validate_gate_commands,
    atomic_write_json,
    is_code_task,
    load_json,
    now_iso,
)
from workspace_session import _load_state

GIT_TIMEOUT = 120


def _git_bytes(
    repo: Path,
    *arguments: str,
    environment: "dict[str, str] | None" = None,
) -> bytes:
    """One Git query in `repo`; `environment` adds variables, as a snapshot's private index and object store do."""
    full = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", **(environment or {})}
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", str(repo), *arguments],
            capture_output=True,
            env=full,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"gates-no-repository: git could not run in {repo}: {error}") from error
    if done.returncode:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise WorkspaceError(f"git {arguments[0]} failed in {repo}: {detail[-1] if detail else done.returncode}")
    return done.stdout


def _git(repo: Path, *arguments: str) -> str:
    return _git_bytes(repo, *arguments).decode("utf-8", "replace").strip()


@contextmanager
def _snapshot(repo: Path, base_commit: str) -> Iterator[dict[str, str]]:
    """The environment of a private snapshot: an index holding the working tree, built from the base tree.

    Reading the repository through its own index is unsafe twice over. A plain `git diff` refreshes stale stat data
    in the real index even with optional locks off. And Git trusts a file whose size and mtime second match its
    index entry, so a quick edit of the same size can read as unchanged (a copy of the index loses Git's racy-clean
    protection). A snapshot starts from `read-tree <base>`, which has no stat data, and `add -A` then hashes every
    file, ignored paths excepted, so what a query sees is exactly the content. The snapshot writes blobs to a
    temporary object directory that reads the real one as an alternate, so the real index, the real object store
    and the working tree are never written.
    """
    with tempfile.TemporaryDirectory(prefix="gates-snapshot-") as scratch:
        objects = Path(scratch) / "objects"
        objects.mkdir()
        common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        environment = {
            "GIT_INDEX_FILE": str(Path(scratch) / "index"),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects"),
        }
        _git_bytes(repo, "read-tree", base_commit, environment=environment)
        _git_bytes(repo, "add", "-A", "--", ".", environment=environment)
        yield environment


def current_head(repo: Path) -> str:
    """The commit `repo` is at. An unborn repository and a directory that is no repository are refused."""
    try:
        _git(repo, "rev-parse", "--git-dir")
    except WorkspaceError as error:
        raise WorkspaceError(f"gates-no-repository: {repo} is not a Git repository ({error})") from error
    try:
        return _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    except WorkspaceError as error:
        raise WorkspaceError(
            f"gates-no-commit: {repo} has no commit yet; make an initial commit first, then approve the gates"
        ) from error


def head_tree(repo: Path, base_commit: str) -> str:
    """A sha256 over the whole change since `base_commit`, committed or not, untracked files included.

    The text is `git diff --cached --binary --full-index` of the snapshot against the base, which holds every path
    that differs and its full content. Ignored paths are left out; untracked and unignored ones (`.coverage`,
    caches) are part of the tree and stay stable between a run and the finish that checks it.
    """
    with _snapshot(repo, base_commit) as environment:
        text = _git_bytes(
            repo,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-color",
            base_commit,
            "--",
            environment=environment,
        )
    return hashlib.sha256(text).hexdigest()


# --- the target repository -----------------------------------------------------------------------------------


def target_repository(state: dict[str, Any]) -> "Path | None":
    """The repository the gates run in: the recorded `target` worktree, or None when there is none to gate.

    `working_directory` is not used: it may be the main checkout while the work happens in a worktree. A target
    entry of kind `none` (a target that is not a repository) and a project with no target entry both give None.
    """
    for entry in state.get("worktrees", []):
        if isinstance(entry, dict) and entry.get("role") == "target" and entry.get("status") != "removed":
            if entry.get("kind") == "none" or not isinstance(entry.get("path"), str):
                return None
            return Path(entry["path"])
    return None


def _need_repository(state: dict[str, Any]) -> Path:
    repo = target_repository(state)
    if repo is None:
        raise WorkspaceError(
            "gates-no-repository: this project records no target repository, so it cannot be gated; "
            'the only way to finish its code tasks is `gates waive` with kinds ["no-repository"] and the user\'s words'
        )
    return repo


# --- discovery ------------------------------------------------------------------------------------------------

TEXT_SOURCES = ("AGENTS.md", "CLAUDE.md")
SHELL_MARKERS = ("|", "&&", "||", ";", ">", "<", "$(", "`", "$", "...", "<project", "\\")
WEAKENING_FLAGS = ("--no-cov", "--cov-branch", "--cov-report=json")
RUN_LINE = re.compile(r"^(?P<indent>\s*)(?:-\s+)?(?:\w[\w-]*:\s+)*run:\s*(?P<rest>.*)$")
INLINE_COMMAND = re.compile(r"`([^`\n]+)`")
TOOL_KINDS = {
    "flake8": "lint",
    "pylint": "lint",
    "mypy": "types",
    "pyright": "types",
    "pytest": "tests",
    "py.test": "tests",
}
PYPROJECT_TABLES = (
    ("ruff", "lint", ["ruff", "check", "."]),
    ("ty", "types", ["ty", "check", "."]),
    ("mypy", "types", ["mypy", "."]),
    ("pytest", "tests", ["pytest", "-q"]),
)


def classify_command(argv: list[str]) -> "str | None":
    """The gate kind of a command line, or None when it is not a check this profile runs."""
    names = [os.path.basename(item) for item in argv]
    for position, name in enumerate(names):
        rest = argv[position + 1 :]
        if name == "ruff":
            if "check" in rest:
                return None if "--fix" in rest else "lint"
            if "format" in rest:
                return "format" if "--check" in rest or "--diff" in rest else None
            return None
        if name == "ty":
            return "types" if "check" in rest else None
        if name in TOOL_KINDS:
            return TOOL_KINDS[name]
    return None


def _split(text: str) -> "list[str] | None":
    """The argv of one documented command line, or None when it is not a plain one."""
    text = re.sub(r"\s#.*$", "", text.strip().removeprefix("$ ").strip())
    if not text or any(marker in text for marker in SHELL_MARKERS):
        return None
    try:
        return shlex.split(text)
    except ValueError:
        return None


def _lines_of_commands(text: str) -> "list[tuple[int, str]]":
    """Command-like lines of a Markdown file: fenced lines and inline code spans, with their line numbers."""
    found: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            found.append((number, line))
        else:
            found.extend((number, match.group(1)) for match in INLINE_COMMAND.finditer(line))
    return found


def _workflow_lines(text: str) -> "list[tuple[int, str]]":
    """The `run:` command lines of a CI workflow, single-line and block-scalar forms."""
    found: list[tuple[int, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = RUN_LINE.match(lines[index])
        index += 1
        if match is None:
            continue
        rest = match.group("rest").strip()
        if rest[:1] in ("|", ">"):
            indent = len(match.group("indent"))
            while index < len(lines) and (
                not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent
            ):
                found.append((index + 1, lines[index]))
                index += 1
        elif rest:
            found.append((index, rest))
    return found


def _usable(argv: "list[str] | None") -> bool:
    return bool(argv) and not any(flag in " ".join(argv or []) for flag in WEAKENING_FLAGS)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidates_in(repo: Path, relative: str, lines: "list[tuple[int, str]]") -> "list[dict[str, Any]]":
    found = []
    for number, text in lines:
        argv = _split(text)
        kind = classify_command(argv) if _usable(argv) and argv else None
        if kind is not None and argv is not None:
            found.append({"kind": kind, "argv": argv, "source": relative, "line": number})
    del repo
    return found


def _pyproject_candidates(repo: Path) -> "list[dict[str, Any]]":
    path = repo / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        tools = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {})
    except (OSError, ValueError):
        return []
    if not isinstance(tools, dict):
        return []
    return [
        {"kind": kind, "argv": list(argv), "source": "pyproject.toml", "line": 0}
        for table, kind, argv in PYPROJECT_TABLES
        if table in tools
    ]


def discover_in(repo: Path) -> dict[str, Any]:
    """Candidate commands and the source files they came from, read from `repo`; nothing is written."""
    candidates: list[dict[str, Any]] = []
    sources: list[dict[str, str]] = []
    files = [(name, _lines_of_commands) for name in TEXT_SOURCES]
    workflows = (
        sorted((repo / ".github" / "workflows").glob("*.y*ml")) if (repo / ".github" / "workflows").is_dir() else []
    )
    files += [(str(path.relative_to(repo)), _workflow_lines) for path in workflows]
    for relative, reader in files:
        path = repo / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = _candidates_in(repo, relative, reader(text))
        if found:
            candidates.extend(found)
            sources.append({"path": relative, "sha256": _file_sha256(path)})
    # A configured tool is a fallback for a kind no document names; it has no source hash to go stale.
    covered = {item["kind"] for item in candidates}
    candidates.extend(item for item in _pyproject_candidates(repo) if item["kind"] not in covered)
    unique: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in candidates:
        if any(item["argv"] == other["argv"] for other in unique):
            continue
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        unique.append(
            {"id": item["kind"] if counts[item["kind"]] == 1 else f"{item['kind']}-{counts[item['kind']]}", **item}
        )
    return {"candidates": unique, "sources": sources}


def _waiver_of(gates: dict[str, Any]) -> dict[str, Any]:
    """The waiver of a `gates` object as a dict; an absent one is empty."""
    waiver = gates.get("waiver")
    return waiver if isinstance(waiver, dict) else {}


def missing_kinds(commands: "list[dict[str, Any]]", waiver: "dict[str, Any] | None") -> list[str]:
    """The mandatory kinds with neither an approved command nor a waiver."""
    have = {item["kind"] for item in commands} | set((waiver or {}).get("kinds", []))
    return [kind for kind in GATE_REQUIRED_KINDS if kind not in have]


def gates_discover(project_dir: Path) -> dict[str, Any]:
    """`gate-discover`: the candidates, their sources and what an approval would still lack. Writes nothing."""
    state = load_json(project_dir.resolve() / "project.json")
    repo = target_repository(state)
    if repo is None or not repo.is_dir():
        return {
            "repository": None,
            "head": None,
            "candidates": [],
            "sources": [],
            "missing_kinds": list(GATE_REQUIRED_KINDS),
        }
    found = discover_in(repo)
    try:
        head: "str | None" = current_head(repo)
    except WorkspaceError:
        head = None
    stored = state.get("gates")
    gates: dict[str, Any] = stored if isinstance(stored, dict) else {}
    return {
        "repository": str(repo),
        "head": head,
        **found,
        "missing_kinds": missing_kinds(found["candidates"], _waiver_of(gates)),
    }


# --- approval and waiver --------------------------------------------------------------------------------------

GATE_STATUSES = ("PLANNING", "EXECUTING", "REVIEW")


def is_pytest(argv: list[str]) -> bool:
    """Whether the command is a pytest invocation: an argument named pytest, or the one that follows `-m`."""
    return any(os.path.basename(item) in ("pytest", "py.test") for item in argv) or any(
        left == "-m" and right == "pytest" for left, right in itertools.pairwise(argv)
    )


def _coverage_conflict(argv: list[str]) -> "str | None":
    for position, item in enumerate(argv):
        if item == "--no-cov" or item.startswith("--cov-branch") or item.startswith("--cov-report=json"):
            return item
        if (item == "-p" and argv[position + 1 : position + 2] == ["no:cov"]) or item == "-pno:cov":
            return "-p no:cov"
    return None


def _approved_commands(value: object) -> "list[dict[str, Any]]":
    """The request's command list after its refusals: closed objects, a known kind, a pytest tests command."""
    commands = list_input(value, 20, "commands")
    if not commands:
        raise WorkspaceError("approval-invalid: commands must be a non-empty list")
    checked: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        object_input(command, {"id", "kind", "argv"}, {"id", "kind", "argv"}, where=f"commands[{index}]")
        report = ValidationReport()
        _validate_gate_commands([command], report)
        if report.errors:
            raise WorkspaceError(report.errors[0].replace("gates.commands[0]", f"commands[{index}]"))
        if any(command["id"] == other["id"] for other in checked):
            raise WorkspaceError(f"commands[{index}]: duplicate command id {command['id']!r}")
        if command["kind"] == "tests":
            if not is_pytest(command["argv"]):
                raise WorkspaceError(
                    f"approval-invalid: the tests command {command['id']!r} must be a pytest invocation "
                    "(an argument named pytest, or `-m pytest`), so the run can measure coverage"
                )
            conflict = _coverage_conflict(command["argv"])
            if conflict:
                raise WorkspaceError(
                    f"approval-invalid: the tests command {command['id']!r} holds {conflict}; the gate run adds "
                    "its own coverage options and refuses an argv that switches coverage off or owns its report"
                )
        checked.append(command)
    return checked


def _gate_confirmation(request: dict[str, Any]) -> dict[str, str]:
    confirmation = request["confirmation"]
    return {key: string_input(confirmation[key], f"confirmation.{key}") for key in ("source", "response")}


def gates_change(
    project_dir: Path, state: dict[str, Any], request: dict[str, Any]
) -> "tuple[dict[str, Any], str, dict[str, Any]]":
    """The patch, decision body and result of `gates approve` or `gates waive`, after their refusals."""
    del project_dir
    if state["status"] not in GATE_STATUSES:
        raise WorkspaceError(
            f"gates change needs a PLANNING, EXECUTING or REVIEW project; this one is {state['status']}"
        )
    operation = request.get("operation")
    if operation not in ("approve", "waive"):
        raise WorkspaceError("gates operation must be approve or waive")
    confirmation = _gate_confirmation(request)
    existing = state.get("gates")
    new: dict[str, Any] = (
        copy.deepcopy(existing)
        if isinstance(existing, dict)
        else {
            "enabled": True,
            "commands": [],
            "approval": None,
            "waiver": None,
        }
    )
    if operation == "approve":
        outcome = _approve(state, request, new, confirmation)
    else:
        outcome = _waive(state, request, new, confirmation)
    body = (
        f"kind: gates\noperation: {operation}\n{outcome.pop('summary')}\n"
        f"source: {confirmation['source']}\nresponse: {confirmation['response']}"
    )
    outcome["missing_kinds"] = missing_kinds(new["commands"], new["waiver"])
    return {"gates": new}, body, outcome


def _approve(
    state: dict[str, Any], request: dict[str, Any], new: dict[str, Any], confirmation: dict[str, str]
) -> dict[str, Any]:
    commands = _approved_commands(request.get("commands"))
    repo = _need_repository(state)
    head = current_head(repo)
    merged = {item["id"]: item for item in new["commands"]}
    for command in commands:
        merged[command["id"]] = command
    found = discover_in(repo)
    new.update(
        enabled=True,
        commands=list(merged.values()),
        sources=found["sources"],
        base_commit=new.get("base_commit") or head,
        approval={"source": confirmation["source"], "response": confirmation["response"], "approved_at": now_iso()},
    )
    return {
        "commands": [item["id"] for item in new["commands"]],
        "base_commit": new["base_commit"],
        "sources": [item["path"] for item in found["sources"]],
        "summary": "commands: "
        + ", ".join(f"{item['id']} ({item['kind']}): {shlex.join(item['argv'])}" for item in commands),
    }


def _waive(
    state: dict[str, Any], request: dict[str, Any], new: dict[str, Any], confirmation: dict[str, str]
) -> dict[str, Any]:
    kinds = request.get("kinds")
    if not isinstance(kinds, list) or not kinds:
        raise WorkspaceError("gates waiver: kinds must be a non-empty list")
    if not all(item in GATE_WAIVER_KINDS for item in kinds):
        raise WorkspaceError(f"gates waiver: kinds must hold only {', '.join(GATE_WAIVER_KINDS)}")
    reason = string_input(request.get("reason"), "reason").strip()
    repo = target_repository(state)
    if GATE_NO_REPOSITORY in kinds and repo is not None:
        raise WorkspaceError(
            f"the {GATE_NO_REPOSITORY} waiver is for a target that is not a repository, but {repo} is a repository"
        )
    if repo is None and any(item != GATE_NO_REPOSITORY for item in kinds):
        _need_repository(state)
    if repo is not None and not new.get("base_commit"):
        new["base_commit"] = current_head(repo)
    union = list(dict.fromkeys([*(new["waiver"] or {}).get("kinds", []), *kinds]))
    new["waiver"] = {
        "reason": reason,
        "source": confirmation["source"],
        "response": confirmation["response"],
        "granted_at": now_iso(),
        "kinds": union,
    }
    return {"waived": union, "summary": f"waived: {', '.join(kinds)}\nreason: {reason}"}


# --- the change since the base: hunks, file classes -----------------------------------------------------------

HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@")
CONFIG_SUFFIXES = (".toml", ".yaml", ".yml", ".ini", ".cfg")
CONFIG_NAMES = ("pyproject.toml", "setup.cfg", "ruff.toml", ".coveragerc", "tox.ini")
PROSE_SUFFIXES = (".md", ".rst", ".txt")
LANGUAGES = {
    ".py": "python",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".ini": "ini",
    ".cfg": "ini",
    ".json": "json",
    ".sh": "shell",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}


@dataclass
class FileChange:
    """One file's change against the base: added lines by new line number, a removal count, binary or not."""

    path: str
    old_path: "str | None" = None
    status: str = "modified"
    binary: bool = False
    added: dict[int, str] = field(default_factory=dict)
    removed: int = 0
    deletions: list[int] = field(default_factory=list)

    @property
    def touched(self) -> set[int]:
        """New-file lines a change touches: the added ones, and the line before each pure deletion."""
        return set(self.added) | {line for line in self.deletions if line >= 1}


def classify_file(path: str) -> str:
    """`test`, `config`, `doc`, `source` or `other`, by path alone (design, gate-run mechanics item 8)."""
    parts = Path(path).parts
    name = parts[-1] if parts else path
    if any(part in ("tests", "test") for part in parts[:-1]) or (
        name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    ):
        return "test"
    if name in CONFIG_NAMES or name.endswith(CONFIG_SUFFIXES):
        return "config"
    if name.endswith(PROSE_SUFFIXES):
        return "doc"
    return "source" if name.endswith(".py") else "other"


def language_of(path: str) -> str:
    return LANGUAGES.get(Path(path).suffix, "other")


def _header_path(line: str) -> "str | None":
    """The path of a `---` or `+++` header line, None for /dev/null."""
    text = line[4:].split("\t")[0]
    if text == "/dev/null":
        return None
    return text[2:] if text[:2] in ("a/", "b/") else text


def parse_diff(text: str) -> list[FileChange]:
    """The `git diff -U0` text as file changes. Hunk line counts decide what is body and what is a header."""
    changes: list[FileChange] = []
    current: "FileChange | None" = None
    old_left = new_left = 0
    new_line = 0
    for line in text.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("+") and new_left > 0 and current is not None:
                current.added[new_line] = line[1:]
                new_line += 1
                new_left -= 1
                continue
            if line.startswith("-") and old_left > 0 and current is not None:
                current.removed += 1
                old_left -= 1
                continue
        if line.startswith("diff --git "):
            old_left = new_left = 0
            header = line[len("diff --git ") :]
            current = FileChange(path=header.rsplit(" b/", 1)[-1] if " b/" in header else header)
            changes.append(current)
        elif current is None:
            continue
        elif (hunk := HUNK.match(line)) is not None:
            old_left = int(hunk.group("old_count") or 1)
            new_left = int(hunk.group("new_count") or 1)
            new_line = int(hunk.group("new"))
            if new_left == 0:
                current.deletions.append(new_line)
        elif line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.old_path, current.status = line[len("rename from ") :], "renamed"
        elif line.startswith("rename to "):
            current.path = line[len("rename to ") :]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- ") and current.status == "deleted":
            current.path = _header_path(line) or current.path
        elif line.startswith("+++ ") and (target := _header_path(line)) is not None:
            current.path = target
    return changes


def changed_files(repo: Path, base_commit: str) -> list[FileChange]:
    """Everything changed since `base_commit`, committed or not; an untracked file is a wholly added one."""
    with _snapshot(repo, base_commit) as environment:
        text = _git_bytes(
            repo,
            "diff",
            "--cached",
            "-U0",
            "--find-renames",
            "--no-ext-diff",
            "--no-color",
            base_commit,
            "--",
            environment=environment,
        ).decode("utf-8", "replace")
    return parse_diff(text)


# --- the diff scan: bypasses on added lines -------------------------------------------------------------------

BYPASS_PATTERNS = (
    ("type: ignore", re.compile(r"#\s*type:\s*ignore")),
    ("noqa", re.compile(r"#\s*noqa")),
    ("pyright: ignore", re.compile(r"#\s*pyright:\s*ignore")),
    ("ty: ignore", re.compile(r"#\s*ty:\s*ignore")),
    ("cast", re.compile(r"\bcast\(")),
    ("pytest.mark.skip", re.compile(r"pytest\.mark\.skip\b")),
    ("xfail", re.compile(r"\bxfail\b")),
    ("unittest.skip", re.compile(r"\bunittest\.skip\b")),
    ("pragma: no cover", re.compile(r"#\s*pragma:\s*no\s*cover")),
)
CONDITIONAL_PATTERNS = (
    ("pytest.mark.skipif", re.compile(r"pytest\.mark\.skipif\b")),
    ("pytest.skip", re.compile(r"\bpytest\.skip\(")),
    ("unittest.skipif", re.compile(r"\bunittest\.skip(?:If|Unless)\b")),
)
NO_COV = re.compile(r"--no-cov\b")
GUARD = re.compile(r"\s*(?:if|elif|else|except)\b")
REASON_KEYWORD = re.compile(r"\breason\s*=")
FIRST_STRING = re.compile(r"\(\s*[rRuUbB]*['\"]")
SECOND_STRING = re.compile(r",\s*[rRuU]*['\"]")


def _call_text(lines: list[str], number: int) -> str:
    """The text of the call that starts on line `number` (1-based), up to its closing parenthesis."""
    collected: list[str] = []
    depth = 0
    for text in lines[number - 1 : number + 9]:
        collected.append(text)
        depth += text.count("(") - text.count(")")
        if depth <= 0:
            break
    return "\n".join(collected)


def _guarded(lines: list[str], number: int) -> bool:
    """Whether the statement on line `number` sits under an `if`, `elif`, `else` or `except` block."""
    own = lines[number - 1]
    if re.match(r"\s*(?:if|elif)\b", own):
        return True
    indent = len(own) - len(own.lstrip())
    for text in reversed(lines[: number - 1]):
        if text.strip() and len(text) - len(text.lstrip()) < indent:
            return GUARD.match(text) is not None
    return False


def _conditional(name: str, lines: list[str], number: int) -> bool:
    """Whether a skip carries its condition and a reason, so it is information and not a bypass."""
    call = _call_text(lines, number)
    if name == "pytest.skip":
        return _guarded(lines, number) and bool(REASON_KEYWORD.search(call) or FIRST_STRING.search(call))
    if name == "unittest.skipif":
        return bool(REASON_KEYWORD.search(call) or SECOND_STRING.search(call))
    return bool(REASON_KEYWORD.search(call))


def scan_changes(repo: Path, changes: list[FileChange]) -> "tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    """Bypasses on lines the change added (findings) and what a reviewer should look at (information).

    Only added lines count, so an ignore already in the base never fails. Python patterns apply to `.py` files,
    `--no-cov` to every file that is not prose. A skip with a condition and a reason is information.
    """
    findings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    for change in changes:
        kind = classify_file(change.path)
        if Path(change.path).name == "conftest.py":
            info.append({"code": "conftest-changed", "file": change.path, "line": 0, "detail": "fixtures and hooks"})
        if change.status == "deleted" and kind == "test":
            info.append({"code": "test-deleted", "file": change.path, "line": 0, "detail": "test file deleted"})
        if change.binary or kind == "doc" or not change.added:
            continue
        lines: list[str] = []
        if change.path.endswith(".py"):
            try:
                lines = (repo / change.path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
        for number, text in sorted(change.added.items()):
            hits: list[tuple[str, bool]] = []
            if change.path.endswith(".py"):
                hits.extend((name, False) for name, pattern in BYPASS_PATTERNS if pattern.search(text))
                hits.extend(
                    (name, _conditional(name, lines or [text], number if lines else 1))
                    for name, pattern in CONDITIONAL_PATTERNS
                    if pattern.search(text)
                )
            if NO_COV.search(text):
                hits.append(("--no-cov", False))
            for name, conditional in hits:
                entry = {"file": change.path, "line": number, "detail": name}
                if conditional:
                    info.append({"code": "conditional-skip", **entry})
                else:
                    findings.append({"code": "bypass-added", **entry})
    return findings, info


# --- the config thresholds ------------------------------------------------------------------------------------

GATE_CONFIG_FILES = ("pyproject.toml", "setup.cfg", "ruff.toml", ".coveragerc")
KEY_ALIASES = {
    "fail-under": "fail_under",
    "max_complexity": "max-complexity",
    "line_length": "line-length",
    "max-line-length": "line-length",
    "extend_ignore": "extend-ignore",
    "extend_exclude": "extend-exclude",
    "per_file_ignores": "per-file-ignores",
}
FLOOR_KEYS = ("fail_under",)
CEILING_KEYS = ("max-complexity", "line-length")
REQUIRED_KEYS = ("select", "testpaths")
FORBID_KEYS = ("ignore", "extend-ignore", "omit", "exclude", "extend-exclude", "per-file-ignores")
EXACT_KEYS = ("extend",)
OPTION_KEYS = ("addopts",)
WATCHED_KEYS = (*FLOOR_KEYS, *CEILING_KEYS, *REQUIRED_KEYS, *FORBID_KEYS, *EXACT_KEYS, *OPTION_KEYS)
WEAK_OPTIONS = ("--no-cov", "--ignore", "--deselect", "-pno:", "no:")


def _collect(data: dict[str, Any], found: dict[str, list[Any]]) -> None:
    """Every watched key at any depth, under its canonical name. Moving a key between tables is not a change."""
    for key, value in data.items():
        name = KEY_ALIASES.get(key, key)
        if name in WATCHED_KEYS:
            found.setdefault(name, []).append(value)
        elif isinstance(value, dict):
            _collect(value, found)


def _cfg_value(name: str, raw: str) -> Any:
    try:
        return float(raw)
    except ValueError:
        pass
    if name in OPTION_KEYS or name in EXACT_KEYS:
        return raw.strip()
    return [item.strip() for item in re.split(r"[\n,]", raw) if item.strip()]


def _config_values(name: str, text: str) -> dict[str, list[Any]]:
    """The watched values of one config file; raises ValueError when it does not parse."""
    found: dict[str, list[Any]] = {}
    if name.endswith(".toml"):
        _collect(tomllib.loads(text), found)
        return found
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text if re.search(r"^\[", text, re.MULTILINE) else f"[report]\n{text}")
    except configparser.Error as error:
        raise ValueError(str(error)) from error
    for section in parser.sections():
        _collect(
            {
                KEY_ALIASES.get(key, key): _cfg_value(KEY_ALIASES.get(key, key), raw)
                for key, raw in parser.items(section)
            },
            found,
        )
    return found


def _strings(values: list[Any]) -> set[str]:
    """Every entry of the lists behind a key; a table of lists (per-file-ignores) reads as `path:entry`."""
    found: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            found.update(
                f"{key}:{item}"
                for key, entries in value.items()
                for item in (entries if isinstance(entries, list) else [entries])
            )
        elif isinstance(value, list):
            found.update(str(item) for item in value)
        else:
            found.add(str(value))
    return found


def _numbers(values: list[Any]) -> list[float]:
    return [float(item) for item in values if type(item) in (int, float)]


def _compare(name: str, old: "list[Any] | None", new: "list[Any] | None") -> list[str]:
    """What weakened between the base and the new values of one watched key."""
    if new is None:
        return [f"{name} removed"] if old is not None and name not in FORBID_KEYS else []
    if name in FLOOR_KEYS and old is not None and _numbers(new) and _numbers(old):
        if min(_numbers(new)) < min(_numbers(old)):
            return [f"{name} lowered from {min(_numbers(old)):g} to {min(_numbers(new)):g}"]
    if name in CEILING_KEYS and old is not None and _numbers(new) and _numbers(old):
        if max(_numbers(new)) > max(_numbers(old)):
            return [f"{name} raised from {max(_numbers(old)):g} to {max(_numbers(new)):g}"]
    before = _strings(old or [])
    after = _strings(new)
    if name in REQUIRED_KEYS and before - after:
        return [f"{name} lost {', '.join(sorted(before - after))}"]
    if name in FORBID_KEYS and after - before:
        return [f"{name} gained {', '.join(sorted(after - before))}"]
    if name in EXACT_KEYS and old is not None and before != after:
        return [f"{name} changed from {', '.join(sorted(before))} to {', '.join(sorted(after))}"]
    if name in OPTION_KEYS:
        tokens_before = {token for item in old or [] for token in shlex.split(str(item))}
        tokens_after = {token for item in new for token in shlex.split(str(item))}
        weak = sorted(token for token in tokens_after - tokens_before if token.startswith(WEAK_OPTIONS))
        lost = sorted(tokens_before - tokens_after)
        if weak or lost:
            return [
                f"{name} "
                + "; ".join(
                    part
                    for part in (f"lost {', '.join(lost)}" if lost else "", f"gained {', '.join(weak)}" if weak else "")
                    if part
                )
            ]
    return []


def scan_config(repo: Path, base_commit: str, changes: list[FileChange]) -> list[dict[str, Any]]:
    """`floor-lowered` findings: a changed gate config file compared with its text at the base commit."""
    findings: list[dict[str, Any]] = []
    for change in changes:
        name = Path(change.path).name
        if name not in GATE_CONFIG_FILES or change.binary:
            continue

        def finding(detail: str, path: str = change.path) -> None:
            findings.append({"code": "floor-lowered", "file": path, "line": 0, "detail": detail})

        if change.status == "deleted":
            finding(f"{change.path} deleted")
            continue
        try:
            new_values = _config_values(name, (repo / change.path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            finding(f"{change.path} cannot parse: {error}")
            continue
        try:
            old_text = _git(repo, "show", f"{base_commit}:{change.old_path or change.path}")
            old_values = _config_values(name, old_text)
        except (WorkspaceError, ValueError):
            old_values = {}
        for key in sorted(new_values.keys() | old_values.keys()):
            for detail in _compare(key, old_values.get(key), new_values.get(key)):
                finding(f"{key}: {detail}" if not detail.startswith(key) else detail)
    return findings


# --- diff coverage ---------------------------------------------------------------------------------------------

FILE_COVERAGE_KEYS = ("executed_lines", "missing_lines", "executed_branches", "missing_branches")


def _coverage_format_problem(report: object) -> "str | None":
    """Why a coverage JSON cannot be read for diff coverage, or None. It must hold branch data per file."""
    if not isinstance(report, dict):
        return "the report is not a JSON object"
    meta = report.get("meta")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        return "meta.branch_coverage is not true; the run must measure branches (--cov-branch)"
    files = report.get("files")
    if not isinstance(files, dict):
        return "the report has no files object"
    for name, entry in files.items():
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), list) for key in FILE_COVERAGE_KEYS):
            return f"{name} lacks {', '.join(FILE_COVERAGE_KEYS)} as lists"
    return None


def _report_key(repo: Path, key: str) -> str:
    path = Path(key)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return key
    return path.as_posix()


def _statement_lines(source: str) -> "set[int] | None":
    """The lines that start a statement (and decorators), as coverage counts them; None when it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            lines.add(node.lineno)
            lines.update(item.lineno for item in getattr(node, "decorator_list", []))
    return lines


def diff_coverage(repo: Path, changes: list[FileChange], report: object) -> dict[str, Any]:
    """Covered and total changed executable lines and branches of the changed source files, with any failures.

    Branches count when their source line is a changed line. A changed `.py` source file with a changed
    statement that the run did not measure is `coverage-missing`; a report without branch data is
    `coverage-format`. No changed executable lines is zero over zero.
    """
    problem = _coverage_format_problem(report)
    if problem is not None:
        return {"covered": 0, "total": 0, "failures": [f"coverage-format: {problem}"]}
    assert isinstance(report, dict)
    measured = {_report_key(repo, name): entry for name, entry in report["files"].items()}
    covered = total = 0
    unmeasured: list[str] = []
    for change in changes:
        if change.status == "deleted" or change.binary or not change.path.endswith(".py"):
            continue
        if classify_file(change.path) != "source":
            continue
        added = set(change.added)
        entry = measured.get(change.path)
        if entry is None:
            try:
                statements = _statement_lines((repo / change.path).read_text(encoding="utf-8"))
            except OSError:
                statements = None
            if statements is None or statements & added:
                unmeasured.append(change.path)
            continue
        executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
        covered += len(added & executed)
        total += len(added & (executed | missing))
        for pairs, hit in ((entry["executed_branches"], 1), (entry["missing_branches"], 0)):
            count = sum(1 for pair in pairs if isinstance(pair, list) and pair and pair[0] in added)
            covered += hit * count
            total += count
    failures = (
        [f"coverage-missing: the run did not measure changed source file(s): {', '.join(unmeasured)}"]
        if unmeasured
        else []
    )
    return {"covered": covered, "total": total, "failures": failures}


# --- touched-function complexity -------------------------------------------------------------------------------

COMPLEXITY_LIMIT = 10
COMPLEXITY_MESSAGE = re.compile(r"\((\d+) > \d+\)")


class Extent(NamedTuple):
    """A function: its name, its `def` line (where ruff reports it) and the lines it spans, decorators included."""

    name: str
    line: int
    start: int
    end: int


def function_extents(source: str) -> "list[Extent] | None":
    """Every function and method of `source` with its line extent, or None when the source does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    extents = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            extents.append(Extent(node.name, node.lineno, start, node.end_lineno or node.lineno))
    return sorted(extents, key=lambda item: item.line)


def ruff_argv() -> list[str]:
    """How to run ruff for the complexity check: on the PATH, else this interpreter's module, else `uv run`."""
    found = shutil.which("ruff")
    if found:
        return [found]
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    return ["uv", "run", "ruff"]


def _ruff_complexity(repo: Path, files: list[str]) -> "list[dict[str, Any]]":
    """The C901 violations of `files` at the fixed limit, ignoring every config file (`--isolated`)."""
    command = [
        *ruff_argv(),
        "check",
        "--isolated",
        "--no-cache",
        "--select",
        "C901",
        "--config",
        f"lint.mccabe.max-complexity={COMPLEXITY_LIMIT}",
        "--output-format",
        "json",
        "--",
        *files,
    ]
    try:
        done = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"complexity check could not run ({command[0]}): {error}") from error
    if done.returncode not in (0, 1):
        raise WorkspaceError(
            f"complexity check could not run: ruff exited {done.returncode}: {done.stderr.strip()[-300:]}"
        )
    try:
        loaded = json.loads(done.stdout)
    except ValueError as error:
        raise WorkspaceError(f"complexity check output was not JSON: {done.stdout[:200]!r}") from error
    if not isinstance(loaded, list):
        raise WorkspaceError("complexity check output was not JSON list")
    return [item for item in loaded if isinstance(item, dict)]


def touched_complexity(repo: Path, changes: list[FileChange]) -> dict[str, Any]:
    """Complexity of the functions the change touches: `touched`, `max`, `over_limit` and any `failures`.

    A ruff C901 violation counts only when its function's extent holds a touched line (an added line, or the line
    before a deletion). Legacy violations elsewhere in the file are not the change's. A file that does not parse
    fails the check.
    """
    failures: list[str] = []
    extents: dict[str, list[Extent]] = {}
    touched = 0
    for change in changes:
        if change.status == "deleted" or change.binary or not change.path.endswith(".py"):
            continue
        try:
            source = (repo / change.path).read_text(encoding="utf-8")
        except OSError as error:
            failures.append(f"{change.path}: cannot be read: {error}")
            continue
        found = function_extents(source)
        if found is None:
            failures.append(f"{change.path}: does not parse, so its complexity cannot be checked")
            continue
        hit = [item for item in found if any(item.start <= line <= item.end for line in change.touched)]
        extents[change.path] = hit
        touched += len(hit)
    over: list[dict[str, Any]] = []
    if any(extents.values()):
        try:
            violations = _ruff_complexity(repo, [path for path, hit in extents.items() if hit])
        except WorkspaceError as error:
            return {"touched": touched, "max": None, "over_limit": [], "failures": [*failures, str(error)]}
        for item in violations:
            raw_location = item.get("location")
            location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
            filename = _report_key(repo, str(item.get("filename", "")))
            measure = COMPLEXITY_MESSAGE.search(str(item.get("message", "")))
            match = next((hit for hit in extents.get(filename, []) if hit.line == location.get("row")), None)
            if match is not None and measure is not None:
                over.append(
                    {"file": filename, "function": match.name, "line": match.line, "complexity": int(measure.group(1))}
                )
    return {
        "touched": touched,
        "max": max((item["complexity"] for item in over), default=None),
        "over_limit": sorted(over, key=lambda item: (item["file"], item["line"])),
        "failures": failures,
    }


# --- the gate run ----------------------------------------------------------------------------------------------

DIFF_COVERAGE_FLOOR = 90.0
TESTS_TIMEOUT = 3600
GATE_TIMEOUT = 900
OBSERVABILITY = "observability"
SCRATCH = ".gates-scratch"
FINDING = re.compile(
    r"head_tree=(?P<tree>[0-9a-f]{64}) run=(?P<run>gates-[a-z0-9-]+) record_sha256=(?P<record>[0-9a-f]{64})"
)


def commands_sha256(commands: list[dict[str, Any]]) -> str:
    """The identity of an approved command set: a run is stale once the set it ran differs."""
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode("utf-8")).hexdigest()


def _pytest_position(argv: list[str]) -> int:
    for position, item in enumerate(argv):
        if os.path.basename(item) in ("pytest", "py.test") or (
            position and argv[position - 1] == "-m" and item == "pytest"
        ):
            return position
    return 0


def tests_argv(argv: list[str], scratch: Path) -> list[str]:
    """The approved pytest argv plus the options the run needs: branch coverage to a JSON report and a junit file.

    `--cov=.` is added only when the argv has no `--cov` option (without one pytest-cov writes no report); an
    approved `--cov=<dir>` is kept. The options go before a `--` separator that follows the pytest token.
    """
    position = _pytest_position(argv)
    extra = [] if any(item == "--cov" or item.startswith("--cov=") for item in argv) else ["--cov=."]
    extra += ["--cov-branch", f"--cov-report=json:{scratch}/coverage.json", f"--junitxml={scratch}/junit.xml"]
    separator = next((index for index in range(position + 1, len(argv)) if argv[index] == "--"), len(argv))
    return [*argv[:separator], *extra, *argv[separator:]]


def _setup_problems(state: dict[str, Any], gates: dict[str, Any]) -> list[str]:
    """Why the recorded approval cannot be run or trusted: nothing approved, a kind missing, a source changed."""
    if not gates.get("base_commit"):
        return [
            "gates-not-approved: no approved gate commands; record them with `workflow <project-dir> gates` "
            "(operation approve, or waive) and the user's words"
        ]
    missing = missing_kinds(gates["commands"], gates["waiver"])
    if missing:
        return [
            f"gates-not-approved: no approved command or waiver for kind(s) {', '.join(missing)}; approve a "
            "command of each kind, or waive the kind with the user's words"
        ]
    repo = target_repository(state)
    if repo is None:
        return ["gates-no-repository: this project records no target repository to run the gates in"]
    for source in gates.get("sources", []):
        try:
            current = _file_sha256(repo / source["path"])
        except OSError:
            current = "missing"
        if current != source["sha256"]:
            return [
                f"gates-source-changed: {source['path']} changed since the commands were approved; "
                "discover the commands again and re-approve them"
            ]
    return []


def _next_run_number(project: Path, task_id: str) -> int:
    lower = re.escape(task_id.lower())
    numbers = [0]
    for path in (project / ".operations").glob(f"gates-{task_id.lower()}-*.json"):
        found = re.fullmatch(rf"gates-{lower}-(\d+)\.json", path.name)
        numbers.extend(int(found.group(1)) for _ in [0] if found)
    notes = project / "tasks" / f"{task_id}.md"
    if notes.is_file():
        numbers.extend(
            int(item)
            for item in re.findall(rf"research-entry: gates-{lower}-(\d+) -->", notes.read_text(encoding="utf-8"))
        )
    return max(numbers) + 1


def _junit(path: Path) -> "tuple[int, int] | None":
    """(test cases, test files) of a junit report, or None when it is missing or unreadable."""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    cases = sum(int(suite.get("tests", "0")) for suite in root.iter("testsuite") if suite.get("tests", "0").isdigit())
    files = {case.get("file") or str(case.get("classname", "")).rsplit(".", 1)[0] for case in root.iter("testcase")}
    return cases, len(files)


def _gate_outcome(command: dict[str, Any], attempt: "dict[str, Any] | None") -> "tuple[bool, str]":
    if attempt is None:
        return (
            False,
            f"gate-not-run: {command['id']} ({command['kind']}) did not run; an earlier gate stopped the batch",
        )
    if attempt.get("status") != "recorded":
        return (
            False,
            f"gate-failed: {command['id']} ({command['kind']}) {attempt.get('status')}: {attempt.get('error', '')}",
        )
    code = attempt["result"]["exit_code"]
    if code != 0:
        return False, f"gate-failed: {command['id']} ({command['kind']}) exited with exit {code}"
    return True, ""


def _record_failures(scan: list[dict[str, Any]]) -> list[str]:
    return [
        f"{item['code']}: {item['file']}:{item['line']} {item['detail']}"
        if item["line"]
        else f"{item['code']}: {item['file']} {item['detail']}"
        for item in scan
    ]


def _measure_tests(
    repo: Path, commands: list[dict[str, Any]], outcomes: list[bool], scratch: Path, changes: list[FileChange]
) -> "tuple[dict[str, int], int, int, str | None, list[str]]":
    """Test counts and diff coverage from the reports the tests commands wrote; the failures they imply."""
    failures: list[str] = []
    cases = files = covered = total = 0
    note: "str | None" = None
    indexes = [index for index, command in enumerate(commands) if command["kind"] == "tests"]
    if not indexes:
        return {"files": 0, "cases": 0}, 0, 0, "waived", failures
    for position, index in enumerate(indexes):
        command = commands[index]
        if not outcomes[index]:
            continue
        counts = _junit(scratch / command["id"] / "junit.xml")
        if counts is None:
            failures.append(f"gate-failed: tests {command['id']} wrote no junit report, so no test case was counted")
        else:
            cases += counts[0]
            files += counts[1]
            if counts[0] == 0:
                failures.append(f"gate-failed: tests {command['id']} ran 0 test cases")
        if position:
            continue
        try:
            report = json.loads((scratch / command["id"] / "coverage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failures.append(f"coverage-format: tests {command['id']} wrote no readable coverage report")
            continue
        measured = diff_coverage(repo, changes, report)
        covered, total = measured["covered"], measured["total"]
        failures.extend(measured["failures"])
        if not measured["failures"] and total and 100.0 * covered / total < DIFF_COVERAGE_FLOOR:
            failures.append(
                f"gate-failed: diff coverage {100.0 * covered / total:.1f}% ({covered} of {total}) is below "
                f"{DIFF_COVERAGE_FLOOR:g}%"
            )
    return {"files": files, "cases": cases}, covered, total, note, failures


def _run_locked(
    project: Path, state: dict[str, Any], gates: dict[str, Any], task_id: str, repo: Path
) -> dict[str, Any]:
    number = _next_run_number(project, task_id)
    run = f"gates-{task_id.lower()}-{number}"
    scratch = project / SCRATCH / run
    scratch.mkdir(parents=True)
    commands: list[dict[str, Any]] = gates["commands"]
    try:
        attempts: list[dict[str, Any]] = []
        if commands:
            checks = []
            for command in commands:
                where = scratch / command["id"]
                where.mkdir()
                tests = command["kind"] == "tests"
                checks.append(
                    {
                        "task": task_id,
                        "cwd": str(repo),
                        "timeout": TESTS_TIMEOUT if tests else GATE_TIMEOUT,
                        "argv": tests_argv(command["argv"], where) if tests else command["argv"],
                    }
                )
            attempts = verify(project, {"id": run, "checks": checks, "continue_on_failure": True})["attempts"]
        else:
            atomic_write_json(
                project / ".operations" / f"{run}.json", {"kind": "scan", "id": run, "complete": True, "attempts": []}
            )
        outcomes: list[bool] = []
        failures: list[str] = []
        for index, command in enumerate(commands):
            passed, message = _gate_outcome(command, attempts[index] if index < len(attempts) else None)
            outcomes.append(passed)
            if message:
                failures.append(message)
        changes = changed_files(repo, gates["base_commit"])
        tests, covered, total, note, test_failures = _measure_tests(repo, commands, outcomes, scratch, changes)
        findings, info = scan_changes(repo, changes)
        findings += scan_config(repo, gates["base_commit"], changes)
        complexity = touched_complexity(repo, changes)
        failures += test_failures + _record_failures(findings) + complexity.pop("failures")
        failures += [
            f"gate-failed: complexity {item['complexity']} over {COMPLEXITY_LIMIT} in {item['function']} "
            f"({item['file']}:{item['line']})"
            for item in complexity["over_limit"]
        ]
        tree = head_tree(repo, gates["base_commit"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            (project / SCRATCH).rmdir()
        except OSError:
            pass
    record = observe.build_record(
        task=task_id,
        base_commit=gates["base_commit"],
        head_tree=tree,
        run=run,
        commands_sha256=commands_sha256(commands),
        files=[
            {
                "path": item.path,
                "added": len(item.added),
                "removed": item.removed,
                "language": language_of(item.path),
                "kind": classify_file(item.path),
            }
            for item in changes
        ],
        tests=tests,
        covered=covered,
        total=total,
        complexity=complexity,
        scan={"findings": findings, "info": info},
        failures=failures,
        note=note,
    )
    problems = observe.validate_record(record)
    if problems:
        raise WorkspaceError("gate record is malformed: " + "; ".join(problems))
    path = project / OBSERVABILITY / f"{task_id}.json"
    path.parent.mkdir(exist_ok=True)
    atomic_write_json(path, record)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    append_record(
        project,
        "finding",
        f"head_tree={tree} run={run} record_sha256={digest}",
        task_id=task_id,
        entry_id=run,
    )
    return {
        "task": task_id,
        "run": run,
        "passed": not failures,
        "failures": failures,
        "gates": [
            {"id": command["id"], "kind": command["kind"], "passed": outcomes[index]}
            for index, command in enumerate(commands)
        ],
        "evidence": [item["result"]["record_id"] for item in attempts if item.get("status") == "recorded"],
        "tests": tests,
        "diff_coverage": record["diff_coverage"],
        "complexity": complexity,
        "scan": record["scan"],
        "head_tree": tree,
        "record": f"{OBSERVABILITY}/{task_id}.json",
        "record_sha256": digest,
    }


def gates_run(project_dir: Path, request: dict[str, Any], *, lock_timeout: float = 5.0) -> dict[str, Any]:
    """`gate-run`: run the approved gates for a RUNNING task and record the result.

    It builds one ordinary `verify` request under a fresh id and holds only a per-task lock, never
    `.project.lock`, while the commands run. The record and the finding it leaves are what the finish gate reads.
    """
    project = project_dir.resolve()
    schema_input(request, "gate-run")
    task_id = string_input(request["task"], "task")
    state = _load_state(project)
    task = next((item for item in state["tasks"] if item.get("id") == task_id), None)
    if task is None:
        raise WorkspaceError(f"unknown task: {task_id}")
    gates = state.get("gates")
    if not isinstance(gates, dict) or not gates.get("enabled"):
        raise WorkspaceError(
            "gates-not-approved: this project has no enabled gates; "
            "approve commands with `workflow <project-dir> gates`"
        )
    if task["status"] != "RUNNING":
        raise WorkspaceError(f"task {task_id} is {task['status']}; start it with `task start` before gate-run")
    if GATE_NO_REPOSITORY in _waiver_of(gates).get("kinds", []):
        return {
            "task": task_id,
            "passed": True,
            "failures": [],
            "skipped": f"{GATE_NO_REPOSITORY} waiver: nothing to run",
        }
    problems = _setup_problems(state, gates)
    if problems:
        raise WorkspaceError("; ".join(problems))
    (project / ".operations").mkdir(exist_ok=True)
    with DirectoryLock(project / ".operations" / f"gates-{task_id.lower()}.lock", timeout=lock_timeout):
        return _run_locked(project, state, gates, task_id, _need_repository(state))


# --- the finish check -------------------------------------------------------------------------------------------

REMEDY = "start the task, run gate-run, then finish"


def _latest_finding(project: Path, task_id: str) -> "tuple[int, re.Match[str] | None]":
    """The newest `gates-<task>-<n>` finding in the task notes and its parsed one-line body; (0, None) when none."""
    path = project / "tasks" / f"{task_id}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0, None
    marker = re.compile(rf"<!-- research-entry: gates-{re.escape(task_id.lower())}-(\d+) -->")
    found = list(marker.finditer(text))
    if not found:
        return 0, None
    last = max(found, key=lambda item: int(item.group(1)))
    end = min((item.start() for item in found if item.start() > last.start()), default=len(text))
    return int(last.group(1)), FINDING.search(text[last.end() : end])


def _run_problems(project: Path, task_id: str, gates: dict[str, Any], repo: Path) -> list[str]:
    """Why the newest recorded run of `task_id` does not satisfy the gates; empty when it does."""
    number, line = _latest_finding(project, task_id)
    if number == 0:
        return [f"gate-not-run: no gate run is recorded for {task_id}; {REMEDY}"]
    if line is None:
        return [f"gate-not-run: the finding gates-{task_id.lower()}-{number} cannot be read; {REMEDY}"]
    run = line.group("run")
    path = project / OBSERVABILITY / f"{task_id}.json"
    try:
        data = path.read_bytes()
    except OSError:
        return [f"gate-not-run: the record {OBSERVABILITY}/{task_id}.json is missing; {REMEDY}"]
    if hashlib.sha256(data).hexdigest() != line.group("record"):
        return [f"gate-failed: the record changed since the run {run}; {REMEDY}"]
    try:
        record = json.loads(data)
    except ValueError:
        record = None
    malformed = observe.validate_record(record)
    if malformed or not isinstance(record, dict):
        return [f"gate-failed: the record is malformed ({malformed[0]}); {REMEDY}"]
    if record["run"] != run or record["task"] != task_id:
        return [f"gates-stale: the record belongs to run {record['run']}, not {run}; {REMEDY}"]
    if record["commands_sha256"] != commands_sha256(gates["commands"]) or record["base_commit"] != gates["base_commit"]:
        return [f"gates-stale: the approved commands or the base changed since run {run}; {REMEDY}"]
    problems = _journal_problems(project, run, gates["commands"])
    if problems:
        return problems
    if record["failures"]:
        return list(record["failures"])
    if head_tree(repo, gates["base_commit"]) != line.group("tree") or record["head_tree"] != line.group("tree"):
        return [f"gates-stale: the tree changed since run {run}; {REMEDY}"]
    return []


def _journal_problems(project: Path, run: str, commands: list[dict[str, Any]]) -> list[str]:
    path = project / ".operations" / f"{run}.json"
    if not path.is_file():
        return [f"gate-not-run: the run journal {run} is missing; {REMEDY}"]
    try:
        journal = load_json(path)
    except (OSError, WorkspaceError, ValueError):
        return [f"gate-failed: the run journal {run} cannot be read; {REMEDY}"]
    attempts = journal.get("attempts")
    if (
        not isinstance(attempts, list)
        or journal.get("kind") not in ("checks", "scan")
        or len(attempts) != len(commands)
    ):
        count = len(attempts) if isinstance(attempts, list) else 0
        return [
            f"gate-failed: the run journal holds {count} attempt(s) for {len(commands)} approved command(s); {REMEDY}"
        ]
    messages = []
    for command, attempt in zip(commands, attempts, strict=True):
        passed, message = _gate_outcome(command, attempt if isinstance(attempt, dict) and "status" in attempt else None)
        if not passed:
            messages.append(message)
    return messages


def _no_repository_waived(gates: dict[str, Any]) -> bool:
    """Whether a `no-repository` waiver stands in for every run of this project."""
    return GATE_NO_REPOSITORY in _waiver_of(gates).get("kinds", [])


def gate_findings(project_dir: Path, state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Why a gated project refuses `task` becoming DONE; empty when it may.

    `state` is the candidate. Only a code task of a project whose `gates.enabled` is true is judged, and a
    `no-repository` waiver stands in for a run. Otherwise the approval must be sound and the newest run must be
    recorded, pass, match the approved commands and describe the tree as it is now.
    """
    gates = state.get("gates")
    if not isinstance(gates, dict) or gates.get("enabled") is not True or not is_code_task(task):
        return []
    if _no_repository_waived(gates):
        return []
    problems = _setup_problems(state, gates)
    if problems:
        return problems
    repo = target_repository(state)
    assert repo is not None  # _setup_problems refused a project without one
    try:
        return _run_problems(project_dir.resolve(), task["id"], gates, repo)
    except WorkspaceError as error:
        return [f"gates-stale: the tree cannot be read ({error}); {REMEDY}"]


# --- the validation warnings ------------------------------------------------------------------------------------


def _finished_before_approval(gates: dict[str, Any], task: dict[str, Any]) -> bool:
    """Whether `task` finished before the commands were approved; unreadable stamps never excuse a task."""
    approval = gates.get("approval")
    if not isinstance(approval, dict):
        return False
    try:
        approved = datetime.fromisoformat(str(approval["approved_at"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(task["finished_at"]).replace("Z", "+00:00"))
        return finished < approved
    except (KeyError, ValueError):
        return False


def _record_of(project: Path, task_id: str) -> "tuple[dict[str, Any] | None, str]":
    """The sound record of `task_id` and an empty reason, or no record and why it cannot be trusted.

    Only the record's own soundness is judged: its hash against the newest finding, its shape, and whose it is.
    Whether the tree still matches is the finish gate's question, not a later validation's.
    """
    number, line = _latest_finding(project, task_id)
    name = f"{OBSERVABILITY}/{task_id}.json"
    if number == 0:
        return None, "no gate run is recorded"
    if line is None:
        return None, f"the finding gates-{task_id.lower()}-{number} cannot be read"
    try:
        data = (project / name).read_bytes()
    except OSError:
        return None, f"the record {name} is missing"
    if hashlib.sha256(data).hexdigest() != line.group("record"):
        return None, f"the record {name} changed since run {line.group('run')}"
    try:
        record = json.loads(data)
    except ValueError:
        record = None
    malformed = observe.validate_record(record)
    if malformed or not isinstance(record, dict):
        return None, f"the record {name} is malformed ({malformed[0] if malformed else 'not an object'})"
    if record["task"] != task_id or record["run"] != line.group("run"):
        return None, f"the record {name} belongs to run {record['run']} of {record['task']}"
    return record, ""


def _unsupported(record: dict[str, Any]) -> bool:
    """Whether the record names executable-looking files and none of them is Python."""
    files = record["files"]
    return not any(item["language"] == "python" for item in files) and any(
        item["kind"] in ("source", "other") for item in files
    )


def record_warnings(project_dir: Path, state: dict[str, Any]) -> list[str]:
    """Warnings for the DONE code tasks of a gated project that lack a sound observability record.

    A project without an enabled `gates` field, a task that is not a DONE code task, a project with the
    `no-repository` waiver and a task finished before the commands were approved are never judged. One warning
    names every record of a target with no Python file: the language is unsupported, not a fault per file.
    """
    gates = state.get("gates")
    tasks = state.get("tasks")
    if not isinstance(gates, dict) or gates.get("enabled") is not True or not isinstance(tasks, list):
        return []
    if _no_repository_waived(gates):
        return []
    project = project_dir.resolve()
    warnings: list[str] = []
    unsupported: list[str] = []
    for task in tasks:
        if not (isinstance(task, dict) and task.get("status") == "DONE" and is_code_task(task)):
            continue
        if _finished_before_approval(gates, task):
            continue
        record, reason = _record_of(project, task["id"])
        if record is None:
            warnings.append(
                f"task {task['id']}: gated project has a DONE code task with no valid observability record: "
                f"{reason}; to fix: {REMEDY}"
            )
        elif _unsupported(record):
            unsupported.append(task["id"])
    if unsupported:
        warnings.append(
            f"unsupported language: the observability record of {', '.join(unsupported)} lists no Python file; "
            "the gates and the record cover Python only, so coverage and complexity say nothing here"
        )
    return warnings
