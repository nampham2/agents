"""The model behind the research dashboard: projects, the change fingerprint and the session index.

Fixtures are small workspaces and a small `~/.claude` built in a temporary directory. Nothing here
reads the real home directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import workspace_dashboard as dash
import workspace_lib as lib
from workspace_workflows import workflow

from tests.plugins.research.project.test_lifecycle_commands import request

ID = "2026-10-12-003"
OTHER = "2026-10-11-001"


def write_project(root: Path, identity: str = ID, **state: Any) -> Path:
    directory = root / identity
    directory.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "schema_version": 4,
        "project": identity,
        "title": f"Title of {identity}",
        "status": "EXECUTING",
        "created": "2026-10-12T10:00:00+02:00",
        "updated": "2026-10-12T11:00:00+02:00",
        "working_directory": "/work/agents",
        "tasks": [],
    }
    base.update(state)
    (directory / "project.json").write_text(json.dumps(base), encoding="utf-8")
    return directory


def task(identity: str = "T01", status: str = "TODO", **fields: Any) -> dict[str, Any]:
    return {
        "id": identity,
        "name": f"Task {identity}",
        "status": status,
        "effect": {"kind": "local_write"},
        "evidence": [],
        **fields,
    }


def waiting(row: dict[str, Any]) -> bool:
    return bool(row["needs_you"])


# --- project rows --------------------------------------------------------------------------------


def test_a_full_project_row(tmp_path: Path) -> None:
    directory = write_project(
        tmp_path,
        working_directory="/git/agents.worktrees/2026-10-12-003",
        tasks=[
            task("T01", "DONE", evidence=["a", "b"]),
            task("T02", "RUNNING"),
            task("T03", "BLOCKED", block_reason="Waiting for the user"),
        ],
        review={"required": True, "status": "pending", "cycle": 1},
        worktrees=[
            {"branch": "feat/x", "path": "/git/agents.worktrees/x", "status": "active", "pull_request": {"url": "u"}},
            {"branch": "feat/y", "path": "/p", "status": "kept", "pull_request": None},
            "not a worktree",
        ],
        cancellation_reason="",
    )
    (directory / "spec.md").write_text(
        "# T\n\n## Current specification\n\n### Objective and audience\nShow projects.\n\n"
        "### In scope\n- one\n- two\n\n"
        "### Out of scope\nNothing.\n\n## Decision history\n\n"
        "- first\n- <!-- marker --> second\n- \n- third\n",
        encoding="utf-8",
    )
    row = dash.build_project(directory)
    assert row["unreadable"] is False
    assert (row["id"], row["status"], row["title"]) == (ID, "EXECUTING", f"Title of {ID}")
    assert row["repo"] == "agents"
    assert (row["task_total"], row["task_done"]) == (3, 1)
    assert row["task_counts"] == {"DONE": 1, "RUNNING": 1, "BLOCKED": 1}
    assert row["running"] == ["T02 Task T02"]
    assert [item["evidence"] for item in row["tasks"]] == [2, 0, 0]
    assert all("authorization" not in item for item in row["tasks"])
    assert row["block_reasons"] == ["Waiting for the user"]
    assert row["review"] == "pending" and row["review_required"] is True
    assert row["objective"] == "Show projects."
    assert row["in_scope"] == "- one\n- two"
    assert row["decisions"] == ["third", "second", "first"]
    assert row["worktrees"] == [
        {"branch": "feat/x", "path": "/git/agents.worktrees/x", "status": "active", "mr": "u"},
        {"branch": "feat/y", "path": "/p", "status": "kept", "mr": ""},
    ]
    assert waiting(row)


def test_decisions_keep_the_latest_six_newest_first(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    entries = "".join(f"- decision {number}\n" for number in range(10))
    (directory / "spec.md").write_text(f"# T\n\n## Decision history\n\n{entries}", encoding="utf-8")
    assert dash.build_project(directory)["decisions"] == [f"decision {number}" for number in range(9, 3, -1)]


def test_a_version_3_project_without_optional_parts_shows_empty_fields(tmp_path: Path) -> None:
    row = dash.build_project(write_project(tmp_path, schema_version=3, status="DONE"))
    assert row["has_handoff"] is False
    assert row["worktrees"] == []
    assert (row["objective"], row["in_scope"], row["decisions"]) == ("", "", [])
    assert row["review"] == "not_required" and row["review_required"] is False
    assert row["recorded_session"] is None
    assert waiting(row) is False


@pytest.mark.parametrize("text", ["{not json", "[]", '{"status": 3}', '{"status": ""}', "{}"])
def test_an_unreadable_project_is_a_row_not_an_error(tmp_path: Path, text: str) -> None:
    directory = tmp_path / OTHER
    directory.mkdir()
    (directory / "project.json").write_text(text, encoding="utf-8")
    row = dash.build_project(directory)
    assert row["unreadable"] is True
    assert (row["status"], row["title"], row["needs_you"], row["sessions"]) == ("UNREADABLE", OTHER, False, [])


def test_a_directory_without_project_json_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / OTHER).mkdir()
    assert dash.build_project(tmp_path / OTHER)["status"] == "UNREADABLE"


def test_malformed_fields_do_not_break_a_row(tmp_path: Path) -> None:
    directory = write_project(
        tmp_path,
        title=None,
        working_directory=None,
        tasks=["junk", task("T01", effect="x", evidence="y", authorization="z")],
        review="nope",
        worktrees="nope",
        created=3,
    )
    row = dash.build_project(directory)
    assert row["title"] == ID and row["working_directory"] == "" and row["repo"] == ""
    assert row["created"] == ""
    assert row["tasks"][0]["effect"] == "" and row["tasks"][0]["evidence"] == 0
    assert row["review"] == "not_required"
    assert row["worktrees"] == []


def test_tasks_that_are_not_a_list_count_as_none(tmp_path: Path) -> None:
    assert dash.build_project(write_project(tmp_path, tasks="nope"))["task_total"] == 0


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"status": "BLOCKED"}, True),
        ({"status": "ALIGNING"}, False),
        ({"review": {"required": True, "status": "pending"}}, True),
        ({"review": {"required": True, "status": "accepted"}}, False),
        (
            {"tasks": [task(status="RUNNING", authorization={"required": True, "status": "pending"})]},
            True,
        ),
        (
            {"tasks": [task(status="RUNNING", authorization={"required": True, "status": "explicit"})]},
            False,
        ),
        (
            {"tasks": [task(status="TODO", authorization={"required": True, "status": "pending"})]},
            False,
        ),
        ({"status": "DONE", "review": {"required": True, "status": "pending"}}, False),
        ({"status": "CANCELLED"}, False),
    ],
)
def test_who_waits_on_the_user(tmp_path: Path, state: dict[str, Any], expected: bool) -> None:
    assert waiting(dash.build_project(write_project(tmp_path, **state))) is expected


@pytest.mark.parametrize(("label", "expected"), [("waiting", True), ("blocked", True), ("working", False)])
def test_a_waiting_session_label_needs_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, expected: bool
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    project = lib.allocate_project(
        tmp_path / "workspace", title="Waiting", working_directory=tmp_path, create_root=True
    )
    reason = {} if label == "working" else {"stop_reason": "user-decision"}
    workflow(project, "checkpoint", request(project, "cp", continuation={"next": "Do it", "session": label, **reason}))
    row = dash.build_project(project)
    assert row["has_handoff"] is True
    assert row["session"] == label
    assert row["next"] == "Do it"
    assert waiting(row) is expected


def test_the_checkpoint_fields_and_recorded_session_reach_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.chdir(tmp_path)
    project = lib.allocate_project(
        tmp_path / "workspace", title="Recorded", working_directory=tmp_path, create_root=True
    )
    workflow(
        project,
        "checkpoint",
        request(project, "cp", continuation={"next": "Go", "questions": "Which?", "do_not": "Never push"}),
    )
    row = dash.build_project(project)
    assert (row["questions"], row["do_not"]) == ("Which?", "Never push")
    assert row["recorded_session"] == {"host": "claude", "id": "abc-123", "cwd": os.getcwd()}


def test_a_malformed_checkpoint_is_no_handoff(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    broken = "<!-- research-checkpoint-v1\n{broken\n-->\n# Continuation\n"
    (directory / "handoff.md").write_text(broken, encoding="utf-8")
    assert dash.build_project(directory)["has_handoff"] is False


def test_a_checkpoint_with_a_non_object_session_has_no_recorded_session(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    metadata = {"version": 1, "revision": 0, "sources": {}, "continuation": {"next": "x"}, "git": {}}
    (directory / "handoff.md").write_text(
        "<!-- research-checkpoint-v1\n" + json.dumps(metadata) + "\n-->\n# Continuation\n", encoding="utf-8"
    )
    row = dash.build_project(directory)
    assert row["has_handoff"] is True and row["recorded_session"] is None


def test_an_unreadable_spec_is_an_empty_one(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    (directory / "spec.md").write_bytes(b"\xff\xfe invalid")
    assert dash.build_project(directory)["objective"] == ""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/git/agents", "agents"),
        ("/git/agents.worktrees/2026-10-12-003", "agents"),
        ("/", "/"),
        ("", ""),
    ],
)
def test_the_repository_name(path: str, expected: str) -> None:
    assert dash._repository(path) == expected


# --- the root ------------------------------------------------------------------------------------


def test_only_canonical_project_directories_are_listed_newest_first(tmp_path: Path) -> None:
    for name in (OTHER, ID, "2026-10-12-001"):
        write_project(tmp_path, name)
    (tmp_path / "memory").mkdir()
    (tmp_path / "2026-10-12-9").mkdir()
    (tmp_path / "2026-10-12-004").write_text("a file", encoding="utf-8")
    assert [path.name for path in dash.project_directories(tmp_path)] == [ID, "2026-10-12-001", OTHER]
    model = dash.build_model(tmp_path)
    assert model["root"] == str(tmp_path)
    assert [row["id"] for row in model["projects"]] == [ID, "2026-10-12-001", OTHER]


def test_a_root_that_cannot_be_listed_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(lib.WorkspaceError, match="cannot list"):
        dash.project_directories(tmp_path / "missing")


def test_an_empty_root_has_no_projects(tmp_path: Path) -> None:
    assert dash.build_model(tmp_path)["projects"] == []


# --- the fingerprint -----------------------------------------------------------------------------


def bump(path: Path) -> None:
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))


def test_the_fingerprint_moves_with_each_watched_file(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    (directory / "spec.md").write_text("spec", encoding="utf-8")
    (directory / "handoff.md").write_text("handoff", encoding="utf-8")
    before = dash.fingerprint(tmp_path)
    assert dash.fingerprint(tmp_path) == before
    for name in dash.WATCHED_FILES:
        bump(directory / name)
        after = dash.fingerprint(tmp_path)
        assert after != before, name
        before = after


def test_the_fingerprint_moves_when_a_size_or_a_project_changes(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    before = dash.fingerprint(tmp_path)
    (directory / "spec.md").write_text("new file", encoding="utf-8")
    after_spec = dash.fingerprint(tmp_path)
    assert after_spec != before
    write_project(tmp_path, OTHER)
    assert dash.fingerprint(tmp_path) != after_spec


def test_the_fingerprint_ignores_files_the_page_does_not_read(tmp_path: Path) -> None:
    directory = write_project(tmp_path)
    before = dash.fingerprint(tmp_path)
    (directory / "evidence.md").write_text("evidence", encoding="utf-8")
    (tmp_path / "INDEX.md").write_text("index", encoding="utf-8")
    assert dash.fingerprint(tmp_path) == before


def test_a_missing_watched_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ID).mkdir()
    assert dash.fingerprint(tmp_path) == dash.fingerprint(tmp_path)


# --- the session index ---------------------------------------------------------------------------


def transcript(claude: Path, folder: str, session: str, text: str, *, when: int | None = None) -> Path:
    directory = claude / "projects" / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text(text, encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))
    return path


def mentions(identity: str, times: int, cwd: str = "/git/agents") -> str:
    head = json.dumps({"cwd": cwd, "type": "attachment"}, separators=(",", ":")) + "\n"
    return head + "".join(f"/root/workspace/{identity} step\n" for _ in range(times))


def command(identity: str, verb: str = "workflow") -> str:
    return f"research-project {verb} /root/workspace/{identity} resume -\n"


def test_a_transcript_that_ran_a_write_command_qualifies(tmp_path: Path) -> None:
    transcript(tmp_path, "-git-agents", "s1", mentions(ID, 3) + command(ID))
    found = dash.SessionIndex(tmp_path).find()
    assert [(item["id"], item["cwd"], item["commanded"], item["mentions"]) for item in found[ID]] == [
        ("s1", "/git/agents", True, 4)
    ]
    assert found[ID][0]["exact"] is False


@pytest.mark.parametrize("verb", ["context", "read", "list-projects"])
def test_a_read_command_proves_nothing(tmp_path: Path, verb: str) -> None:
    text = mentions(ID, 3) + mentions(OTHER, 20) + command(ID, verb)
    transcript(tmp_path, "-git", "s1", text)
    found = dash.SessionIndex(tmp_path).find()
    assert ID not in found
    assert OTHER in found


def test_the_share_of_mentions_qualifies_without_a_command(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "s1", mentions(ID, 25) + mentions(OTHER, 100))
    assert [item["commanded"] for item in dash.SessionIndex(tmp_path).find()[ID]] == [False]


def test_a_listing_is_not_work(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "s1", mentions(ID, 24) + mentions(OTHER, 100))
    assert ID not in dash.SessionIndex(tmp_path).find()


def test_too_few_mentions_never_qualify(tmp_path: Path) -> None:
    # The command line is itself a mention: 1 + 1 is 2, below the minimum of 3.
    transcript(tmp_path, "-git", "s1", mentions(ID, 1) + command(ID, "update"))
    assert dash.SessionIndex(tmp_path).find() == {}
    transcript(tmp_path, "-git", "s2", mentions(ID, 2) + command(ID, "update"))
    assert [item["id"] for item in dash.SessionIndex(tmp_path).find()[ID]] == ["s2"]


def test_the_session_that_moved_last_comes_first_and_the_list_is_capped(tmp_path: Path) -> None:
    for number, when in enumerate([1_000, 4_000, 2_000, 5_000, 3_000]):
        transcript(tmp_path, "-git", f"s{number}", mentions(ID, 5 + number), when=when)
    assert [item["id"] for item in dash.SessionIndex(tmp_path).find()[ID]] == ["s3", "s1", "s4"]


def test_the_working_directory_comes_from_the_transcript(tmp_path: Path) -> None:
    transcript(tmp_path, "-git-a", "s1", mentions(ID, 5, cwd="/git/a b"))
    transcript(tmp_path, "-git-b", "s2", "no directory here\n" + "".join(f"workspace/{ID}\n" for _ in range(5)))
    cwd = {item["id"]: item["cwd"] for item in dash.SessionIndex(tmp_path).find()[ID]}
    assert cwd == {"s1": "/git/a b", "s2": ""}


def test_a_transcript_with_no_project_is_skipped(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "s1", "hello\n")
    assert dash.SessionIndex(tmp_path).find() == {}


def test_a_transcript_that_cannot_be_read_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "projects" / "-git" / "broken.jsonl").mkdir(parents=True)
    transcript(tmp_path, "-git", "s1", mentions(ID, 4))
    assert [item["id"] for item in dash.SessionIndex(tmp_path).find()[ID]] == ["s1"]


def test_a_missing_claude_directory_yields_no_sessions(tmp_path: Path) -> None:
    assert dash.SessionIndex(tmp_path / "nowhere").find() == {}


def test_a_transcript_is_read_again_only_when_it_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = transcript(tmp_path, "-git", "s1", mentions(ID, 4), when=1_000)
    index = dash.SessionIndex(tmp_path)
    reads: list[Path] = []
    original = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    index.find()
    index.find()
    assert reads == [path]
    path.write_text(mentions(ID, 9), encoding="utf-8")
    assert index.find()[ID][0]["mentions"] == 9
    assert reads == [path, path]


def test_running_sessions_and_prompts_label_a_session(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "s1", mentions(ID, 5))
    transcript(tmp_path, "-git", "s2", mentions(ID, 5), when=1)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text(json.dumps({"sessionId": "s1", "status": "busy"}), encoding="utf-8")
    (sessions / "2.json").write_text(json.dumps({"sessionId": "s2"}), encoding="utf-8")
    (sessions / "3.json").write_text("{broken", encoding="utf-8")
    (sessions / "4.json").write_text(json.dumps({"status": "idle"}), encoding="utf-8")
    (sessions / "5.json").write_text("[]", encoding="utf-8")
    history = [
        {"sessionId": "s1", "timestamp": 10, "display": "older"},
        {"sessionId": "s1", "timestamp": 20, "display": "x" * 500},
        {"sessionId": "s1", "timestamp": 5, "display": "oldest"},
        {"sessionId": "s2", "timestamp": 7, "display": ["not text"]},
        {"timestamp": 1},
        {"sessionId": "s3", "timestamp": "late"},
    ]
    (tmp_path / "history.jsonl").write_text(
        "\n".join(json.dumps(line) for line in history) + "\nnot json\n", encoding="utf-8"
    )
    found = {item["id"]: item for item in dash.SessionIndex(tmp_path).find()[ID]}
    assert (found["s1"]["running"], found["s1"]["prompt"]) == ("busy", "x" * dash.PROMPT_CHARS)
    assert (found["s2"]["running"], found["s2"]["prompt"]) == ("running", "")


def test_a_missing_history_means_no_prompts(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "s1", mentions(ID, 5))
    assert dash.SessionIndex(tmp_path).find()[ID][0]["prompt"] == ""


def test_an_undecodable_working_directory_is_replaced_not_fatal(tmp_path: Path) -> None:
    path = transcript(tmp_path, "-git", "s1", "")
    line = b"workspace/" + ID.encode() + b"\n"
    path.write_bytes(b'{"cwd":"/git/\xff"}\n' + line * 5)
    assert dash.SessionIndex(tmp_path).find()[ID][0]["cwd"] == "/git/�"


# --- pi sessions ---------------------------------------------------------------------------------


def pi_session(
    root: Path,
    identity: str,
    session: str,
    mentions: int,
    *,
    when: int | None = None,
    cwd: object = "/git/agents",
    prompt: object = "the prompt",
    command: str = "",
) -> Path:
    directory = root / "sessions" / "--git-agents--"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2026-10-19T10-00-00-000Z_{session}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session,
                "timestamp": "2026-10-19T10:00:00.000Z",
                "cwd": cwd,
            }
        )
    ]
    if prompt is not None:
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "id": "a",
                    "parentId": None,
                    "timestamp": "2026-10-19T10:00:01.000Z",
                    "message": {"role": "user", "content": prompt, "timestamp": 1},
                }
            )
        )
    lines.extend(
        json.dumps(
            {
                "type": "message",
                "id": f"m{number}",
                "parentId": "a",
                "timestamp": "2026-10-19T10:00:02.000Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": f"/root/workspace/{identity}\n"}],
                    "timestamp": 2,
                },
            }
        )
        for number in range(mentions)
    )
    if command:
        lines.append(command)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))
    return path


def test_pi_sessions_are_found_with_host_and_prompt(tmp_path: Path) -> None:
    pi_session(tmp_path, ID, "p1", 4)
    found = dash.PiSessionIndex(tmp_path).find()[ID]
    observed = [
        (item["id"], item["cwd"], item["host"], item["prompt"], item["mentions"], item["exact"])
        for item in found
    ]
    assert observed == [("p1", "/git/agents", "pi", "the prompt", 4, False)]


def test_a_pi_session_that_ran_a_write_command_qualifies(tmp_path: Path) -> None:
    pi_session(tmp_path, ID, "p1", 2, command=command(ID))
    found = dash.PiSessionIndex(tmp_path).find()[ID]
    assert [(item["commanded"], item["mentions"]) for item in found] == [(True, 3)]


def test_a_pi_listing_is_not_work(tmp_path: Path) -> None:
    path = pi_session(tmp_path, ID, "p1", 24)
    path.write_text(
        path.read_text() + "".join(f"/root/workspace/{OTHER}\n" for _ in range(100)),
        encoding="utf-8",
    )
    assert ID not in dash.PiSessionIndex(tmp_path).find()


def test_pi_sessions_sort_by_time_and_are_capped(tmp_path: Path) -> None:
    for number, when in enumerate([1_000, 4_000, 2_000, 5_000, 3_000]):
        pi_session(tmp_path, ID, f"p{number}", 5 + number, when=when)
    assert [item["id"] for item in dash.PiSessionIndex(tmp_path).find()[ID]] == ["p3", "p1", "p4"]


def test_pi_session_without_a_usable_header_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "sessions" / "--git--"
    directory.mkdir(parents=True)
    (directory / "empty.jsonl").write_bytes(b"")
    (directory / "bad.jsonl").write_text("not json\n", encoding="utf-8")
    (directory / "list.jsonl").write_text("[]\n", encoding="utf-8")
    (directory / "message.jsonl").write_text(json.dumps({"type": "message"}) + "\n", encoding="utf-8")
    (directory / "no-id.jsonl").write_text(json.dumps({"type": "session", "cwd": "/git"}) + "\n", encoding="utf-8")
    (directory / "non-text-id.jsonl").write_text(
        json.dumps({"type": "session", "id": 1, "cwd": "/git"}) + "\n", encoding="utf-8"
    )
    (directory / "quiet.jsonl").write_text(
        json.dumps({"type": "session", "id": "quiet", "cwd": "/git"}) + "\n", encoding="utf-8"
    )
    assert dash.PiSessionIndex(tmp_path).find() == {}


def test_a_pi_header_without_a_text_cwd_keeps_the_row(tmp_path: Path) -> None:
    pi_session(tmp_path, ID, "p1", 4, cwd=5)
    assert dash.PiSessionIndex(tmp_path).find()[ID][0]["cwd"] == ""


def test_a_pi_directory_where_a_transcript_should_be_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "sessions" / "--git--" / "bad.jsonl").mkdir(parents=True)
    assert dash.PiSessionIndex(tmp_path).find() == {}


def test_a_missing_pi_directory_yields_no_sessions(tmp_path: Path) -> None:
    assert dash.PiSessionIndex(tmp_path / "nowhere").find() == {}


def test_a_pi_transcript_is_read_again_only_when_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = pi_session(tmp_path, ID, "p1", 4, when=1_000)
    index = dash.PiSessionIndex(tmp_path)
    reads: list[Path] = []
    original = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    index.find()
    index.find()
    assert reads == [path]
    path.write_text(path.read_text() + "extra\n", encoding="utf-8")
    index.find()
    assert reads == [path, path]


def test_the_last_pi_user_message_labels_the_session(tmp_path: Path) -> None:
    path = pi_session(tmp_path, ID, "p1", 4, prompt=None)
    additions = [
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "first"}]}},
        {"type": "message", "message": {"role": "user", "content": [
            {"type": "text", "text": "second"}, {"type": "text", "text": "part"}
        ]}},
    ]
    path.write_text(
        path.read_text() + "not json\n" + "[]\n" + "".join(json.dumps(item) + "\n" for item in additions),
        encoding="utf-8",
    )
    assert dash.PiSessionIndex(tmp_path).find()[ID][0]["prompt"] == "second part"


def test_a_pi_session_without_user_text_has_no_prompt(tmp_path: Path) -> None:
    path = pi_session(tmp_path, ID, "p1", 4, prompt=None)
    additions = [
        {"type": "message", "message": {"role": "assistant", "content": "ignored"}},
        {"type": "message", "message": "not an object"},
        {"type": "message", "message": {"role": "user", "content": 5}},
        {"type": "message", "message": {"role": "user", "content": [{"type": "image"}]}},
        {"type": "message", "message": {"role": "user", "content": "   "}},
    ]
    path.write_text(
        path.read_text() + "".join(json.dumps(item) + "\n" for item in additions), encoding="utf-8"
    )
    assert dash.PiSessionIndex(tmp_path).find()[ID][0]["prompt"] == ""


def test_a_pi_session_with_too_few_mentions_is_skipped(tmp_path: Path) -> None:
    pi_session(tmp_path, ID, "p1", 2)
    assert dash.PiSessionIndex(tmp_path).find() == {}


def test_claude_and_pi_sessions_merge_with_resume_commands(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    pi = tmp_path / "pi"
    transcript(claude, "-git", "c1", mentions(ID, 5), when=2_000)
    pi_session(pi, ID, "p1", 5, when=1_000)
    sessions = dash.attach_sessions([row(ID)], dash.SessionIndex(claude), dash.PiSessionIndex(pi))[0]["sessions"]
    assert [(item["host"], item["id"]) for item in sessions] == [("claude", "c1"), ("pi", "p1")]
    assert [item["resume"] for item in sessions] == [
        "cd /git/agents && claude --resume c1",
        "cd /git/agents && pi --session p1",
    ]


def test_a_recorded_pi_session_uses_the_pi_resume_command(tmp_path: Path) -> None:
    projects = [row(ID, recorded_session={"host": "pi", "id": "p1", "cwd": "/git/agents"})]
    sessions = dash.attach_sessions(projects, dash.SessionIndex(tmp_path / "claude"))[0]["sessions"]
    assert [(item["host"], item["exact"], item["resume"]) for item in sessions] == [
        ("pi", True, "cd /git/agents && pi --session p1")
    ]


def test_a_recorded_pi_session_is_listed_once(tmp_path: Path) -> None:
    pi_session(tmp_path, ID, "p1", 5)
    projects = [row(ID, recorded_session={"host": "pi", "id": "p1", "cwd": "/git/agents"})]
    sessions = dash.attach_sessions(
        projects, dash.SessionIndex(tmp_path / "claude"), dash.PiSessionIndex(tmp_path)
    )[0]["sessions"]
    assert [(item["host"], item["id"], item["exact"]) for item in sessions] == [("pi", "p1", True)]


# --- sessions on the projects --------------------------------------------------------------------


def row(identity: str, **fields: Any) -> dict[str, Any]:
    return {"id": identity, "recorded_session": None, "sessions": [], **fields}


def test_projects_without_sessions_get_an_empty_list_and_the_input_is_untouched(tmp_path: Path) -> None:
    projects = [row(ID)]
    result = dash.attach_sessions(projects, dash.SessionIndex(tmp_path))
    assert result[0]["sessions"] == []
    assert result[0] is not projects[0]


def test_a_recorded_session_comes_first_and_exact(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "found", mentions(ID, 5), when=9_000)
    transcript(tmp_path, "-git", "recorded", mentions(ID, 5), when=1_000)
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"sessionId": "recorded", "timestamp": 1, "display": "the prompt"}) + "\n", encoding="utf-8"
    )
    projects = [row(ID, recorded_session={"host": "claude", "id": "recorded", "cwd": "/git/agents"})]
    sessions = dash.attach_sessions(projects, dash.SessionIndex(tmp_path))[0]["sessions"]
    assert [(item["id"], item["exact"]) for item in sessions] == [("recorded", True), ("found", False)]
    assert (sessions[0]["cwd"], sessions[0]["prompt"], sessions[0]["running"]) == ("/git/agents", "the prompt", "")


def test_a_recorded_session_is_listed_once_even_when_the_search_finds_it(tmp_path: Path) -> None:
    transcript(tmp_path, "-git", "same", mentions(ID, 5))
    projects = [row(ID, recorded_session={"id": "same", "cwd": "/git/agents"})]
    sessions = dash.attach_sessions(projects, dash.SessionIndex(tmp_path))[0]["sessions"]
    assert [(item["id"], item["exact"]) for item in sessions] == [("same", True)]


@pytest.mark.parametrize("recorded", [{"host": "claude"}, {"id": ""}, None])
def test_a_recorded_session_without_an_id_is_ignored(tmp_path: Path, recorded: dict[str, Any] | None) -> None:
    projects = [row(ID, recorded_session=recorded)]
    assert dash.attach_sessions(projects, dash.SessionIndex(tmp_path))[0]["sessions"] == []


def test_a_recorded_session_with_a_non_text_directory_has_none(tmp_path: Path) -> None:
    projects = [row(ID, recorded_session={"id": "x", "cwd": 5})]
    assert dash.attach_sessions(projects, dash.SessionIndex(tmp_path))[0]["sessions"][0]["cwd"] == ""
