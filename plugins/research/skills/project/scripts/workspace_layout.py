"""`research-project migrate --layout WORKSPACE_DIR [--apply]`: move a workspace to the records layout.

Seven steps, each checking real state first and skipping when already done, so a rerun after an
interruption resumes where it stopped and a second apply changes nothing. There is no journal. The
tool stages its changes (S6) but never commits; the user makes the commit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from workspace_doctor import (
    LFS_KEYS,
    VIEW_NAMES,
    Finding,
    attributes_line,
    git_toplevel,
    ignore_lines,
    run_doctor,
    run_git,
    workspace_prefix,
)
from workspace_lib import WORKSPACE_MARKER, WorkspaceError, atomic_write_text

MARKER_CONTENT = '{"layout":1}\n'


@dataclass(frozen=True)
class Context:
    root: Path
    toplevel: Path
    prefix: str

    @property
    def marker(self) -> Path:
        return self.root / WORKSPACE_MARKER

    @property
    def gitignore(self) -> Path:
        return self.toplevel / ".gitignore"

    @property
    def gitattributes(self) -> Path:
        return self.toplevel / ".gitattributes"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        raise WorkspaceError(f"cannot read {path}: {error}") from error


def _lines(path: Path) -> set[str]:
    return {line.strip() for line in _read(path).splitlines()}


def _append_lines(path: Path, wanted: list[str]) -> None:
    """Append the absent `wanted` lines, preserving every existing byte."""
    text = _read(path)
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in wanted if line not in present]
    if missing:
        separator = "" if not text or text.endswith("\n") else "\n"
        atomic_write_text(path, text + separator + "\n".join(missing) + "\n")


def _lfs_configured(context: Context) -> bool:
    return all(run_git(context.root, "config", "--get", key, check=False).stdout.strip() for key in LFS_KEYS)


def _tracked_views(context: Context) -> list[str]:
    output = run_git(context.root, "ls-files", "-z", "--", *VIEW_NAMES).stdout
    return [item.decode("utf-8") for item in output.split(b"\0") if item]


def _staged(context: Context) -> bool:
    paths = [str(context.marker), str(context.gitignore), str(context.gitattributes)]
    if not all(path.is_file() for path in map(Path, paths)):
        return False
    for path in paths:
        if not run_git(context.toplevel, "ls-files", "-s", "--", path).stdout.strip():
            return False
    return not run_git(context.toplevel, "diff", "--name-only", "--", *paths).stdout.strip()


def _stage(context: Context) -> None:
    run_git(context.toplevel, "add", "--", str(context.marker), str(context.gitignore), str(context.gitattributes))


@dataclass(frozen=True)
class Step:
    id: str
    description: str
    done: Callable[[Context], bool]
    apply: Callable[[Context], object]


STEPS = (
    Step(
        "S1",
        "git lfs install --local",
        _lfs_configured,
        lambda context: run_git(context.root, "lfs", "install", "--local"),
    ),
    Step(
        "S2",
        f"write {WORKSPACE_MARKER} containing {MARKER_CONTENT.strip()}",
        lambda context: context.marker.is_file(),
        lambda context: atomic_write_text(context.marker, MARKER_CONTENT),
    ),
    Step(
        "S3",
        "add the anchored view and temporary-file lines to .gitignore",
        lambda context: set(ignore_lines(context.prefix)) <= _lines(context.gitignore),
        lambda context: _append_lines(context.gitignore, ignore_lines(context.prefix)),
    ),
    Step(
        "S4",
        "add the artifacts/large LFS line to .gitattributes",
        lambda context: attributes_line(context.prefix) in _lines(context.gitattributes),
        lambda context: _append_lines(context.gitattributes, [attributes_line(context.prefix)]),
    ),
    Step(
        "S5",
        "git rm --cached the derived views that are tracked",
        lambda context: not _tracked_views(context),
        lambda context: run_git(context.root, "rm", "--cached", "-q", "--", *_tracked_views(context)),
    ),
    Step(
        "S6",
        "git add the marker, .gitignore and .gitattributes (no commit)",
        _staged,
        _stage,
    ),
)


def _after_step(step_id: str) -> None:
    """Called after each applied step; the seam interruption tests use."""


def migrate_layout(workspace: Path, *, apply: bool) -> tuple[list[str], list[Finding]]:
    """Plan or apply the layout migration. Returns report lines and, after an apply, doctor findings."""
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError(f"workspace directory does not exist: {root}")
    toplevel = git_toplevel(root)
    if toplevel is None:
        raise WorkspaceError(f"{root} is not inside a git repository; the layout needs one")
    context = Context(root, toplevel, workspace_prefix(root, toplevel))
    lines = []
    for step in STEPS:
        if step.done(context):
            lines.append(f"{step.id} skip (already done): {step.description}")
        elif apply:
            step.apply(context)
            lines.append(f"{step.id} applied: {step.description}")
            _after_step(step.id)
        else:
            lines.append(f"{step.id} would apply: {step.description}")
    if not apply:
        lines.append("S7 would run doctor; rerun with --apply to perform the steps above (nothing is committed)")
        return lines, []
    findings = run_doctor(root)
    lines.append(f"S7 doctor: {len(findings)} finding(s); review and commit the staged files yourself")
    return lines, findings
