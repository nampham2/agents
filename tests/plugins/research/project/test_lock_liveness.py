"""P3: lock liveness (`lock_status`), the busy-lock refusal, and the guarded `research-project unlock`.

A lock directory holds `owner.json` naming the pid, the host and an opaque start token of the
process that took it. A dead pid, or a live pid whose start token differs, is stale; anything the
tool cannot judge (legacy owner, foreign host, unreadable token) is unknown and never removed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manage_workspace
import pytest
import workspace_lib
from workspace_lib import DirectoryLock, WorkspaceConflict, lock_status, own_start_token, process_start_token

PROJECT = "2026-01-01-001"


@pytest.fixture(autouse=True)
def _no_env_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(workspace_lib.WORKSPACE_ROOT_ENV_VAR, raising=False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / PROJECT).mkdir(parents=True)
    (workspace / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    return workspace.resolve()


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _owner(**overrides: Any) -> dict[str, Any]:
    owner: dict[str, Any] = {
        "pid": os.getpid(),
        "created": "2026-01-01T00:00:00+00:00",
        "host": socket.gethostname(),
        "started": own_start_token(),
    }
    owner.update(overrides)
    return {key: value for key, value in owner.items() if value is not None}


def _make_lock(path: Path, owner: "dict[str, Any] | None") -> Path:
    path.mkdir(parents=True)
    if owner is not None:
        (path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    return path


def _stale_owner() -> dict[str, Any]:
    return _owner(pid=_dead_pid(), started="token")


def _unlock(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str]:
    with patch.object(sys, "argv", ["research-project", "unlock", *argv]):
        code = manage_workspace.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload, captured.err


# --- start token ---------------------------------------------------------------------------------


def test_own_start_token_is_read_once_and_cached() -> None:
    with patch.object(workspace_lib, "_OWN_START_TOKEN", {}):
        with patch.object(workspace_lib, "process_start_token", return_value="linux:7") as reader:
            assert own_start_token() == "linux:7"
            assert own_start_token() == "linux:7"
    assert reader.call_count == 1


def test_own_token_is_readable_on_this_platform() -> None:
    token = process_start_token(os.getpid())
    if sys.platform == "darwin":
        assert token.startswith("darwin:")
    elif sys.platform.startswith("linux"):
        assert token.startswith("linux:")


@pytest.mark.skipif(sys.platform != "darwin", reason="the ps lstart token is macOS-only")
def test_darwin_token_is_stable_across_tz_and_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = os.getpid()
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")
    first = process_start_token(pid)
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert process_start_token(pid) == first != ""


def test_darwin_token_pins_ps_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "Thu Nov  6 10:00:00 2026\n", "")

    with patch.object(workspace_lib.sys, "platform", "darwin"), patch.object(workspace_lib.subprocess, "run", fake_run):
        assert process_start_token(42) == "darwin:Thu Nov  6 10:00:00 2026"
    assert seen["command"] == ["ps", "-o", "lstart=", "-p", "42"]
    assert (seen["env"]["TZ"], seen["env"]["LC_ALL"]) == ("UTC", "C")


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "no such process"),
        subprocess.CompletedProcess([], 0, "   \n", ""),
        OSError("no ps"),
        subprocess.TimeoutExpired("ps", 5),
    ],
)
def test_darwin_token_unreadable_is_empty(result: object) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> object:
        if isinstance(result, BaseException):
            raise result
        return result

    with patch.object(workspace_lib.sys, "platform", "darwin"), patch.object(workspace_lib.subprocess, "run", fake_run):
        assert process_start_token(42) == ""


def test_linux_token_reads_field_22_after_the_last_paren(tmp_path: Path) -> None:
    (tmp_path / "42").mkdir()
    # A command name with spaces and parentheses must not shift the fields.
    fields = " ".join(str(number) for number in range(3, 30))
    (tmp_path / "42" / "stat").write_text(f"42 (odd) name) {fields}\n", encoding="utf-8")
    with patch.object(workspace_lib.sys, "platform", "linux"), patch.object(workspace_lib, "PROC_ROOT", tmp_path):
        assert process_start_token(42) == "linux:22"
        assert process_start_token(43) == ""
    (tmp_path / "44").mkdir()
    (tmp_path / "44" / "stat").write_text("44 (short) S 1\n", encoding="utf-8")
    with patch.object(workspace_lib.sys, "platform", "linux"), patch.object(workspace_lib, "PROC_ROOT", tmp_path):
        assert process_start_token(44) == ""


def test_other_platform_token_is_empty() -> None:
    with patch.object(workspace_lib.sys, "platform", "win32"):
        assert process_start_token(42) == ""


# --- owner.json ----------------------------------------------------------------------------------


def test_new_lock_records_host_and_start_token(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with DirectoryLock(path):
        owner = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        assert lock_status(path) == ("live", "owner alive")
    assert set(owner) == {"pid", "created", "host", "started"}
    assert (owner["pid"], owner["host"], owner["started"]) == (os.getpid(), socket.gethostname(), own_start_token())
    assert not path.exists()


# --- lock_status ---------------------------------------------------------------------------------


def test_dead_pid_is_stale(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _stale_owner())
    assert lock_status(path) == ("stale", "dead pid")


def test_reused_pid_with_patched_token_is_stale(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(started="linux:1"))
    with patch.object(workspace_lib, "process_start_token", return_value="linux:2"):
        assert lock_status(path) == ("stale", "pid reused")


def test_reused_pid_with_real_token_is_stale(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(started="darwin:never"))
    if own_start_token():
        assert lock_status(path) == ("stale", "pid reused")


def test_live_owner(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(started="linux:1"))
    with patch.object(workspace_lib, "process_start_token", return_value="linux:1"):
        assert lock_status(path) == ("live", "owner alive")


def test_permission_error_counts_as_alive(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(started="linux:1"))
    with (
        patch.object(workspace_lib.os, "kill", side_effect=PermissionError()),
        patch.object(workspace_lib, "process_start_token", return_value="linux:1"),
    ):
        assert lock_status(path) == ("live", "owner alive")


@pytest.mark.parametrize(
    ("owner", "reason"),
    [
        (None, "no owner"),
        ({"pid": 1, "created": "held"}, "legacy owner"),
        (_owner(started=""), "no start token"),
        ({"pid": 1, "created": "x", "host": "h"}, "no start token"),
        (_owner(host="elsewhere.example"), "foreign host elsewhere.example"),
        (_owner(pid="12"), "unreadable owner"),
        (_owner(pid=0), "unreadable owner"),
        (_owner(pid=True), "unreadable owner"),
    ],
)
def test_unknown_owners(tmp_path: Path, owner: "dict[str, Any] | None", reason: str) -> None:
    path = _make_lock(tmp_path / "x.lock", owner)
    assert lock_status(path) == ("unknown", reason)


def test_malformed_owner_is_unknown(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", None)
    (path / "owner.json").write_text("{not json", encoding="utf-8")
    assert lock_status(path) == ("unknown", "unreadable owner")
    (path / "owner.json").write_text("[1]", encoding="utf-8")
    assert lock_status(path) == ("unknown", "unreadable owner")


def test_unreadable_token_is_unknown(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(started="linux:1"))
    with patch.object(workspace_lib, "process_start_token", return_value=""):
        assert lock_status(path) == ("unknown", "start token unreadable")


def test_failed_pid_check_is_unknown(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "x.lock", _owner(pid=2**62))
    assert lock_status(path) == ("unknown", "pid check failed")


# --- busy-lock refusal ---------------------------------------------------------------------------


def test_busy_refusal_names_status_reason_and_unlock_command(root: Path) -> None:
    path = _make_lock(root / PROJECT / ".project.lock", _stale_owner())
    with pytest.raises(WorkspaceConflict) as caught:
        with DirectoryLock(path, timeout=0):
            pass
    message = str(caught.value)
    assert "lock is busy" in message
    assert "stale (dead pid)" in message
    assert f"research-project unlock {path} --workspace-root {root}" in message


def test_busy_refusal_falls_back_to_env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(workspace_lib.WORKSPACE_ROOT_ENV_VAR, "/somewhere/root")
    path = _make_lock(tmp_path / "loose" / "x.lock", {"pid": 1, "created": "held"})
    with pytest.raises(WorkspaceConflict) as caught:
        with DirectoryLock(path, timeout=0):
            pass
    message = str(caught.value)
    assert "unknown (legacy owner)" in message
    assert f"research-project unlock {path} --workspace-root /somewhere/root" in message


def test_busy_refusal_omits_root_when_none_is_known(tmp_path: Path) -> None:
    path = _make_lock(tmp_path / "loose" / "x.lock", _owner())
    with pytest.raises(WorkspaceConflict) as caught:
        with DirectoryLock(path, timeout=0):
            pass
    message = str(caught.value)
    assert "live (owner alive)" in message
    assert f"research-project unlock {path}" in message
    assert "--workspace-root" not in message


# --- unlock --------------------------------------------------------------------------------------


def test_unlock_removes_a_stale_lock(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / PROJECT / "project.json").write_text("{}", encoding="utf-8")
    path = _make_lock(root / PROJECT / ".project.lock", _stale_owner())
    code, payload, _ = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 0
    assert payload == {"status": "stale", "reason": "dead pid", "removed": True}
    assert not path.exists()
    assert sorted(child.name for child in (root / PROJECT).iterdir()) == ["project.json"]


def test_unlock_reads_root_from_env(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(workspace_lib.WORKSPACE_ROOT_ENV_VAR, str(root))
    path = _make_lock(root / ".memory.lock", _stale_owner())
    code, payload, _ = _unlock([str(path)], capsys)
    assert (code, payload["removed"]) == (0, True)


def test_unlock_without_root_refuses(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".memory.lock", _stale_owner())
    code, _, err = _unlock([str(path)], capsys)
    assert code == 1
    assert "no workspace root" in err
    assert path.is_dir()


OWNERS: dict[str, Any] = {
    "live": lambda: _owner(),
    "legacy": lambda: {"pid": 1, "created": "held"},
    "foreign": lambda: _owner(pid=_dead_pid(), host="elsewhere.example"),
    "none": lambda: None,
}


@pytest.mark.parametrize(
    ("kind", "status", "reason"),
    [
        ("live", "live", "owner alive"),
        ("legacy", "unknown", "legacy owner"),
        ("foreign", "unknown", "foreign host elsewhere.example"),
        ("none", "unknown", "no owner"),
    ],
)
def test_unlock_refuses_a_lock_that_is_not_stale(
    root: Path, capsys: pytest.CaptureFixture[str], kind: str, status: str, reason: str
) -> None:
    path = _make_lock(root / ".index.lock", OWNERS[kind]())
    before = sorted(child.name for child in path.iterdir())
    code, payload, _ = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert payload == {"status": status, "reason": reason, "removed": False}
    assert sorted(child.name for child in path.iterdir()) == before


def test_unlock_refuses_outside_root(root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(tmp_path / "outside" / "x.lock", _stale_owner())
    code, _, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "outside the workspace root" in err
    assert path.is_dir()


def test_unlock_refuses_the_root_itself(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _unlock([str(root), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "outside the workspace root" in err


def test_unlock_refuses_symlink_escape(root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _make_lock(tmp_path / "outside" / "x.lock", _stale_owner())
    link = root / "escape.lock"
    link.symlink_to(target)
    code, _, err = _unlock([str(link), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "outside the workspace root" in err
    assert target.is_dir()


def test_unlock_refuses_a_name_without_lock_suffix(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / PROJECT / "notes", _stale_owner())
    code, _, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "does not end in .lock" in err
    assert path.is_dir()


def test_unlock_refuses_extra_files(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())
    (path / "owner.json.tmp").write_text("{}", encoding="utf-8")
    code, _, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "holds more than owner.json" in err
    assert (path / "owner.json").is_file()


def test_unlock_refuses_a_missing_or_non_directory_path(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _unlock([str(root / "gone.lock"), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "not a lock directory" in err
    (root / "file.lock").write_text("", encoding="utf-8")
    code, _, err = _unlock([str(root / "file.lock"), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "not a lock directory" in err


def test_unlock_writes_no_invocation_line(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / PROJECT / "project.json").write_text("{}", encoding="utf-8")
    path = _make_lock(root / PROJECT / ".project.lock", _owner())
    _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert not (root / PROJECT / "invocations.jsonl").exists()


def _rename_then(action: Any) -> Any:
    real_rename = os.rename

    def rename(source: Any, destination: Any) -> None:
        real_rename(source, destination)
        action(Path(source), Path(destination))

    return rename


def test_identity_mismatch_is_restored(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())
    replacement = _owner()

    def swap(lock: Path, tombstone: Path) -> None:
        (tombstone / "owner.json").write_text(json.dumps(replacement), encoding="utf-8")

    with patch.object(workspace_lib.os, "rename", _rename_then(swap)):
        code, payload, _ = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert payload["removed"] is False
    assert payload["status"] == "live"
    assert "tombstone" not in payload
    assert json.loads((path / "owner.json").read_text(encoding="utf-8")) == replacement
    assert [child.name for child in root.iterdir() if "tombstone" in child.name] == []


def test_identical_but_no_longer_stale_is_restored(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _owner(started="linux:1"))
    tokens = iter(["linux:2", "linux:1", "linux:1"])
    with patch.object(workspace_lib, "process_start_token", side_effect=lambda pid: next(tokens)):
        code, payload, _ = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert (code, payload["removed"]) == (1, False)
    assert (path / "owner.json").is_file()


def test_restore_onto_occupied_path_keeps_the_tombstone(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())

    def swap_and_occupy(lock: Path, tombstone: Path) -> None:
        (tombstone / "owner.json").write_text(json.dumps(_owner()), encoding="utf-8")
        lock.mkdir()

    with patch.object(workspace_lib.os, "rename", _rename_then(swap_and_occupy)):
        code, payload, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert payload["removed"] is False
    tombstone = Path(payload["tombstone"])
    assert (tombstone / "owner.json").is_file()
    assert payload["lock"] == str(path)
    assert str(tombstone) in err and str(path) in err
    assert path.is_dir() and not (path / "owner.json").exists()


def test_missing_second_owner_is_not_identical(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())

    def drop(lock: Path, tombstone: Path) -> None:
        (tombstone / "owner.json").unlink()

    with patch.object(workspace_lib.os, "rename", _rename_then(drop)):
        code, payload, _ = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert (code, payload["removed"]) == (1, False)
    assert Path(payload["tombstone"]).is_dir()


def test_removal_failure_keeps_the_tombstone(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())
    with patch.object(Path, "rmdir", side_effect=OSError("busy")):
        code, payload, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert (code, payload["removed"]) == (1, False)
    assert "tombstone" in payload
    assert "busy" in err


def test_rename_failure_refuses(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _make_lock(root / ".index.lock", _stale_owner())
    with patch.object(workspace_lib.os, "rename", side_effect=OSError("denied")):
        code, _, err = _unlock([str(path), "--workspace-root", str(root)], capsys)
    assert code == 1
    assert "denied" in err
    assert (path / "owner.json").is_file()


def test_own_start_token_is_cached_per_process() -> None:
    """A forked child has its own start time, so it must not reuse its parent's cached token."""
    with patch.object(workspace_lib, "_OWN_START_TOKEN", {}):
        with patch.object(workspace_lib, "process_start_token", side_effect=lambda pid: f"linux:{pid}") as reader:
            with patch.object(workspace_lib.os, "getpid", return_value=100):
                assert own_start_token() == "linux:100"
            with patch.object(workspace_lib.os, "getpid", return_value=200):
                assert own_start_token() == "linux:200"
                assert own_start_token() == "linux:200"
    assert reader.call_count == 2
