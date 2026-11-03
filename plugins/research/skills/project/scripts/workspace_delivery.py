"""Delivery records of a repository worktree: the MR, the alpha line, the release and the merge.

The tools never call a Git host and never write to Git. They record what the agent reports and
check only what a local read can show: the host of the remote, the version strings inside the
recorded files, and the order of the steps. The gates turn on for projects created on or after
`DELIVERY_GATES_FROM`.
"""

from __future__ import annotations

import copy
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from workspace_context import _git
from workspace_fields import EXEMPTION_REPLACED_FIELDS, NESTED_INPUTS, field_errors
from workspace_journal import list_input, object_input, string_input
from workspace_lib import WorkspaceError, _is_timestamp, _non_empty_string, is_delivery_gated, needs_merge, now_iso
from workspace_occupancy import check_base, own_text

DELIVERY_OPERATIONS = {"pull_request", "alpha", "release", "merge"}
HOSTS = {"github", "gitlab"}
STYLES = {"pep440", "semver"}
METHODS = {"squash", "merge", "rebase"}
MERGE_STATES = {"merged", "declined"}
PLAIN_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PR_EXEMPTION_REASONS = {"no_remote", "local_only"}
# The full commit name the merge gate accepts: 40 hex digits, or 64 for a SHA-256 repository.
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAXIMUM_FILES = 10


def spelling(base: str, alpha: int, style: str) -> str:
    """The alpha version as a Python file (PEP 440) or a plugin manifest (SemVer) spells it."""
    return f"{base}a{alpha}" if style == "pep440" else f"{base}-alpha.{alpha}"


def _contains(text: str, version: str) -> bool:
    """Whether `version` stands alone in `text`: `0.24.0a1` does not match inside `0.24.0a10`."""
    return re.search(r"(?<![\w.-])" + re.escape(version) + r"(?![\w.-])", text) is not None


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceError(f"{field} must be a whole number of at least 1")
    return value


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _base_move(line: dict[str, Any], base: str, request: dict[str, Any], label: str) -> dict[str, Any] | None:
    """The recorded base move when `base` leaves the line, or None when it stays.

    A base moves only when the main branch released past it or another source occupies it; the agent
    asks the user first and says so with `base_moved: true` and a `reason`. An alpha record on a moved
    base restarts the alpha number at 1; the occupancy check refuses a moved base that is also taken.
    """
    moved = request.get("base_moved", False)
    if not isinstance(moved, bool):
        raise WorkspaceError("base_moved must be true or false")
    if base == line["base"]:
        if moved:
            raise WorkspaceError(f"base_moved needs a base different from the recorded {base}")
        return None
    if not moved:
        raise WorkspaceError(f"{label} {line['base']} for the whole project unless base_moved is true; found {base}")
    if _version_tuple(base) <= _version_tuple(line["base"]):
        raise WorkspaceError(f"a moved base must be greater than {line['base']}; found {base}")
    reason = string_input(request.get("reason"), "reason")
    return {"from": line["base"], "to": base, "reason": reason, "recorded_at": now_iso()}


def _read_version_file(root: Path, relative: str) -> str:
    """The text of a version file that states this project's version; a `uv.lock` reads at its own entry."""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkspaceError(f"version file path must be relative to the worktree with no '..': {relative}")
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents or not resolved.is_file():
        raise WorkspaceError(f"version file is missing in the worktree: {relative}")
    return own_text(resolved.read_text(encoding="utf-8", errors="replace"))


def _remote_hostname(root: Path) -> str:
    """The host part of the `origin` URL, for an https, ssh or scp-style remote."""
    url = _git(root, "remote", "get-url", "origin")
    if not url:
        raise WorkspaceError("the worktree has no `origin` remote; record an exemption with the user's words")
    if "://" in url:
        hostname = urlparse(url).hostname
    else:
        hostname = url.split("@")[-1].split(":")[0]
    if not hostname:
        raise WorkspaceError(f"cannot read a host from the origin remote: {url}")
    return hostname.lower()


def _pull_request(entry: dict[str, Any], request: dict[str, Any], root: Path, confirmation: dict[str, str]) -> None:
    if "pull_request" in entry or "pr_exemption" in entry:
        raise WorkspaceError("the worktree already has a pull_request or a pr_exemption")
    if "exempt" in request:
        exempt = request["exempt"]
        if exempt == "local_only":
            # The user's words are the guard: the tool does not test that the branch is never pushed.
            entry["pr_exemption"] = {"reason": "local_only", **confirmation}
            return
        if exempt != "no_remote":
            raise WorkspaceError("exempt must be no_remote or local_only")
        remotes = _git(root, "remote")
        if remotes is None:
            raise WorkspaceError("Git could not be consulted for the worktree; retry when it is available")
        if remotes.strip():
            raise WorkspaceError(f"the worktree has a remote ({remotes.split()[0]}); open a MR instead")
        entry["pr_exemption"] = {"reason": "no_remote", **confirmation}
        return
    host = string_input(request.get("host"), "host")
    if host not in HOSTS:
        raise WorkspaceError("host must be github or gitlab")
    url = string_input(request.get("url"), "url")
    number = _positive_integer(request.get("number"), "number")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != _remote_hostname(root):
        raise WorkspaceError(f"url must be an https URL on the host of the origin remote: {url}")
    path = "/pull/" if host == "github" else "/-/merge_requests/"
    if not re.search(re.escape(path) + str(number) + r"/?$", parsed.path):
        raise WorkspaceError(f"url must end with {path}{number} for host {host}: {url}")
    entry["pull_request"] = {"host": host, "url": url, "number": number, "recorded_at": now_iso()}


def _files(request: dict[str, Any], line: dict[str, Any] | None) -> list[dict[str, str]]:
    if "files" not in request:
        if line is None:
            raise WorkspaceError("files are required for the first alpha or release record")
        return line["files"]
    files = []
    for item in list_input(request["files"], MAXIMUM_FILES, "files"):
        item = object_input(item, {"path", "style"}, {"path", "style"}, "files item")
        if item["style"] not in STYLES:
            raise WorkspaceError("files style must be pep440 or semver")
        files.append({"path": string_input(item["path"], "files path"), "style": item["style"]})
    if not files:
        raise WorkspaceError("files must name at least one version file")
    if line is not None and files != line["files"]:
        raise WorkspaceError("the version files cannot change during the project")
    return files


def _alpha(
    entry: dict[str, Any], request: dict[str, Any], root: Path, confirmation: dict[str, str],
    workspace_root: Path, project: str,
) -> list[str]:
    """Record an alpha line or a version exemption; returns notes about the sources the check could not read."""
    if request.get("exempt") is True:
        if "version_line" in entry or "version_exemption" in entry:
            raise WorkspaceError("the worktree already has a version_line or a version_exemption")
        reason = string_input(request.get("reason"), "reason")
        entry["version_exemption"] = {"reason": reason, **confirmation}
        return []
    if "version_exemption" in entry:
        raise WorkspaceError("the worktree has a version_exemption; no alpha line applies")
    if entry.get("merge", {}).get("state") == "merged":
        raise WorkspaceError("the pull request is merged; the alpha line is closed")
    line = entry.get("version_line")
    base = string_input(request.get("base"), "base")
    if not PLAIN_VERSION.match(base):
        raise WorkspaceError("base must be a plain X.Y.Z version")
    move = None if line is None else _base_move(line, base, request, "the base stays")
    alpha = _positive_integer(request.get("alpha"), "alpha")
    if line is not None:
        # The base differs from the last record's when this record moves it, and also when a release moved
        # it earlier: either way the alpha line starts again at 1. A release-only line has no record yet.
        previous_base = line["history"][-1].get("base", line["base"]) if line["history"] else line["base"]
        if base != previous_base and alpha != 1:
            raise WorkspaceError(f"a moved base restarts the alpha number at 1: found {alpha}")
        if base == previous_base and line["alpha"] is not None and alpha <= line["alpha"]:
            raise WorkspaceError(
                f"the alpha number only goes up while the base stays: recorded {line['alpha']}, found {alpha}"
            )
    reason = string_input(request.get("reason"), "reason")
    files = _files(request, line)
    notes: list[str] = []
    if line is None or move is not None:
        notes = check_base(root, entry, files, base, workspace_root=workspace_root, project=project)
    for item in files:
        text = _read_version_file(root, item["path"])
        expected = spelling(base, alpha, item["style"])
        if not _contains(text, expected):
            raise WorkspaceError(f"version file {item['path']} does not contain {expected}")
    history = copy.deepcopy(line["history"]) if line is not None else []
    history.append({"alpha": alpha, "base": base, "recorded_at": now_iso(), "reason": reason})
    moves = copy.deepcopy(line.get("base_moves", [])) if line is not None else []
    if move is not None:
        moves.append(move)
    entry["version_line"] = {
        "files": files, "base": base, "alpha": alpha, "history": history, "release": None, "base_moves": moves,
    }
    return notes


# A check the caller runs before a release or a merge changes the entry: it raises to refuse.
Gate = Callable[[str, dict[str, Any]], None]


# The reason a version exemption recorded before 0.30.0 carries: that version kept no reason.
PRE_030_REASON = "recorded before 0.30.0 with no reason"


def _replace_exemption(entry: dict[str, Any], confirmation: dict[str, str], stamp: str) -> None:
    """Move the version exemption into `version_exemption_replaced`, beside the release confirmation."""
    old = entry.pop("version_exemption")
    entry["version_exemption_replaced"] = {
        "reason": old.get("reason", PRE_030_REASON),
        "source": old["source"],
        "response": old["response"],
        "replaced_at": stamp,
        "replaced_source": confirmation["source"],
        "replaced_response": confirmation["response"],
    }


def _release(
    entry: dict[str, Any], request: dict[str, Any], root: Path, workspace_root: Path, project: str,
    confirmation: dict[str, str], gate: Gate | None = None,
) -> list[str]:
    """Record the plain release version. A project that never recorded an alpha line starts its line here.

    `replaces_exemption` true turns a version exemption into the release and keeps the exemption's record.
    """
    replaces = request.get("replaces_exemption", False)
    if not isinstance(replaces, bool):
        raise WorkspaceError("replaces_exemption must be true or false")
    if "version_exemption" in entry and not replaces:
        raise WorkspaceError(
            "the worktree has a version_exemption; no release record applies without replaces_exemption true"
        )
    if replaces and "version_exemption" not in entry:
        raise WorkspaceError("replaces_exemption needs a version_exemption to replace; the worktree has none")
    line = entry.get("version_line")
    version = string_input(request.get("version"), "version")
    if not PLAIN_VERSION.match(version):
        raise WorkspaceError("the release version must be a plain X.Y.Z version")
    move = None if line is None else _base_move(line, version, request, "the release version must equal the base")
    files = _files(request, None) if line is None else line["files"]
    leftover = re.compile(re.escape(version) + r"(?:a\d+|-alpha\.\d+)")
    for item in files:
        text = _read_version_file(root, item["path"])
        if leftover.search(text):
            raise WorkspaceError(f"version file {item['path']} still carries an alpha version of {version}")
        if not _contains(text, version):
            raise WorkspaceError(f"version file {item['path']} does not contain {version}")
    # The base was checked when it was first recorded, but main may have released it since.
    notes = check_base(root, entry, files, version, workspace_root=workspace_root, project=project)
    if gate is not None:
        gate("release", entry)
    release = {"version": version, "recorded_at": now_iso()}
    if replaces:
        _replace_exemption(entry, confirmation, release["recorded_at"])
    if line is None:
        entry["version_line"] = {
            "files": files, "base": version, "alpha": None, "history": [], "release": release, "base_moves": [],
        }
        return notes
    if move is not None:
        line["base"] = version
        line["base_moves"] = [*line.get("base_moves", []), move]
    line["release"] = release
    return notes


def _merge(
    entry: dict[str, Any], request: dict[str, Any], confirmation: dict[str, str], gate: Gate | None = None
) -> None:
    if not needs_merge(entry):
        raise WorkspaceError(
            "no pull_request or local_only exemption is recorded; "
            "an exempt worktree with no_remote has nothing to merge"
        )
    state = request.get("merge_state")
    if state not in MERGE_STATES:
        raise WorkspaceError("merge_state must be merged or declined")
    recorded = entry.get("merge")
    if isinstance(recorded, dict) and recorded.get("state") == "merged":
        raise WorkspaceError("the merge is already recorded; a merge is final")
    merged_head = _merged_head(request, state)
    method = evidence = None
    if state == "merged":
        method = request.get("method")
        if method not in METHODS:
            raise WorkspaceError("a merged record needs method squash, merge or rebase")
        evidence = string_input(request.get("evidence"), "evidence")
        line = entry.get("version_line")
        if line is not None and line["release"] is None:
            raise WorkspaceError("record the release version before the merge")
        if gate is not None:
            gate("merge", entry)
    entry["merge"] = {
        "state": state, "method": method, "evidence": evidence, **confirmation, "recorded_at": now_iso(),
    }
    if merged_head is not None:
        entry["merge"]["merged_head"] = merged_head


def _merged_head(request: dict[str, Any], state: str) -> str | None:
    """The head commit a merged record names, or None. None counts as absent, as the merge gate counts it."""
    value = request.get("merged_head")
    if value is None:
        return None
    if state != "merged":
        raise WorkspaceError("merged_head belongs to a merged record, not a declined one")
    if not isinstance(value, str) or not COMMIT_PATTERN.match(value):
        raise WorkspaceError("merged_head must be a full lowercase hex commit (40 or 64 digits)")
    return value


def gone_worktree_hint(entry: dict[str, Any]) -> str | None:
    """What to run when the recorded directory no longer exists, or None when there is nothing to say.

    Only an entry with a string repository and branch gets a hint. The branch is probed in the
    repository, not in the missing directory: a squash merge deletes it, and then the worktree can only
    come back detached. Git unavailable or the repository gone gives the retry message.
    """
    path, repository, branch = entry.get("path"), entry.get("repository"), entry.get("branch")
    if not (isinstance(path, str) and isinstance(repository, str) and isinstance(branch, str)):
        return None
    if not (repository and branch) or Path(path).exists():
        return None
    try:
        readable = _git(Path(repository), "rev-parse", "--git-dir") is not None
        present = readable and _git(Path(repository), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except (OSError, subprocess.TimeoutExpired):
        readable = False
    if not readable:
        return "Git could not be consulted for the worktree; retry when it is available"
    gone = f"worktree {path} no longer exists; "
    if present:
        return f"{gone}bring it back with: git -C {repository} worktree add {path} {branch}"
    merge = entry.get("merge")
    head = merge.get("merged_head") if isinstance(merge, dict) else None
    commit = head if isinstance(head, str) and head else entry.get("base_commit")
    if not isinstance(commit, str) or not commit:
        return None
    return (
        f"{gone}branch {branch} is gone from {repository} (as after a squash merge); bring it back detached "
        f"with: git -C {repository} worktree add --detach {path} {commit}. A detached worktree can carry an "
        "exemption or a merge record, not a release: the version diff needs the recorded branch."
    )


def delivery_operation(
    worktrees: list[dict[str, Any]], path: str, operation: str, request: dict[str, Any], confirmation: dict[str, str],
    *, workspace_root: Path, project: str, gate: Gate | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply one delivery operation to the active entry at `path`; returns the entry and any notes.

    `workspace_root` and `project` let the occupancy check read the other projects' records and skip
    this project's own.
    """
    entry = next((item for item in worktrees if item["path"] == path), None)
    if entry is None or entry["status"] != "active" or entry["kind"] == "none":
        raise WorkspaceError(f"no active repository worktree is recorded at {path}")
    hint = None if operation == "merge" and request.get("merge_state") == "declined" else gone_worktree_hint(entry)
    if hint is not None:
        raise WorkspaceError(hint)
    root = Path(path)
    notes: list[str] = []
    if operation == "pull_request":
        _pull_request(entry, request, root, confirmation)
    elif operation == "alpha":
        notes = _alpha(entry, request, root, confirmation, workspace_root, project)
    elif operation == "release":
        notes = _release(entry, request, root, workspace_root, project, confirmation, gate)
    else:
        _merge(entry, request, confirmation, gate)
    return entry, notes


CONFIRMATION = NESTED_INPUTS["confirmation"][0]

# The sentence the operation code raises for a key; the combined refusal repeats it for callers who search for it.
KEY_HINTS = {
    "files": "files are required for the first alpha or release record",
    "reason": "reason must be non-empty text",
    "evidence": "evidence must be non-empty text",
    "method": "a merged record needs method squash, merge or rebase",
    "alpha": "an alpha record needs base, alpha, reason and files, or exempt true with a reason",
    "base": "an alpha record needs base, alpha, reason and files, or exempt true with a reason",
    "host": "a pull_request record needs host, url and number, or exempt no_remote or local_only",
    "url": "a pull_request record needs host, url and number, or exempt no_remote or local_only",
    "number": "a pull_request record needs host, url and number, or exempt no_remote or local_only",
}


def required_for(operation: object, request: dict[str, Any], entry: dict[str, Any] | None) -> frozenset[str]:
    """The keys a worktree request must carry beyond `operation`, `path` and `confirmation`.

    The printed `--schema` table over-reports on purpose; this is what the request is refused for. `entry` is
    the recorded worktree at the request's path, or None when the path is unknown: then the one key that depends
    on the entry (`files`) is not enforced here and the later lookup refuses the path.
    """
    if operation == "record":
        return frozenset() if request.get("kind") == "none" else frozenset({"branch"})
    if operation == "close":
        return frozenset({"decision"})
    if operation == "pull_request":
        return frozenset() if "exempt" in request else frozenset({"host", "url", "number"})
    has_line = entry is not None and "version_line" in entry
    if operation == "alpha":
        if request.get("exempt") is True:
            return frozenset({"reason"})
        keys = {"base", "alpha", "reason"}
        if entry is not None and not has_line:
            keys.add("files")
        return frozenset(keys)
    if operation == "release":
        keys = {"version"}
        if entry is not None and (not has_line or request.get("replaces_exemption") is True):
            keys.add("files")
        return frozenset(keys)
    if operation == "merge":
        state = request.get("merge_state")
        return frozenset({"merge_state", "method", "evidence"} if state == "merged" else {"merge_state"})
    return frozenset()


def _object_errors(
    value: object, label: str, fields: set[str], allowed_extra: frozenset[str] = frozenset()
) -> list[str]:
    """Missing and unexpected fields of a closed object. `allowed_extra` names optional keys."""
    found = field_errors(value, fields | allowed_extra, fields)
    if found.not_object:
        return [f"{label}: must be an object"]
    return [f"{label}: missing {name}" for name in found.missing] + [
        f"{label}: unexpected field {name}" for name in found.unknown
    ]


def _line_errors(line: object, label: str) -> list[str]:
    fields = {"files", "base", "alpha", "history", "release"}
    if isinstance(line, dict) and "base_moves" in line:
        fields.add("base_moves")  # lines recorded before base moves existed carry no list
    problems = _object_errors(line, label, fields)
    if problems or not isinstance(line, dict):
        return problems
    base, alpha, history, release = line["base"], line["alpha"], line["history"], line["release"]
    if not isinstance(base, str) or not PLAIN_VERSION.match(base):
        problems.append(f"{label}: base must be a plain X.Y.Z version")
    problems += _move_errors(line.get("base_moves", []), base, label)
    release_only = alpha is None
    if release_only:
        if history != [] or release is None:
            problems.append(f"{label}: a line with no alpha needs an empty history and a release")
    elif isinstance(alpha, bool) or not isinstance(alpha, int) or alpha < 1:
        problems.append(f"{label}: alpha must be a whole number of at least 1, or null on a release-only line")
    files = line["files"]
    if not isinstance(files, list) or not files or not all(
        isinstance(item, dict) and item.keys() == {"path", "style"} and _non_empty_string(item["path"])
        and item["style"] in STYLES for item in files
    ):
        problems.append(f"{label}: files must list objects with a path and a style of pep440 or semver")
    if not release_only:
        problems += _history_errors(history, line, label)
    if release is not None and (
        _object_errors(release, f"{label}.release", {"version", "recorded_at"})
        or release["version"] != base or not _is_timestamp(release["recorded_at"])
    ):
        problems.append(f"{label}: release needs version equal to base and a recorded_at timestamp")
    return problems


def _history_errors(history: object, line: dict[str, Any], label: str) -> list[str]:
    """Shape and order errors of the alpha history.

    An item records `alpha` and, since the alpha number restarts when the base moves, the `base` it was
    recorded on. Items without a `base` belong to a line recorded before that: they only have to rise.
    Inside one base the number rises; where the base changes it is 1 and a `base_moves` entry ends there.
    """
    if not isinstance(history, list) or not history:
        return [f"{label}: history must be a non-empty list"]
    legacy = {"alpha", "recorded_at", "reason"}
    problems: list[str] = []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(history, start=1):
        if (
            not isinstance(item, dict) or item.keys() not in (legacy, legacy | {"base"})
            or isinstance(item["alpha"], bool) or not isinstance(item["alpha"], int)
            or ("base" in item and not (isinstance(item["base"], str) and PLAIN_VERSION.match(item["base"])))
            or not _is_timestamp(item["recorded_at"]) or not _non_empty_string(item["reason"])
        ):
            problems.append(f"{label}: history item {index} needs a whole alpha, recorded_at and reason")
        else:
            items.append(item)
    if len(items) != len(history):
        return problems
    moved_to = {move["to"] for move in line.get("base_moves", []) if isinstance(move, dict) and "to" in move}
    for position in range(1, len(items)):
        before, after = items[position - 1], items[position]
        where = f"{label}: history item {position + 1}"
        if "base" in before and "base" not in after:
            problems.append(f"{where} lacks the base that the items before it record")
        elif "base" in after and (
            ("base" in before and before["base"] != after["base"])
            or ("base" not in before and after["alpha"] <= before["alpha"])
        ):
            # A base move: the number restarts at 1 and a base_moves entry ends at the new base.
            if after["alpha"] != 1 or after["base"] not in moved_to:
                problems.append(
                    f"{where} moves the base to {after['base']}; a move restarts at alpha 1 and needs a "
                    "matching base_moves entry"
                )
        elif after["alpha"] <= before["alpha"]:
            problems.append(f"{label}: history must increase while the base stays")
    last = items[-1]
    moves = line.get("base_moves", [])
    # A release may move the base without a new history item: then the last move ends at the line's base.
    last_move = moves[-1] if moves and isinstance(moves[-1], dict) else {}
    released_move = bool(line.get("release")) and last_move.get("to") == line["base"]
    if last["alpha"] != line["alpha"] or ("base" in last and last["base"] != line["base"] and not released_move):
        problems.append(f"{label}: history must end at the current alpha and base")
    return problems


def _move_errors(moves: object, base: object, label: str) -> list[str]:
    """Shape errors of `base_moves`: ordered plain versions, each move explained, ending at the base."""
    if not isinstance(moves, list):
        return [f"{label}: base_moves must be a list"]
    for index, item in enumerate(moves, start=1):
        if (
            not isinstance(item, dict) or item.keys() != {"from", "to", "reason", "recorded_at"}
            or not all(isinstance(item[key], str) and PLAIN_VERSION.match(item[key]) for key in ("from", "to"))
            or _version_tuple(item["from"]) >= _version_tuple(item["to"])
            or not _non_empty_string(item["reason"]) or not _is_timestamp(item["recorded_at"])
        ):
            return [f"{label}: base_moves item {index} needs from < to as plain versions, a reason and recorded_at"]
    if moves and moves[-1]["to"] != base:
        return [f"{label}: the last base move must end at the current base"]
    return []


def delivery_errors(entry: dict[str, Any], label: str) -> list[str]:
    """Shape errors of the optional delivery fields of one worktree entry."""
    problems: list[str] = []
    request = entry.get("pull_request")
    if "pull_request" in entry:
        problems += _object_errors(request, f"{label}.pull_request", {"host", "url", "number", "recorded_at"})
        if isinstance(request, dict) and not problems:
            number = request["number"]
            if request["host"] not in HOSTS or not _non_empty_string(request["url"]) or isinstance(number, bool) \
                    or not isinstance(number, int) or number < 1 or not _is_timestamp(request["recorded_at"]):
                problems.append(f"{label}.pull_request: needs host github or gitlab, a URL, a number and a timestamp")
    for name, reason in (("pr_exemption", True), ("version_exemption", False)):
        if name in entry:
            fields = {"reason", *CONFIRMATION} if reason else set(CONFIRMATION)
            if not reason and isinstance(entry[name], dict) and "reason" in entry[name]:
                fields = {"reason", "source", "response"}  # records before 0.30.0 carry no reason
            found = _object_errors(entry[name], f"{label}.{name}", fields)
            problems += found
            if not found and (not all(_non_empty_string(entry[name][key]) for key in fields)
                              or (reason and entry[name]["reason"] not in PR_EXEMPTION_REASONS)):
                problems.append(f"{label}.{name}: needs the user's quoted source and response")
    if "pull_request" in entry and "pr_exemption" in entry:
        problems.append(f"{label}: pull_request and pr_exemption exclude each other")
    if "version_exemption_replaced" in entry:
        replaced = entry["version_exemption_replaced"]
        label_replaced = f"{label}.version_exemption_replaced"
        found = _object_errors(replaced, label_replaced, set(EXEMPTION_REPLACED_FIELDS))
        problems += found
        if not found and (not _is_timestamp(replaced["replaced_at"])
                          or not all(_non_empty_string(replaced[key]) for key in EXEMPTION_REPLACED_FIELDS
                                     if key != "replaced_at")):
            problems.append(
                f"{label_replaced}: needs the exemption's reason, source and response, the release time and the "
                "release confirmation"
            )
    if "version_line" in entry:
        problems += _line_errors(entry["version_line"], f"{label}.version_line")
        if "version_exemption" in entry:
            problems.append(f"{label}: version_line and version_exemption exclude each other")
    if "merge" in entry:
        fields = {"state", "method", "evidence", "source", "response", "recorded_at"}
        found = _object_errors(entry["merge"], f"{label}.merge", fields, frozenset({"merged_head"}))
        problems += found
        if not found:
            merge = entry["merge"]
            if merge["state"] not in MERGE_STATES or not _is_timestamp(merge["recorded_at"]) \
                    or not _non_empty_string(merge["source"]) or not _non_empty_string(merge["response"]):
                problems.append(f"{label}.merge: needs state merged or declined, a timestamp and the user's words")
            if not needs_merge(entry):
                problems.append(f"{label}.merge: needs a pull_request or a local_only exemption")
            head = merge.get("merged_head")
            if "merged_head" in merge and (
                merge["state"] != "merged" or not isinstance(head, str) or not COMMIT_PATTERN.match(head)
            ):
                problems.append(
                    f"{label}.merge: merged_head belongs to a merged record and is a full lowercase hex commit"
                )
            if merge["state"] == "merged":
                line = entry.get("version_line")
                if merge["method"] not in METHODS or not _non_empty_string(merge["evidence"]):
                    problems.append(f"{label}.merge: a merged record needs a method and evidence")
                if isinstance(line, dict) and line.get("release") is None:
                    problems.append(f"{label}.merge: a merged record with a version_line needs a release")
    return problems


def start_findings(state: dict[str, Any]) -> list[str]:
    """What stops a task that writes to the target: no MR for the target worktree. An alpha line is optional."""
    if not is_delivery_gated(state):
        return []
    findings = []
    for item in state.get("worktrees", []):
        if item["role"] != "target" or item["kind"] == "none" or item["status"] != "active":
            continue
        if "pull_request" not in item and "pr_exemption" not in item:
            findings.append(
                f"worktree {item['path']} has no merge request; open a draft MR after the user approves it and record "
                "`workflow <project-dir> worktree` operation pull_request, or record operation pull_request with "
                "exempt local_only when the user says the project is never pushed"
            )
    return findings


def abandoned_note(entry: dict[str, Any]) -> str | None:
    """What removing a worktree whose local merge was declined throws away: the commits of its branch.

    Only a `local_only` worktree with a declined merge has such a note. The count comes from the repository,
    not from the removed directory; when Git cannot be read the note says the count is unknown.
    """
    merge = entry.get("merge")
    declined = isinstance(merge, dict) and merge.get("state") == "declined"
    if "pull_request" in entry or not needs_merge(entry) or not declined:
        return None
    repository, branch, base = entry.get("repository"), entry.get("branch"), entry.get("base_commit")
    if not (isinstance(repository, str) and repository and isinstance(branch, str) and branch and base):
        return None
    count = _git(Path(repository), "rev-list", "--count", f"{base}..{branch}")
    if count is None or not count.strip().isdigit():
        return (
            f"the merge was declined and the number of commits of {branch} that are abandoned is unknown: "
            "Git could not be read"
        )
    number = int(count)
    return f"the merge was declined: {number} {'commit' if number == 1 else 'commits'} of {branch} are abandoned"


def removal_findings(entry: dict[str, Any], *, gated: bool) -> list[str]:
    """What must be recorded before a worktree may be removed, each missing record named.

    c5: the removal decision comes last, after the release and the host merge have records. The MR
    and release requirements belong to the delivery gates, so `gated` turns them on; the merged
    check has always applied to a worktree that has an MR, so it stays unconditional. An exemption
    satisfies its half.
    """
    path = entry["path"]
    findings: list[str] = []
    if gated and "pull_request" not in entry and "pr_exemption" not in entry:
        findings.append(f"worktree {path} has no merge request; record operation pull_request, or a pr_exemption")
    if gated and "version_exemption" not in entry:
        line = entry.get("version_line")
        if not isinstance(line, dict) or line.get("release") is None:
            findings.append(
                f"worktree {path} has no release record; record operation release with the plain version, or a "
                "version_exemption"
            )
    merge = entry.get("merge")
    state = merge.get("state") if isinstance(merge, dict) else None
    if "pull_request" in entry and state != "merged":
        findings.append(f"worktree {path} is not merged; record operation merge, merged, before removing it")
    elif needs_merge(entry) and state not in MERGE_STATES:
        # A declined local merge abandons the work, so it ends the worktree as a merge does.
        findings.append(
            f"worktree {path} has no merge decision; record operation merge, merged or declined, before removing it"
        )
    return findings


def closure_findings(state: dict[str, Any]) -> list[str]:
    """What stops closure for a gated project: every repository worktree needs its delivery records."""
    if not is_delivery_gated(state):
        return []
    findings = []
    for item in state.get("worktrees", []):
        if item["kind"] == "none" or item["status"] == "removed":
            continue
        path = item["path"]
        if "pull_request" not in item and "pr_exemption" not in item:
            findings.append(f"worktree {path} has no pull_request or pr_exemption")
        elif needs_merge(item) and "merge" not in item:
            findings.append(
                f"worktree {path} has no merge decision; ask the user to merge, naming the method, "
                "and record operation merge, merged or declined"
            )
        line = item.get("version_line")
        if "version_exemption" not in item and (not isinstance(line, dict) or line.get("release") is None):
            findings.append(
                f"worktree {path} has no release record or version_exemption; record operation release with the "
                "plain version, or ask the user for an exemption and record operation alpha with exempt true, "
                "a reason and their words"
            )
    return findings


def no_merge_request_message(where: str, task_ids: list[str]) -> str:
    """The one sentence that says target tasks are planned before any MR or exemption is recorded."""
    return (
        f"worktree {where} has no merge request; task(s) {', '.join(task_ids)} write to the target. Open a draft "
        "MR, or record pr_exemption local_only; or depend on a task that does (effect external, output root "
        "external)."
    )


def _objects(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _writes_target(task: dict[str, Any]) -> bool:
    return any(output.get("root") == "target" for output in _objects(task.get("outputs")))


def _opens_merge_request(task: dict[str, Any]) -> bool:
    effect = task.get("effect")
    external = isinstance(effect, dict) and effect.get("kind") == "external"
    return external and any(output.get("root") == "external" for output in _objects(task.get("outputs")))


def _has_external_ancestor(by_id: dict[str, dict[str, Any]], task: dict[str, Any]) -> bool:
    """Depth first over `depends_on`; unknown ids, non-list values and cycles end the walk."""
    seen: set[str] = set()
    pending = [task]
    while pending:
        depends_on = pending.pop().get("depends_on")
        for identifier in depends_on if isinstance(depends_on, list) else []:
            ancestor = by_id.get(identifier) if isinstance(identifier, str) else None
            if ancestor is None or identifier in seen:
                continue
            if _opens_merge_request(ancestor):
                return True
            seen.add(identifier)
            pending.append(ancestor)
    return False


def mr_gate_errors(
    project_dir: Path, previous: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """The MR gate at planning time: a changed target-output task needs an MR record, an exemption or an MR ancestor.

    It runs before the state validator, so every read is guarded and malformed JSON is the validator's to
    report. Only tasks that are new, or whose outputs or `depends_on` changed, are judged; an `authorize`
    patch changes neither. Git unavailable gives a warning, never a refusal.
    """
    from workspace_context import worktree_status

    if not is_delivery_gated(candidate):
        return [], []
    before = {item.get("id"): item for item in _objects(previous.get("tasks"))}
    tasks = _objects(candidate.get("tasks"))
    judged = [
        task
        for task in tasks
        if _writes_target(task)
        and isinstance(task.get("id"), str)
        and (
            task["id"] not in before
            or any(task.get(key) != before[task["id"]].get(key) for key in ("outputs", "depends_on"))
        )
    ]
    worktrees = candidate.get("worktrees", [])
    if not judged or not isinstance(candidate.get("working_directory"), str) or not isinstance(worktrees, list):
        return [], []
    if not all(isinstance(item, dict) and "role" in item and "status" in item for item in worktrees):
        return [], []
    entry = next((item for item in worktrees if item["role"] == "target" and item["status"] == "active"), None)
    if entry is not None:
        if entry.get("kind") == "none" or "pull_request" in entry or "pr_exemption" in entry:
            return [], []
        where = str(entry.get("path"))
    else:
        status = worktree_status(candidate)
        if not status["available"]:
            return [], status["warnings"][:1]
        if not status["is_repository"]:
            return [], []
        where = candidate["working_directory"]
    by_id = {task["id"]: task for task in tasks if isinstance(task.get("id"), str)}
    unprotected = [task["id"] for task in judged if not _has_external_ancestor(by_id, task)]
    return ([no_merge_request_message(where, unprotected)] if unprotected else []), []
