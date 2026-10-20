"""Versions held by other work on the same repository: the check behind a new or moved base.

A base is occupied when it is at or below the newest version released on main, or when any other
in-flight work holds exactly that version. Four sources are read, all from what the local checkout
can show and none from a Git host:

- main: the version files at `origin/main` (or `origin/HEAD`) and the version tags;
- workspace: other live projects whose recorded worktree is for the same repository and has a
  `version_line` that is not merged;
- remote: the version files on every other `origin/*` branch;
- local: the version files in every other worktree and on every other local branch.

The agent runs `git fetch --prune` with the user's words first; remote refs older than a day only
produce a note. The tool excludes this project's own record, branch and worktree, and strips alpha
spellings, so `0.30.0a3` and `0.30.0-alpha.3` both read as `0.30.0`.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from workspace_context import _git
from workspace_lib import WorkspaceError, load_json
from workspace_similar import LIVE_STATUSES

PEP440_VERSION = re.compile(r"""^\s*(?:__version__|version)\s*=\s*["']v?(\d+\.\d+\.\d+)(?:a\d+)?["']""", re.MULTILINE)
SEMVER_VERSION = re.compile(r'"version"\s*:\s*"(\d+\.\d+\.\d+)(?:-alpha\.\d+)?"')
TAG_VERSION = re.compile(r"^v?(\d+\.\d+\.\d+)$")
PLAIN_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
DEFAULT_REFS = ("origin/main", "origin/master")
STALE_REFS_SECONDS = 24 * 3600


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _holding(source: str, ref: str, version: str) -> dict[str, str]:
    return {"source": source, "ref": ref, "version": version}


def _query(root: Path, *arguments: str) -> str | None:
    """One read-only Git query; None when Git refused. A Git that cannot run at all is an error."""
    try:
        return _git(root, *arguments)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"Git could not be consulted ({error}); retry when it is available") from error


def versions_in(text: str, style: str) -> list[str]:
    """The plain versions a version file states, whichever alpha spelling it carries."""
    return (PEP440_VERSION if style == "pep440" else SEMVER_VERSION).findall(text)


def _default_ref(root: Path) -> str | None:
    head = _query(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    for ref in ([head] if head else []) + list(DEFAULT_REFS):
        if _query(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{ref}"):
            return ref
    return None


def _from_ref(root: Path, ref: str, files: list[dict[str, str]]) -> list[str]:
    found: list[str] = []
    for item in files:
        text = _query(root, "show", f"{ref}:{item['path']}")
        if text:
            found += versions_in(text, item["style"])
    return found


def _main(root: Path, files: list[dict[str, str]], has_origin: bool) -> tuple[list[dict[str, str]], str | None]:
    """Versions released on main: the default branch of origin and the version tags."""
    holdings: list[dict[str, str]] = []
    default = None
    if has_origin:
        default = _default_ref(root)
        if default is None:
            raise WorkspaceError(
                "origin has no main branch ref (origin/HEAD, origin/main or origin/master), so the released "
                "versions cannot be read; run `git fetch origin` with the user's words, then retry"
            )
        holdings += [_holding("main", default, version) for version in _from_ref(root, default, files)]
    for tag in (_query(root, "tag", "--list") or "").splitlines():
        found = TAG_VERSION.match(tag.strip())
        if found:
            holdings.append(_holding("main", f"tag {tag.strip()}", found.group(1)))
    return holdings, default


def _workspace(workspace_root: Path, project: str, repository: Path) -> list[dict[str, str]]:
    """Bases recorded by other live projects for the same repository, not yet merged."""
    holdings: list[dict[str, str]] = []
    try:
        directories = sorted(workspace_root.iterdir())
    except OSError:
        return holdings
    for directory in directories:
        if directory.name == project or directory.name.startswith(".") or not (directory / "project.json").is_file():
            continue
        try:
            state = load_json(directory / "project.json")
        except WorkspaceError:
            continue
        if state.get("status") not in LIVE_STATUSES or not isinstance(state.get("worktrees"), list):
            continue
        for item in state["worktrees"]:
            line = item.get("version_line") if isinstance(item, dict) else None
            if not isinstance(line, dict) or not isinstance(line.get("base"), str):
                continue
            merge = item.get("merge")
            if isinstance(merge, dict) and merge.get("state") == "merged":
                continue
            if not PLAIN_VERSION.match(line["base"]) or not isinstance(item.get("repository"), str):
                continue
            if Path(item["repository"]).resolve() == repository.resolve():
                holdings.append(_holding("workspace", f"project {directory.name}", line["base"]))
    return holdings


def _branches(
    root: Path, files: list[dict[str, str]], own_branch: str, default: str | None
) -> list[dict[str, str]]:
    """Versions on other origin branches, then on other local branches that no origin branch repeats."""
    holdings: list[dict[str, str]] = []
    seen: set[str] = set()
    default_name = default.split("/", 1)[1] if default else None
    for source, prefix in (("remote", "refs/remotes/origin"), ("local", "refs/heads")):
        listing = _query(root, "for-each-ref", "--format=%(objectname) %(refname)", prefix) or ""
        for line in listing.splitlines():
            sha, full = line.split(" ", 1)
            name = full[len(prefix) + 1:]
            if name in ("HEAD", own_branch, default_name) or sha in seen:
                continue
            seen.add(sha)
            ref = f"origin/{name}" if source == "remote" else name
            holdings += [_holding(source, ref, version) for version in _from_ref(root, full, files)]
    return holdings


def _worktrees(root: Path, files: list[dict[str, str]], own: Path) -> list[dict[str, str]]:
    holdings: list[dict[str, str]] = []
    listing = _query(root, "worktree", "list", "--porcelain") or ""
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        if path.resolve() == own.resolve():
            continue
        for item in files:
            try:
                text = (path / item["path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            holdings += [_holding("local", f"worktree {path}", version) for version in versions_in(text, item["style"])]
    return holdings


def _stale_note(root: Path) -> str | None:
    """A note when origin refs look old. FETCH_HEAD is per worktree and the refs are shared, so the newest
    FETCH_HEAD of this worktree and of the main checkout counts."""
    newest: float | None = None
    lookups = ((("rev-parse", "--git-path", "FETCH_HEAD"), ""), (("rev-parse", "--git-common-dir"), "FETCH_HEAD"))
    for arguments, name in lookups:
        found = _query(root, *arguments)
        if not found:
            continue
        path = root / found / name
        if path.is_file():
            newest = max(newest or 0.0, os.path.getmtime(path))
    if newest is None:
        return None
    age = time.time() - newest
    if age <= STALE_REFS_SECONDS:
        return None
    return (
        f"origin refs were last fetched {int(age // 3600)} hours ago (FETCH_HEAD); run `git fetch --prune` with "
        "the user's words and check again before relying on this result"
    )


def check_base(
    root: Path, entry: dict[str, Any], files: list[dict[str, str]], base: str, *, workspace_root: Path, project: str,
) -> list[str]:
    """Refuse a base that another source occupies, naming each occupier; return notes otherwise.

    `entry` is the recorded worktree of this project: its path, branch and repository are excluded.
    """
    if _query(root, "rev-parse", "--git-dir") is None:
        raise WorkspaceError("Git could not be consulted for the worktree; retry when it is available")
    remotes = (_query(root, "remote") or "").split()
    has_origin = "origin" in remotes
    notes: list[str] = []
    if not has_origin:
        notes.append("no origin remote: the origin/main and remote-branch sources were skipped")
    released, default = _main(root, files, has_origin)
    others = _workspace(workspace_root, project, Path(entry["repository"]))
    others += _branches(root, files, entry["branch"], default)
    others += _worktrees(root, files, Path(entry["path"]))
    newest = max((_tuple(item["version"]) for item in released), default=None)
    occupiers: list[str] = []
    if newest is not None and _tuple(base) <= newest:
        newest_ref = max(released, key=lambda item: _tuple(item["version"]))
        occupiers.append(
            f"main: {newest_ref['ref']} releases {newest_ref['version']}; a base must be above every released version"
        )
    for item in others:
        line = f"{item['source']}: {item['ref']} carries {item['version']}"
        if item["version"] == base and line not in occupiers:  # a ref states its version in every version file
            occupiers.append(line)
    if occupiers:
        every = [_tuple(item["version"]) for item in [*released, *others]] + [_tuple(base)]
        major, minor, patch = max(every)
        raise WorkspaceError(
            f"base {base} is occupied:\n" + "\n".join(f"  {line}" for line in occupiers)
            + f"\nThe next free base is {major}.{minor}.{patch + 1}, the patch above the highest version seen "
            f"({major}.{minor}.{patch}). Ask the user which base to use."
        )
    if has_origin:
        stale = _stale_note(root)
        if stale:
            notes.append(stale)
    return notes
