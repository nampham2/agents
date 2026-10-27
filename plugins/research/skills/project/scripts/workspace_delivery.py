"""Delivery records of a repository worktree: the MR, the alpha line, the release and the merge.

The tools never call a Git host and never write to Git. They record what the agent reports and
check only what a local read can show: the host of the remote, the version strings inside the
recorded files, and the order of the steps. The gates turn on for projects created on or after
`DELIVERY_GATES_FROM`.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from workspace_context import _git
from workspace_journal import list_input, object_input, string_input
from workspace_lib import WorkspaceError, _is_timestamp, _non_empty_string, is_delivery_gated, now_iso
from workspace_occupancy import check_base, own_text

DELIVERY_OPERATIONS = {"pull_request", "alpha", "release", "merge"}
DELIVERY_FIELDS = {"pull_request", "pr_exemption", "version_line", "version_exemption", "merge"}
HOSTS = {"github", "gitlab"}
STYLES = {"pep440", "semver"}
METHODS = {"squash", "merge", "rebase"}
MERGE_STATES = {"merged", "declined"}
PLAIN_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
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
        if request["exempt"] != "no_remote":
            raise WorkspaceError("exempt must be no_remote")
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


def _release(
    entry: dict[str, Any], request: dict[str, Any], root: Path, workspace_root: Path, project: str,
    gate: Gate | None = None,
) -> list[str]:
    """Record the plain release version. A project that never recorded an alpha line starts its line here."""
    if "version_exemption" in entry:
        raise WorkspaceError("the worktree has a version_exemption; no release record applies")
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
    if "pull_request" not in entry:
        raise WorkspaceError("no pull_request is recorded; an exempt worktree has nothing to merge")
    state = request.get("merge_state")
    if state not in MERGE_STATES:
        raise WorkspaceError("merge_state must be merged or declined")
    if entry.get("merge", {}).get("state") == "merged":
        raise WorkspaceError("the merge is already recorded; a merge is final")
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
    root = Path(path)
    notes: list[str] = []
    if operation == "pull_request":
        _pull_request(entry, request, root, confirmation)
    elif operation == "alpha":
        notes = _alpha(entry, request, root, confirmation, workspace_root, project)
    elif operation == "release":
        notes = _release(entry, request, root, workspace_root, project, gate)
    else:
        _merge(entry, request, confirmation, gate)
    return entry, notes


def _object_errors(value: object, label: str, fields: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}: must be an object"]
    problems = [f"{label}: missing {name}" for name in sorted(fields - value.keys())]
    problems += [f"{label}: unexpected field {name}" for name in sorted(value.keys() - fields)]
    return problems


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
            fields = {"reason", "source", "response"} if reason else {"source", "response"}
            if not reason and isinstance(entry[name], dict) and "reason" in entry[name]:
                fields = {"reason", "source", "response"}  # records before 0.30.0 carry no reason
            found = _object_errors(entry[name], f"{label}.{name}", fields)
            problems += found
            if not found and (not all(_non_empty_string(entry[name][key]) for key in fields)
                              or (reason and entry[name]["reason"] != "no_remote")):
                problems.append(f"{label}.{name}: needs the user's quoted source and response")
    if "pull_request" in entry and "pr_exemption" in entry:
        problems.append(f"{label}: pull_request and pr_exemption exclude each other")
    if "version_line" in entry:
        problems += _line_errors(entry["version_line"], f"{label}.version_line")
        if "version_exemption" in entry:
            problems.append(f"{label}: version_line and version_exemption exclude each other")
    if "merge" in entry:
        fields = {"state", "method", "evidence", "source", "response", "recorded_at"}
        found = _object_errors(entry["merge"], f"{label}.merge", fields)
        problems += found
        if not found:
            merge = entry["merge"]
            if merge["state"] not in MERGE_STATES or not _is_timestamp(merge["recorded_at"]) \
                    or not _non_empty_string(merge["source"]) or not _non_empty_string(merge["response"]):
                problems.append(f"{label}.merge: needs state merged or declined, a timestamp and the user's words")
            if "pull_request" not in entry:
                problems.append(f"{label}.merge: needs a pull_request")
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
                f"worktree {item['path']} has no merge request; open a draft MR after the user approves it, then "
                "record `workflow <project-dir> worktree` operation pull_request"
            )
    return findings


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
    if "pull_request" in entry and entry.get("merge", {}).get("state") != "merged":
        findings.append(f"worktree {path} is not merged; record operation merge, merged, before removing it")
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
        elif "pull_request" in item and "merge" not in item:
            findings.append(
                f"worktree {path} has no merge decision; ask the user to merge the MR (naming the method), "
                "then record operation merge, merged or declined"
            )
        line = item.get("version_line")
        if "version_exemption" not in item and (not isinstance(line, dict) or line.get("release") is None):
            findings.append(
                f"worktree {path} has no release record or version_exemption; record operation release with the "
                "plain version, or ask the user for an exemption and record operation alpha with exempt true, "
                "a reason and their words"
            )
    return findings
