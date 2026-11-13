"""`research-project doctor`: read-only checks of the workspace records layout (rules R1-R6).

The rules are listed in `references/records-layout.md`. Everything here only reads: git is called for
queries, never for a write, and no file is created or changed. A failure of git itself, or of the
directory, is a `WorkspaceError`; the CLI maps it to exit code 2.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from workspace_lib import WORKSPACE_MARKER, WorkspaceError, is_canonical_project_id

MIB = 1024 * 1024
HARD_LIMIT = 4 * MIB
SOFT_LIMIT = MIB
VIEW_NAMES = ("INDEX.md", "MEMORY.md", "POSTMORTEMS.md", "RETRO.md")
LFS_KEYS = ("filter.lfs.clean", "filter.lfs.smudge", "filter.lfs.process")
LARGE_TIER = "artifacts/large"
LFS_INSTALL = "git lfs install --local"


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str
    message: str
    fix: str

    def line(self) -> str:
        return f"{self.severity} {self.rule} {self.path}: {self.message}; fix: {self.fix}"


def anchored(prefix: str, name: str) -> str:
    """A root-anchored gitignore or gitattributes path; the prefix is empty at the repository root."""
    return f"/{prefix}/{name}" if prefix else f"/{name}"


def ignore_lines(prefix: str) -> list[str]:
    """The `.gitignore` lines of the layout, in the order migration appends them."""
    return [anchored(prefix, name) for name in VIEW_NAMES] + [
        anchored(prefix, ".*.tmp"),
        anchored(prefix, "*/.*.tmp"),
    ]


def attributes_line(prefix: str) -> str:
    base = f"{prefix}/" if prefix else ""
    return f"{base}*/{LARGE_TIER}/** filter=lfs diff=lfs merge=lfs -text"


def run_git(
    root: Path, *args: str, check: bool = True, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run `git -C root ...`. Raises `WorkspaceError` when git cannot run, or fails with `check`."""
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=stdin, capture_output=True, check=False, env=environment
        )
    except OSError as error:
        raise WorkspaceError(f"cannot run git: {error}") from error
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise WorkspaceError(f"git {' '.join(args)} failed in {root}: {detail}")
    return result


def git_toplevel(root: Path) -> Path | None:
    """The repository root holding `root`, or None when `root` is not inside a git repository."""
    result = run_git(root, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        return None
    return Path(result.stdout.decode("utf-8").strip()).resolve()


def workspace_prefix(root: Path, toplevel: Path) -> str:
    relative = root.resolve().relative_to(toplevel).as_posix()
    return "" if relative == "." else relative


def _names(output: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in output.split(b"\0") if item]


def _blob_sizes(root: Path, blobs: list[str]) -> dict[str, int]:
    if not blobs:
        return {}
    result = run_git(root, "cat-file", "--batch-check", stdin=("\n".join(blobs) + "\n").encode("ascii"))
    sizes: dict[str, int] = {}
    for line in result.stdout.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    return sizes


def _new_staged_files(root: Path) -> list[tuple[str, int]]:
    """(path, size) of every blob in the index that is not the blob in HEAD, paths below `root`."""
    command = ("diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames", "--relative", "--", ".")
    raw = run_git(root, *command).stdout
    fields = _names(raw)
    entries: dict[str, str] = {}
    for header, path in zip(fields[::2], fields[1::2], strict=True):
        _, new_mode, _, new_sha, status = header.split()
        if status == "D" or new_mode == "160000":
            continue
        entries[path] = new_sha
    sizes = _blob_sizes(root, sorted(set(entries.values())))
    return [(path, sizes[sha]) for path, sha in sorted(entries.items()) if sha in sizes]


def _untracked_files(root: Path, *, ignored: bool = False, pathspec: str = ".") -> list[str]:
    flags = ["-o", "-i"] if ignored else ["-o"]
    return _names(run_git(root, "ls-files", *flags, "--exclude-standard", "-z", "--", pathspec).stdout)


def _file_size(root: Path, relative: str) -> int | None:
    try:
        return (root / relative).stat().st_size
    except OSError:
        return None


def _in_large_tier(relative: str) -> bool:
    return fnmatch.fnmatchcase(relative, f"*/{LARGE_TIER}/*")


def _size_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    candidates = _new_staged_files(root)
    for relative in _untracked_files(root):
        size = _file_size(root, relative)
        if size is not None:
            candidates.append((relative, size))
    for relative, size in candidates:
        mib = size / MIB
        if size > HARD_LIMIT:
            findings.append(
                Finding(
                    "error",
                    "R1",
                    relative,
                    f"new non-LFS file of {mib:.1f} MiB exceeds the 4 MiB push limit",
                    "move it to <project>/artifacts/large/ (LFS), or list it in artifacts/MANIFEST.json and "
                    "git-ignore it, or delete it",
                )
            )
        elif size >= SOFT_LIMIT and not _in_large_tier(relative):
            findings.append(
                Finding(
                    "warning",
                    "R2",
                    relative,
                    f"new file of {mib:.1f} MiB sits outside artifacts/large/",
                    "move it to <project>/artifacts/large/ if irreplaceable, else ignore it and list it in "
                    "artifacts/MANIFEST.json",
                )
            )
    return findings


def _lfs_findings(root: Path) -> list[Finding]:
    unset = [
        key for key in LFS_KEYS if run_git(root, "config", "--get", key, check=False).stdout.strip() == b""
    ]
    problems = []
    if unset:
        problems.append(f"{', '.join(unset)} not set")
    if run_git(root, "lfs", "version", check=False).returncode != 0:
        problems.append("`git lfs version` fails (git-lfs is not installed or not on PATH)")
    if not problems:
        return []
    return [
        Finding(
            "error",
            "R3",
            ".",
            "Git LFS is not configured: " + "; ".join(problems),
            f"install git-lfs, then run `{LFS_INSTALL}` in the repository",
        )
    ]


def _temporary_findings(root: Path) -> list[Finding]:
    directories = [root]
    try:
        directories += sorted(
            child for child in root.iterdir() if child.is_dir() and is_canonical_project_id(child.name)
        )
        found = [
            path
            for directory in directories
            for path in sorted(directory.iterdir())
            if path.is_file() and fnmatch.fnmatchcase(path.name, ".*.*.tmp")
        ]
    except OSError as error:
        raise WorkspaceError(f"cannot read {root}: {error}") from error
    return [
        Finding(
            "warning",
            "R4",
            path.relative_to(root).as_posix(),
            "leftover temporary file from an interrupted write",
            "delete it; research-project rebuild-index sweeps old view temporaries",
        )
        for path in found
    ]


def _read_lines(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeDecodeError):
        return []


def _layout_findings(root: Path, toplevel: Path, prefix: str) -> list[Finding]:
    findings: list[Finding] = []

    def add(path: str, message: str, fix: str) -> None:
        findings.append(Finding("error", "R5", path, message, fix))

    migrate = f"research-project migrate --layout {root} --apply"
    if run_git(root, "ls-files", "-s", "--", WORKSPACE_MARKER).stdout.strip() == b"":
        add(WORKSPACE_MARKER, "workspace marker is not tracked or staged", f"run `{migrate}` and commit")
    for name in VIEW_NAMES:
        if run_git(root, "ls-files", "--", name).stdout.strip():
            add(name, "derived view is tracked by git", f"run `{migrate}` (git rm --cached) and commit")
        if run_git(root, "check-ignore", "-q", "--no-index", "--", name, check=False).returncode != 0:
            add(name, "derived view is not git-ignored", f"run `{migrate}`")
    present = set(_read_lines(toplevel / ".gitignore"))
    missing = [line for line in ignore_lines(prefix) if line not in present]
    if missing:
        add(".gitignore", f"missing lines: {', '.join(missing)}", f"run `{migrate}`")
    if attributes_line(prefix) not in _read_lines(toplevel / ".gitattributes"):
        add(".gitattributes", f"missing line: {attributes_line(prefix)}", f"run `{migrate}`")
    return findings


def _manifest_paths(project: Path) -> set[str]:
    try:
        data = json.loads((project / "artifacts" / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    entries = data.get("files") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return set()
    return {
        PurePosixPath(item["path"]).as_posix()
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str) and "size" in item
    }


def _manifest_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        projects = sorted(
            child for child in root.iterdir() if child.is_dir() and is_canonical_project_id(child.name)
        )
    except OSError as error:
        raise WorkspaceError(f"cannot read {root}: {error}") from error
    for project in projects:
        artifacts = project.relative_to(root).as_posix() + "/artifacts"
        if not (project / "artifacts").is_dir():
            continue
        listed: set[str] | None = None
        for relative in _untracked_files(root, ignored=True, pathspec=artifacts):
            size = _file_size(root, relative)
            if size is None or size <= SOFT_LIMIT:
                continue
            if listed is None:
                listed = _manifest_paths(project)
            inside = PurePosixPath(relative).relative_to(artifacts).as_posix()
            if inside not in listed:
                findings.append(
                    Finding(
                        "warning",
                        "R6",
                        relative,
                        f"git-ignored file of {size / MIB:.1f} MiB is not listed in {artifacts}/MANIFEST.json",
                        'add {"path": "<path under artifacts/>", "size": <bytes>} to that manifest, or delete '
                        "the file; ignored files are lost on a re-clone",
                    )
                )
    return findings


def run_doctor(root: Path) -> list[Finding]:
    """All findings for the workspace at `root`, errors first. Read-only."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError(f"workspace root does not exist: {root}")
    findings = _temporary_findings(root)
    toplevel = git_toplevel(root)
    if toplevel is not None:
        findings += _size_findings(root)
        findings += _lfs_findings(root)
        findings += _layout_findings(root, toplevel, workspace_prefix(root, toplevel))
        findings += _manifest_findings(root)
    return sorted(findings, key=lambda item: (item.severity != "error", item.rule, item.path))
