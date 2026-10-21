"""One line per project-scoped CLI call, written after the call and never in its way.

A retro of the project skill needs what the tool refused, how often, and why. Until this file
existed nothing recorded that: the operation journal keeps successful writes only, and a refused
command left no trace but the agent's memory. The line is written in the CLI's last step, after the
command printed its result, so a failure to log can change neither exit code nor output. Only a
directory that holds `project.json` is written to: a typo in the path must not plant a file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workspace_journal import OperationError
from workspace_lib import WorkspaceConflict, WorkspaceError, now_iso

if TYPE_CHECKING:
    import argparse

INVOCATIONS_FILENAME = "invocations.jsonl"
# Longest message kept: the first line plus the first bullet of a multi-line error, enough to group
# refusals by their cause without letting one line grow unbounded.
MESSAGE_CHARS = 200
EXAMPLE_LIMIT = 10
# `conflict`: a revision, token or entry conflict. `operation`: a named operation failed with a partial
# outcome. `refused`: any other workspace rule. `nonzero`: the command returned a failure without an
# exception (a validation that found errors, a wrapped command that failed). `crash`: anything else.
ERROR_CLASSES = ("conflict", "operation", "refused", "nonzero", "crash")
_VERSION: "list[str]" = []


def classify(error: "BaseException | None", exit_code: int) -> "str | None":
    """The error class of a finished call, or None for a clean success."""
    if error is None:
        return None if exit_code == 0 else "nonzero"
    if isinstance(error, WorkspaceConflict):
        return "conflict"
    if isinstance(error, OperationError) or type(error).__name__ == "CloseOperationError":
        return "operation"
    if isinstance(error, WorkspaceError):
        return "refused"
    return "crash"


def project_directory_of(args: "argparse.Namespace") -> "Path | None":
    """The project directory the call named, resolved, or None for a root-level command."""
    value = getattr(args, "project_directory", None)
    if not isinstance(value, Path):
        return None
    return value.expanduser().resolve()


def refusal_message(error: "BaseException | None", *, limit: int = MESSAGE_CHARS) -> str:
    """The first line plus the first bullet of a multi-line refusal, joined onto one line.

    A candidate-validation refusal reads `candidate validation failed:` and then one bullet per
    problem. Keeping only the first line logged the header and dropped every cause, so a retro could
    not group the failures; keeping the first bullet as well restores the cause without unbounded
    growth. A single-line error is returned unchanged.
    """
    if error is None:
        return ""
    lines = [line.strip() for line in str(error).strip().splitlines() if line.strip()]
    if not lines:
        return ""
    parts = [lines[0]]
    for line in lines[1:]:
        if line.startswith(("-", "*")):
            parts.append(line)
            break
    return " ".join(parts)[:limit]


def action_of(args: "argparse.Namespace") -> str:
    """The sub-choice of the command: a workflow or task action, an append kind, a read document."""
    for name in ("action", "kind", "document"):
        value = getattr(args, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


def plugin_version() -> str:
    """The plugin version the running copy ships, from its manifest; '' when that cannot be read."""
    if not _VERSION:
        manifest = Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
        try:
            value = json.loads(manifest.read_text(encoding="utf-8")).get("version", "")
        except (OSError, ValueError, AttributeError):
            value = ""
        _VERSION.append(value if isinstance(value, str) else "")
    return _VERSION[0]


class Invocation:
    """What one CLI call was, started when the parser finished; `record` writes the line."""

    def __init__(
        self, tool: str, args: "argparse.Namespace", *, command: "str | None" = None, action: "str | None" = None
    ) -> None:
        self.started = time.monotonic()
        self.tool = tool
        self.project_dir = project_directory_of(args)
        self.command = command if command is not None else str(getattr(args, "command", "") or "")
        self.action = action if action is not None else action_of(args)
        self.expected_revision = getattr(args, "expected_revision", None)

    def record(self, exit_code: int, error: "BaseException | None" = None) -> "Path | None":
        """Append the line; return its path, or None when nothing was (or could be) written."""
        if self.project_dir is None or not (self.project_dir / "project.json").is_file():
            return None
        line: dict[str, Any] = {
            "at": now_iso(),
            "tool": self.tool,
            "command": self.command,
            "action": self.action,
            "exit": int(exit_code),
            "ms": int((time.monotonic() - self.started) * 1000),
        }
        if isinstance(self.expected_revision, int):
            line["expected_revision"] = self.expected_revision
        version = plugin_version()
        if version:
            line["version"] = version
        kind = classify(error, exit_code)
        if kind is not None:
            line["error"] = kind
            message = refusal_message(error)
            if message:
                line["message"] = message
        path = self.project_dir / INVOCATIONS_FILENAME
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            return None
        return path


def summarize_invocations(project_dir: Path, *, example_limit: int = EXAMPLE_LIMIT) -> dict[str, Any]:
    """Fold the log into bounded counts: totals by command, failures by class, the first failures."""
    summary: dict[str, Any] = {
        "total": 0, "by_command": {}, "failures": {}, "failed_examples": [], "ms_total": 0, "unparsed": 0,
        "first_at": None, "last_at": None,
    }
    path = project_dir / INVOCATIONS_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return summary
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
            if not isinstance(line, dict):
                raise ValueError("not an object")
        except ValueError:
            summary["unparsed"] += 1
            continue
        summary["total"] += 1
        command = str(line.get("command", ""))
        summary["by_command"][command] = summary["by_command"].get(command, 0) + 1
        ms = line.get("ms")
        if isinstance(ms, int):
            summary["ms_total"] += ms
        at = line.get("at")
        if isinstance(at, str):
            summary["first_at"] = summary["first_at"] or at
            summary["last_at"] = at
        error = line.get("error")
        if isinstance(error, str) and error:
            summary["failures"][error] = summary["failures"].get(error, 0) + 1
            if len(summary["failed_examples"]) < example_limit:
                summary["failed_examples"].append(
                    {key: line.get(key) for key in ("at", "command", "action", "error", "message")}
                )
    return summary
