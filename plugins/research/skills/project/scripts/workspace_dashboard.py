"""Read-only model behind the research dashboard.

`build_model` turns one workspace root into the JSON the page shows. `fingerprint` says whether
anything the page shows has changed. `SessionIndex` finds Claude Code sessions and `PiSessionIndex`
finds Pi sessions. Nothing here writes to the workspace, `~/.claude` or `~/.pi`.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
import shlex
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from workspace_context import checkpoint_data
from workspace_lib import (
    WorkspaceError,
    find_workspace_roots,
    is_canonical_project_id,
    load_json,
    now_iso,
    planning_path,
    read_text,
    resolve_workspace_root,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

TERMINAL = frozenset({"DONE", "CANCELLED"})
WATCHED_FILES = ("project.json", "spec.md", "handoff.md")
DECISION_LIMIT = 6
SESSION_LIMIT = 3
# A transcript counts as having worked on a project when a write command named the project
# directory, or when the project is a real share of the transcript's project mentions. A listing or
# a dump mentions many projects once or twice.
MIN_MENTIONS = 3
MENTION_SHARE = 0.25
CWD_SCAN_BYTES = 200_000
PROMPT_CHARS = 140
MENTION = re.compile(rb"workspace/(\d{4}-\d{2}-\d{2}-\d{3})")
# Only a write command proves work on the project. A read (context, read, list-projects) does not.
WRITE_COMMAND = re.compile(
    rb"research-project\s+(?:init|workflow|update|edit|append|task|close|commit|record-evidence"
    rb"|record-observation|stage)\s+(?:\S*/)?workspace/(\d{4}-\d{2}-\d{2}-\d{3})"
)
CWD = re.compile(rb'"cwd":"([^"]+)"')
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGE = Path(__file__).with_name("dashboard.html")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _section(markdown: str, heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}[ \t]*\n(.*?)(?=^### |^## |\Z)", markdown, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _decisions(markdown: str) -> list[str]:
    match = re.search(r"^## Decision history[ \t]*\n(.*)\Z", markdown, re.DOTALL | re.MULTILINE)
    if not match:
        return []
    entries = [COMMENT.sub("", line[2:]).strip() for line in match.group(1).splitlines() if line.startswith("- ")]
    return [entry for entry in entries if entry][-DECISION_LIMIT:][::-1]


def _repository(working_directory: str) -> str:
    """Name the repository a project works in, seeing through `<repo>.worktrees/<id>` checkouts."""
    path = Path(working_directory)
    if path.parent.name.endswith(".worktrees"):
        return path.parent.name[: -len(".worktrees")]
    return path.name or working_directory


def _read_optional(path: Path) -> str:
    try:
        return read_text(path)
    except WorkspaceError:
        return ""


def _task_rows(tasks: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        effect = task.get("effect")
        evidence = task.get("evidence")
        rows.append(
            {
                "id": _text(task.get("id")),
                "name": _text(task.get("name")),
                "status": _text(task.get("status")),
                "effect": _text(effect.get("kind")) if isinstance(effect, dict) else "",
                "evidence": len(evidence) if isinstance(evidence, list) else 0,
                "block_reason": _text(task.get("block_reason")),
                "authorization": task.get("authorization") if isinstance(task.get("authorization"), dict) else {},
            }
        )
    return rows


def _worktrees(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for tree in state.get("worktrees") or []:
        if not isinstance(tree, dict):
            continue
        request = tree.get("pull_request")
        rows.append(
            {
                "branch": _text(tree.get("branch")),
                "path": _text(tree.get("path")),
                "status": _text(tree.get("status")),
                "mr": _text(request.get("url")) if isinstance(request, dict) else "",
            }
        )
    return rows


def _handoff(project_dir: Path) -> dict[str, Any]:
    """Read the checkpoint block of `handoff.md`; a missing or unreadable note is no handoff."""
    try:
        data = checkpoint_data(project_dir)
    except WorkspaceError:
        data = None
    if data is None:
        return {"has_handoff": False, "session": "", "next": "", "questions": "", "do_not": "", "recorded": None}
    note = data["continuation"]
    recorded = data.get("session")
    return {
        "has_handoff": True,
        "session": note.get("session", ""),
        "next": note.get("next", ""),
        "questions": note.get("questions", ""),
        "do_not": note.get("do_not", ""),
        "recorded": recorded if isinstance(recorded, dict) else None,
    }


def build_project(project_dir: Path) -> dict[str, Any]:
    """Describe one project directory. A project that cannot be read is a row, not an error."""
    row: dict[str, Any] = {"id": project_dir.name, "path": str(project_dir), "unreadable": False}
    try:
        state = load_json(project_dir / "project.json")
    except WorkspaceError:
        state = {}
    status = state.get("status")
    if not isinstance(status, str) or not status:
        row.update(unreadable=True, title=project_dir.name, status="UNREADABLE", needs_you=False, sessions=[])
        return row
    tasks = _task_rows(state["tasks"] if isinstance(state.get("tasks"), list) else [])
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    # Only a task that is running or blocked on its authorization waits on the user now.
    pending_authorization = any(
        task["authorization"].get("required")
        and task["authorization"].get("status") != "explicit"
        and task["status"] in {"RUNNING", "BLOCKED"}
        for task in tasks
    )
    raw_review = state.get("review")
    review: dict[str, Any] = raw_review if isinstance(raw_review, dict) else {}
    handoff = _handoff(project_dir)
    spec = _read_optional(project_dir / "spec.md")
    working_directory = _text(state.get("working_directory"))
    waiting = (
        status == "BLOCKED"
        or pending_authorization
        or handoff["session"] in {"waiting", "blocked"}
        or review.get("status") == "pending"
    )
    row.update(
        {
            "title": _text(state.get("title")) or project_dir.name,
            "status": status,
            "created": _text(state.get("created")),
            "updated": _text(state.get("updated")),
            "schema": state.get("schema_version"),
            "working_directory": working_directory,
            "repo": _repository(working_directory),
            "task_total": len(tasks),
            "task_done": counts.get("DONE", 0),
            "task_counts": counts,
            "running": [f"{task['id']} {task['name']}" for task in tasks if task["status"] == "RUNNING"],
            "tasks": [{key: value for key, value in task.items() if key != "authorization"} for task in tasks],
            "review": _text(review.get("status")) or "not_required",
            "review_required": bool(review.get("required")),
            "has_handoff": handoff["has_handoff"],
            "session": handoff["session"],
            "next": handoff["next"],
            "questions": handoff["questions"],
            "do_not": handoff["do_not"],
            "recorded_session": handoff["recorded"],
            "objective": _section(spec, "Objective and audience"),
            "in_scope": _section(spec, "In scope"),
            "decisions": _decisions(spec),
            "worktrees": _worktrees(state),
            "planning_file": planning_path(project_dir).name,
            "cancellation_reason": _text(state.get("cancellation_reason")),
            "block_reasons": [task["block_reason"] for task in tasks if task["block_reason"]],
            "pending_authorization": pending_authorization,
            "needs_you": status not in TERMINAL and waiting,
            "sessions": [],
        }
    )
    return row


def project_directories(root: Path) -> list[Path]:
    """List the canonical project directories under a root, newest id first."""
    try:
        children = list(root.iterdir())
    except OSError as error:
        raise WorkspaceError(f"cannot list {root}: {error}") from error
    return sorted(
        (child for child in children if child.is_dir() and is_canonical_project_id(child.name)),
        key=lambda child: child.name,
        reverse=True,
    )


def fingerprint(root: Path) -> str:
    """Hash the name, modification time and size of every file the page reads.

    A change to any of them changes the fingerprint, and nothing else does: the page polls this
    cheap value and fetches the model only when it moves.
    """
    digest = hashlib.sha256()
    for directory in project_directories(root):
        for name in WATCHED_FILES:
            try:
                info = (directory / name).stat()
            except OSError:
                continue
            digest.update(f"{directory.name}/{name}\0{info.st_mtime_ns}\0{info.st_size}\n".encode())
    return digest.hexdigest()


def build_model(root: Path) -> dict[str, Any]:
    """Describe every project under a root. Sessions are filled in by `attach_sessions`."""
    return {"root": str(root), "projects": [build_project(directory) for directory in project_directories(root)]}


class SessionIndex:
    """Find the Claude Code sessions that worked on a project, from their transcripts.

    Claude Code files each session as `<claude dir>/projects/<cwd>/<session id>.jsonl`. Whatever
    session works on a research project writes the project directory into its transcript, so the
    transcripts are the only record that links a project to a session. The layout is not a
    documented interface: every failure to read it yields fewer sessions, never an error.

    A transcript is read once and kept until its modification time or size changes.
    """

    def __init__(self, claude_dir: Path) -> None:
        self.claude_dir = claude_dir
        self._seen: dict[Path, tuple[int, int, dict[str, Any]]] = {}

    def _scan(self, transcript: Path) -> dict[str, Any] | None:
        try:
            info = transcript.stat()
            cached = self._seen.get(transcript)
            if cached and cached[:2] == (info.st_mtime_ns, info.st_size):
                return cached[2]
            data = transcript.read_bytes()
        except OSError:
            return None
        mentions: dict[str, int] = {}
        for found in MENTION.findall(data):
            name = found.decode()
            mentions[name] = mentions.get(name, 0) + 1
        cwd = CWD.search(data[:CWD_SCAN_BYTES])
        entry = {
            "mentions": mentions,
            "commanded": {found.decode() for found in WRITE_COMMAND.findall(data)},
            "cwd": cwd.group(1).decode("utf-8", "replace") if cwd else "",
            "last": info.st_mtime,
        }
        self._seen[transcript] = (info.st_mtime_ns, info.st_size, entry)
        return entry

    def _running(self) -> dict[str, str]:
        running: dict[str, str] = {}
        for path in (self.claude_dir / "sessions").glob("*.json"):
            try:
                value = json.loads(read_text(path))
            except (WorkspaceError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("sessionId"), str):
                running[value["sessionId"]] = _text(value.get("status")) or "running"
        return running

    def _prompts(self) -> dict[str, str]:
        """Last prompt of each session, to label it. A line that cannot be read is skipped."""
        latest: dict[str, tuple[int, str]] = {}
        try:
            text = read_text(self.claude_dir / "history.jsonl")
        except WorkspaceError:
            return {}
        for line in text.splitlines():
            try:
                value = json.loads(line)
                session, stamp = value["sessionId"], int(value.get("timestamp") or 0)
            except (ValueError, KeyError, TypeError):
                continue
            if stamp >= latest.get(session, (0, ""))[0]:
                latest[session] = (stamp, _text(value.get("display"))[:PROMPT_CHARS])
        return {session: label for session, (_, label) in latest.items()}

    def find(self) -> dict[str, list[dict[str, Any]]]:
        """Map project id to its sessions, the one that moved last first."""
        running, prompts = self._running(), self._prompts()
        found: dict[str, list[dict[str, Any]]] = {}
        for transcript in sorted((self.claude_dir / "projects").glob("*/*.jsonl")):
            entry = self._scan(transcript)
            if not entry or not entry["mentions"]:
                continue
            busiest = max(entry["mentions"].values())
            for name, count in entry["mentions"].items():
                if count < MIN_MENTIONS or (name not in entry["commanded"] and count < MENTION_SHARE * busiest):
                    continue
                found.setdefault(name, []).append(
                    {
                        "id": transcript.stem,
                        "cwd": entry["cwd"],
                        "last": datetime.fromtimestamp(entry["last"]).astimezone().isoformat(timespec="seconds"),
                        "mentions": count,
                        "commanded": name in entry["commanded"],
                        "exact": False,
                        "running": running.get(transcript.stem, ""),
                        "prompt": prompts.get(transcript.stem, ""),
                        "host": "claude",
                    }
                )
        for sessions in found.values():
            # The last session that worked on the project matters most.
            sessions.sort(key=lambda session: session["last"], reverse=True)
            del sessions[SESSION_LIMIT:]
        return found

    def labels(self, session: str) -> dict[str, str]:
        """Running state and last prompt of one session, for a session a checkpoint recorded."""
        return {"running": self._running().get(session, ""), "prompt": self._prompts().get(session, "")}


def _pi_header(data: bytes) -> dict[str, Any] | None:
    """Read the session header from the first JSONL line, or None when it is unusable."""
    try:
        header = json.loads(data.splitlines()[0])
    except (IndexError, ValueError):
        return None
    if not isinstance(header, dict) or header.get("type") != "session":
        return None
    identity = header.get("id")
    if not isinstance(identity, str) or not identity:
        return None
    if not isinstance(header.get("cwd"), str):
        header["cwd"] = ""
    return header


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return " ".join(parts).strip()
    return ""


def _pi_prompt(data: bytes) -> str:
    """The last user message in a Pi session, trimmed for a row label."""
    prompt = ""
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _user_text(message.get("content"))
        if text:
            prompt = text[:PROMPT_CHARS]
    return prompt


class PiSessionIndex:
    """Find the Pi sessions that worked on a project, from their JSONL transcripts.

    Pi files each session as `<pi dir>/sessions/--<cwd>--/<timestamp>_<id>.jsonl`. The header
    names the session and its working directory. The layout is not a documented interface: every
    failure to read it yields fewer sessions, never an error.
    """

    def __init__(self, pi_dir: Path) -> None:
        self.pi_dir = pi_dir
        self._seen: dict[Path, tuple[int, int, dict[str, Any]]] = {}

    def _scan(self, transcript: Path) -> dict[str, Any] | None:
        try:
            info = transcript.stat()
            cached = self._seen.get(transcript)
            if cached and cached[:2] == (info.st_mtime_ns, info.st_size):
                return cached[2]
            data = transcript.read_bytes()
        except OSError:
            return None
        header = _pi_header(data)
        if header is None:
            return None
        mentions: dict[str, int] = {}
        for found in MENTION.findall(data):
            name = found.decode()
            mentions[name] = mentions.get(name, 0) + 1
        entry = {
            "header": header,
            "mentions": mentions,
            "commanded": {found.decode() for found in WRITE_COMMAND.findall(data)},
            "prompt": _pi_prompt(data),
            "last": info.st_mtime,
        }
        self._seen[transcript] = (info.st_mtime_ns, info.st_size, entry)
        return entry

    def find(self) -> dict[str, list[dict[str, Any]]]:
        """Map project id to its Pi sessions, the one that moved last first."""
        found: dict[str, list[dict[str, Any]]] = {}
        for transcript in sorted((self.pi_dir / "sessions").glob("*/*.jsonl")):
            entry = self._scan(transcript)
            if not entry or not entry["mentions"]:
                continue
            busiest = max(entry["mentions"].values())
            for name, count in entry["mentions"].items():
                if count < MIN_MENTIONS or (name not in entry["commanded"] and count < MENTION_SHARE * busiest):
                    continue
                found.setdefault(name, []).append(
                    {
                        "id": entry["header"]["id"],
                        "cwd": entry["header"]["cwd"],
                        "last": datetime.fromtimestamp(entry["last"]).astimezone().isoformat(timespec="seconds"),
                        "mentions": count,
                        "commanded": name in entry["commanded"],
                        "exact": False,
                        "running": "",
                        "prompt": entry["prompt"],
                        "host": "pi",
                    }
                )
        for sessions in found.values():
            sessions.sort(key=lambda session: session["last"], reverse=True)
            del sessions[SESSION_LIMIT:]
        return found


def _resume(session: dict[str, Any]) -> str:
    """The command that resumes one session in its own working directory."""
    cwd = _text(session.get("cwd"))
    prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    command = "pi --session" if session.get("host") == "pi" else "claude --resume"
    return f"{prefix}{command} {shlex.quote(_text(session.get('id')))}"


def attach_sessions(
    projects: list[dict[str, Any]], index: SessionIndex, pi_index: PiSessionIndex | None = None
) -> list[dict[str, Any]]:
    """Copy the projects with their sessions: the recorded one first and exact, then the search."""
    found = index.find()
    pi_found = pi_index.find() if pi_index is not None else {}
    result = []
    for project in projects:
        sessions = [dict(session) for session in found.get(project["id"], [])]
        sessions += [dict(session) for session in pi_found.get(project["id"], [])]
        recorded = project.get("recorded_session")
        if recorded and recorded.get("id"):
            host = _text(recorded.get("host")) or "claude"
            sessions = [
                session
                for session in sessions
                if not (session.get("host") == host and session["id"] == recorded["id"])
            ]
            labels = index.labels(recorded["id"]) if host == "claude" else {"running": "", "prompt": ""}
            sessions.insert(
                0,
                {
                    "id": recorded["id"],
                    "cwd": _text(recorded.get("cwd")),
                    "last": "",
                    "mentions": 0,
                    "commanded": True,
                    "exact": True,
                    "host": host,
                    **labels,
                },
            )
        sessions.sort(key=lambda session: (bool(session.get("exact")), session.get("last") or ""), reverse=True)
        for session in sessions:
            session["resume"] = _resume(session)
        result.append({**project, "sessions": sessions[:SESSION_LIMIT]})
    return result


class Dashboard:
    """Serve one workspace root: the model is rebuilt only when the fingerprint moves.

    The session index is refreshed on every call: a transcript changes whenever any session runs,
    so it must not decide whether the page reloads.
    """

    def __init__(self, root: Path, claude_dir: Path, page: Path = PAGE, *, pi_dir: Path | None = None) -> None:
        self.root = root
        self.page = page
        self._index = SessionIndex(claude_dir)
        self._pi_index = PiSessionIndex(pi_dir) if pi_dir is not None else None
        self._lock = threading.Lock()
        self._seen = ""
        self._projects: list[dict[str, Any]] = []

    def version(self) -> dict[str, str]:
        return {"fingerprint": fingerprint(self.root)}

    def payload(self) -> dict[str, Any]:
        with self._lock:
            current = fingerprint(self.root)
            if current != self._seen:
                self._projects = build_model(self.root)["projects"]
                self._seen = current
            projects = attach_sessions(self._projects, self._index, self._pi_index)
        return {"root": str(self.root), "generated": now_iso(), "fingerprint": current, "projects": projects}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dashboard: Dashboard, port: int) -> None:
        super().__init__((HOST, port), _Handler)
        self.dashboard = dashboard

    def allowed_hosts(self) -> set[str]:
        return {f"{HOST}:{self.server_port}", f"localhost:{self.server_port}"}


class _Handler(BaseHTTPRequestHandler):
    """Three read-only routes. Anything else is refused."""

    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the terminal for the URL and for errors."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, value: dict[str, Any]) -> None:
        self._send(200, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        # A web page on another origin can reach 127.0.0.1 through a rebound host name. It cannot
        # choose the Host header the browser sends, so only our own address is answered.
        if self.headers.get("Host") not in self.server.allowed_hosts():
            return self._text(403, "Forbidden host.")
        dashboard = self.server.dashboard
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = dashboard.page.read_bytes()
            except OSError:
                return self._text(500, f"{dashboard.page.name} is missing from the plugin.")
            return self._send(200, body, "text/html; charset=utf-8")
        if path in ("/api/version", "/api/projects"):
            try:
                return self._json(dashboard.version() if path == "/api/version" else dashboard.payload())
            except WorkspaceError as error:
                return self._text(500, str(error))
        return self._text(404, "Not found.")


def make_server(dashboard: Dashboard, port: int) -> DashboardServer:
    """Bind the loopback address. Port 0 takes any free port."""
    return DashboardServer(dashboard, port)


def resolve_root(argument: str | None) -> Path:
    """Take the root from the argument, else `$RESEARCH_WORKSPACE`, else the one established root."""
    if argument:
        path = Path(argument).expanduser()
    else:
        try:
            path = resolve_workspace_root(None)
        except WorkspaceError:
            roots = find_workspace_roots()
            if not roots:
                raise WorkspaceError("No workspace root. Pass ROOT or set RESEARCH_WORKSPACE.") from None
            if len(roots) > 1:
                listed = ", ".join(str(root) for root in roots)
                raise WorkspaceError(f"Several workspace roots: {listed}. Pass ROOT to choose one.") from None
            path = roots[0]
    if not path.is_dir():
        raise WorkspaceError(f"{path} is not a directory.")
    return path.resolve()


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be a number from 0 to 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-dashboard", description="Browse the research workspace in a local web page."
    )
    parser.add_argument("root", nargs="?", help="workspace root; default $RESEARCH_WORKSPACE, else the one root found")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default 8765; 0 takes any free port")
    parser.add_argument("--no-browser", action="store_true", help="print the URL and do not open it")
    parser.add_argument("--claude-dir", default="~/.claude", help="where Claude Code keeps its sessions")
    parser.add_argument("--pi-dir", default="~/.pi/agent", help="where Pi keeps its sessions")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_root(args.root)
    except WorkspaceError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        server = make_server(
            Dashboard(
                root,
                Path(args.claude_dir).expanduser(),
                pi_dir=Path(args.pi_dir).expanduser(),
            ),
            args.port,
        )
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print(f"Port {args.port} in use. Use --port 0 for a free port.", file=sys.stderr)
        else:
            print(f"Cannot listen on port {args.port}: {error}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{server.server_port}"
    print(f"Dashboard: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
