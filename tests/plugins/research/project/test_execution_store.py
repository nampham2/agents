"""Tests for execution_store: publish_if_absent, barriers, reap, and grant registry.

Every public function is covered at 100% line coverage.  Crash-injection tests
verify that the 'created' / 'identical' / 'conflict' outcome is correct even when
publish_if_absent is interrupted at each durable-effects boundary.
"""

from __future__ import annotations

import errno
import json
import os
import time
import unittest.mock as mock
from pathlib import Path
from typing import Any, Dict

import pytest
from execution_store import (
    GrantRegistry,
    GrantRegistryError,
    StoreError,
    _atomic_write_json,
    _check_same_volume,
    _mkdir_synced,
    _RegistryLock,
    _to_store_error,
    _write_tmp,
    fsync_dir,
    publish_if_absent,
    reap_tmp_files,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _record(kind: str = "heartbeat", attempt_id: str = "att-1", extra: str = "v1") -> Dict[str, Any]:
    """Minimal valid JSON record body."""
    return {
        "kind": kind,
        "schema_version": 1,
        "attempt_id": attempt_id,
        "extra": extra,
    }


def _encode(record: Dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# _to_store_error
# ---------------------------------------------------------------------------


class TestToStoreError:
    def test_enospc_becomes_store_error(self) -> None:
        exc = OSError(errno.ENOSPC, "No space left on device")
        with pytest.raises(StoreError):
            _to_store_error(exc)

    def test_eio_becomes_store_error(self) -> None:
        exc = OSError(errno.EIO, "Input/output error")
        with pytest.raises(StoreError):
            _to_store_error(exc)

    def test_erofs_becomes_store_error(self) -> None:
        exc = OSError(errno.EROFS, "Read-only file system")
        with pytest.raises(StoreError):
            _to_store_error(exc)

    def test_other_errno_reraises_unchanged(self) -> None:
        original = OSError(errno.EACCES, "Permission denied")
        with pytest.raises(OSError) as exc_info:
            _to_store_error(original)
        assert exc_info.value is original

    def test_store_error_strerror_none(self) -> None:
        exc = OSError(errno.ENOSPC, None)
        with pytest.raises(StoreError):
            _to_store_error(exc)


# ---------------------------------------------------------------------------
# fsync_dir
# ---------------------------------------------------------------------------


class TestFsyncDir:
    def test_fsyncs_existing_directory(self, tmp_path: Path) -> None:
        fsync_dir(tmp_path)  # should not raise

    def test_eio_on_fsync_raises_store_error(self, tmp_path: Path) -> None:
        io_err = OSError(errno.EIO, "I/O error")
        with mock.patch("os.fsync", side_effect=io_err):
            with pytest.raises(StoreError):
                fsync_dir(tmp_path)

    def test_other_errno_on_fsync_reraises(self, tmp_path: Path) -> None:
        eacces = OSError(errno.EACCES, "Permission denied")
        with mock.patch("os.fsync", side_effect=eacces):
            with pytest.raises(OSError) as exc_info:
                fsync_dir(tmp_path)
            assert exc_info.value is eacces

    def test_fd_closed_even_after_fsync_error(self, tmp_path: Path) -> None:
        """os.close(fd) must be called even when fsync raises."""
        closed_fds: list = []
        real_close = os.close

        def recording_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        io_err = OSError(errno.EIO, "I/O error")
        with mock.patch("os.fsync", side_effect=io_err):
            with mock.patch("os.close", side_effect=recording_close):
                with pytest.raises(StoreError):
                    fsync_dir(tmp_path)
        assert len(closed_fds) == 1


# ---------------------------------------------------------------------------
# _mkdir_synced
# ---------------------------------------------------------------------------


class TestMkdirSynced:
    def test_existing_path_no_op(self, tmp_path: Path) -> None:
        _mkdir_synced(tmp_path)

    def test_creates_single_level(self, tmp_path: Path) -> None:
        target = tmp_path / "new_dir"
        _mkdir_synced(target)
        assert target.is_dir()

    def test_creates_nested_levels(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        _mkdir_synced(target)
        assert target.is_dir()

    def test_mkdir_error_propagates(self, tmp_path: Path) -> None:
        target = tmp_path / "new_dir"
        err = OSError(errno.EPERM, "Not permitted")
        with mock.patch("pathlib.Path.mkdir", side_effect=err):
            with pytest.raises(OSError):
                _mkdir_synced(target)

    def test_fsync_error_propagates(self, tmp_path: Path) -> None:
        target = tmp_path / "new_dir"
        io_err = OSError(errno.EIO, "I/O error")
        with mock.patch("os.fsync", side_effect=io_err):
            with pytest.raises(OSError):
                _mkdir_synced(target)


# ---------------------------------------------------------------------------
# _write_tmp
# ---------------------------------------------------------------------------


class TestWriteTmp:
    def test_creates_file_with_body(self, tmp_path: Path) -> None:
        body = b'{"kind":"test"}\n'
        tmp = _write_tmp(body, tmp_path)
        assert tmp.exists()
        assert tmp.read_bytes() == body

    def test_creates_tmp_dir_if_missing(self, tmp_path: Path) -> None:
        d = tmp_path / "sub"
        body = b"data"
        _write_tmp(body, d)
        assert d.is_dir()

    def test_short_write_raises_store_error(self, tmp_path: Path) -> None:
        # Patch os.write to return 0 (short write).
        with mock.patch("os.write", return_value=0):
            with pytest.raises(StoreError, match="short write"):
                _write_tmp(b"data", tmp_path)

    def test_enospc_on_write_raises_store_error(self, tmp_path: Path) -> None:
        err = OSError(errno.ENOSPC, "No space")
        with mock.patch("os.write", side_effect=err):
            with pytest.raises(StoreError):
                _write_tmp(b"data", tmp_path)

    def test_eio_on_fsync_raises_store_error(self, tmp_path: Path) -> None:
        err = OSError(errno.EIO, "I/O error")
        with mock.patch("os.fsync", side_effect=err):
            with pytest.raises(StoreError):
                _write_tmp(b"data", tmp_path)

    def test_tmp_file_cleaned_up_on_error(self, tmp_path: Path) -> None:
        """When _write_tmp raises, it must unlink the tmp file."""
        err = OSError(errno.ENOSPC, "No space")
        with mock.patch("os.write", side_effect=err):
            with pytest.raises(StoreError):
                _write_tmp(b"data", tmp_path)
        assert list(tmp_path.glob("*.json")) == []

    def test_empty_body(self, tmp_path: Path) -> None:
        tmp = _write_tmp(b"", tmp_path)
        assert tmp.read_bytes() == b""

    def test_close_error_during_cleanup_ignored(self, tmp_path: Path) -> None:
        """os.close raising during error cleanup must not mask the original error."""
        with mock.patch("os.write", side_effect=OSError(errno.ENOSPC, "No space")):
            with mock.patch("os.close", side_effect=OSError(errno.EBADF, "bad fd")):
                with pytest.raises(StoreError):
                    _write_tmp(b"data", tmp_path)

    def test_close_error_on_success_ignored(self, tmp_path: Path) -> None:
        """os.close raising after a successful write/fsync must not mask the result."""
        with mock.patch("os.close", side_effect=OSError(errno.EBADF, "bad fd")):
            tmp = _write_tmp(b"data", tmp_path)
        assert tmp.exists()


# ---------------------------------------------------------------------------
# _check_same_volume
# ---------------------------------------------------------------------------


class TestCheckSameVolume:
    def test_same_volume_passes(self, tmp_path: Path) -> None:
        final = tmp_path / "a" / "record.json"
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        _check_same_volume(final, tmp_dir)  # should not raise

    def test_different_volumes_raises_store_error(self, tmp_path: Path) -> None:
        final = tmp_path / "a" / "record.json"
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        # execution_store calls os.stat(str(path)) — always a string.
        # pathlib.Path.stat() calls os.stat(Path(...)) — a Path object.
        # Distinguish by type so Path.exists() still works correctly.
        real_stat = os.stat
        call_n: list = [0]

        def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, str):
                call_n[0] += 1
                m = mock.MagicMock()
                m.st_dev = 100 if call_n[0] == 1 else 200
                return m
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=fake_stat):
            with pytest.raises(StoreError, match="different volumes"):
                _check_same_volume(final, tmp_dir)

    def test_walks_up_when_parent_missing(self, tmp_path: Path) -> None:
        # final.parent (tmp_path / "deep" / "nested") doesn't exist yet.
        final = tmp_path / "deep" / "nested" / "record.json"
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        # Both on same real volume — should not raise.
        _check_same_volume(final, tmp_dir)

    def test_tmp_dir_missing_uses_parent(self, tmp_path: Path) -> None:
        # tmp_dir doesn't exist yet.
        final = tmp_path / "record.json"
        tmp_dir = tmp_path / "tmp"
        # tmp_dir does not exist; _check_same_volume should stat its parent.
        _check_same_volume(final, tmp_dir)  # both backed by same real volume

    def test_nested_tmp_ancestors_may_all_be_missing(self, tmp_path: Path) -> None:
        final = tmp_path / "execution" / "owners" / "run.json"
        tmp_dir = tmp_path / "execution" / "runtime" / "tmp"

        _check_same_volume(final, tmp_dir)

        outcome = publish_if_absent(final, _encode(_record()), tmp_dir)
        assert outcome == "created"
        assert final.is_file()

    def test_stat_os_error_propagates(self, tmp_path: Path) -> None:
        # p.exists() must succeed (so the while loop exits), then os.stat(str(p)) raises.
        # In Python 3.12, Path.exists() re-raises non-FileNotFound errors, so we must
        # NOT mock os.stat for Path-object calls (used by exists()), only for str calls.
        final = tmp_path / "record.json"
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        real_stat = os.stat

        def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, str):
                raise OSError(errno.EIO, "I/O error on stat")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=fake_stat):
            with pytest.raises(OSError):
                _check_same_volume(final, tmp_dir)


# ---------------------------------------------------------------------------
# publish_if_absent — core paths
# ---------------------------------------------------------------------------


class TestPublishIfAbsent:
    """Golden-path and outcome tests for publish_if_absent."""

    def test_created_returns_created(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        outcome = publish_if_absent(final, body, tmp_path / "tmp")
        assert outcome == "created"
        assert final.read_bytes() == body

    def test_created_file_is_durable(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        publish_if_absent(final, body, tmp_path / "tmp")
        # File must be present and readable after the call.
        assert json.loads(final.read_bytes()) == json.loads(body)

    def test_identical_returns_identical(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        publish_if_absent(final, body, tmp_path / "tmp")
        # Second call with the same record (but may differ in writer-stamped fields).
        rec2 = dict(_record())
        rec2["writer"] = "agent-2"  # writer-stamped; stripped by content_digest
        rec2["written_at"] = "2026-01-02"  # writer-stamped
        body2 = _encode(rec2)
        outcome = publish_if_absent(final, body2, tmp_path / "tmp2")
        assert outcome == "identical"

    def test_conflict_returns_conflict(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        rec_a = _record(extra="v1")
        rec_b = _record(extra="v2")
        publish_if_absent(final, _encode(rec_a), tmp_path / "tmp")
        outcome = publish_if_absent(final, _encode(rec_b), tmp_path / "tmp2")
        assert outcome == "conflict"

    def test_conflict_winning_bytes_unchanged(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        first_body = _encode(_record(extra="original"))
        publish_if_absent(final, first_body, tmp_path / "tmp")
        publish_if_absent(final, _encode(_record(extra="interloper")), tmp_path / "tmp2")
        assert final.read_bytes() == first_body

    def test_no_tmp_files_left_after_created(self, tmp_path: Path) -> None:
        tmp_dir = tmp_path / "tmp"
        publish_if_absent(tmp_path / "journal" / "rec.json", _encode(_record()), tmp_dir)
        assert list(tmp_dir.glob("*.json")) == []

    def test_no_tmp_files_left_after_identical(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        tmp_dir = tmp_path / "tmp"
        publish_if_absent(final, body, tmp_dir)
        publish_if_absent(final, body, tmp_dir)
        assert list(tmp_dir.glob("*.json")) == []

    def test_no_tmp_files_left_after_conflict(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        tmp_dir = tmp_path / "tmp"
        publish_if_absent(final, _encode(_record(extra="v1")), tmp_dir)
        publish_if_absent(final, _encode(_record(extra="v2")), tmp_dir)
        assert list(tmp_dir.glob("*.json")) == []

    def test_volume_mismatch_raises_store_error(self, tmp_path: Path) -> None:
        # Simulate volumes disagreeing by patching _check_same_volume directly.
        final = tmp_path / "rec.json"
        with mock.patch(
            "execution_store._check_same_volume",
            side_effect=StoreError("different volumes"),
        ):
            with pytest.raises(StoreError):
                publish_if_absent(final, _encode(_record()), tmp_path / "tmp")

    def test_link_non_eexist_error_raises_store_error(self, tmp_path: Path) -> None:
        final = tmp_path / "rec.json"
        err = OSError(errno.ENOSPC, "No space")
        with mock.patch("os.link", side_effect=err):
            with pytest.raises(StoreError):
                publish_if_absent(final, _encode(_record()), tmp_path / "tmp")

    def test_eexist_unreadable_final_raises_store_error(self, tmp_path: Path) -> None:
        """If final exists but can't be read on EEXIST, raise StoreError."""
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        publish_if_absent(final, body, tmp_path / "tmp")

        # Make final unreadable, then call again to trigger EEXIST path.
        with mock.patch.object(Path, "read_bytes", side_effect=OSError(errno.EACCES, "denied")):
            with pytest.raises(StoreError, match="cannot read"):
                publish_if_absent(final, body, tmp_path / "tmp2")

    def test_fsync_dir_after_created_raises_store_error(self, tmp_path: Path) -> None:
        """If final's dir fsync fails on the 'created' path, raise StoreError."""
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        calls: list = []

        real_fsync = os.fsync

        def fake_fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) >= 2:
                raise OSError(errno.EIO, "I/O error")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fake_fsync):
            with pytest.raises(StoreError):
                publish_if_absent(final, body, tmp_path / "tmp")

    def test_fsync_dir_of_tmp_raises_store_error(self, tmp_path: Path) -> None:
        """If tmp_dir's fsync fails (step 7), raise StoreError."""
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        calls: list = []

        real_fsync = os.fsync

        def fake_fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) >= 3:
                raise OSError(errno.EIO, "I/O error")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fake_fsync):
            with pytest.raises(StoreError):
                publish_if_absent(final, body, tmp_path / "tmp")


# ---------------------------------------------------------------------------
# Crash-injection tests (§7.2 durable-effects prefix)
# ---------------------------------------------------------------------------


class TestPublishIfAbsentCrashInjection:
    """Simulate a process crash at each step boundary.

    After each injected crash, a second call with the same arguments must
    produce the correct outcome: 'created' means the record was not yet written
    (or was and is now re-linked identically), 'identical' means it succeeded.
    """

    # Crash before link: the final file doesn't exist; second call must create it.
    def test_crash_before_link_second_call_creates(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        tmp_dir = tmp_path / "tmp"

        with mock.patch("os.link", side_effect=OSError(errno.EIO, "I/O error")):
            with pytest.raises(StoreError):
                publish_if_absent(final, body, tmp_dir)

        assert not final.exists()
        outcome = publish_if_absent(final, body, tmp_dir)
        assert outcome == "created"

    # Crash after link but before final dir fsync:
    # final is durable (hard link created), second call returns 'identical'.
    def test_crash_after_link_before_dir_fsync(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        tmp_dir = tmp_path / "tmp"
        calls: list = []

        # fsync call sequence for fresh tmp_path (journal/ and tmp/ don't exist):
        #   1: _mkdir_synced fsyncs tmp_path after creating journal/
        #   2: _mkdir_synced fsyncs tmp_path after creating tmp/
        #   3: _write_tmp fsyncs the file fd
        #   4: fsync_dir(final.parent) -- crash here
        real_fsync = os.fsync

        def crash_on_dir_fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) >= 4:
                raise OSError(errno.EIO, "crash before dir fsync")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=crash_on_dir_fsync):
            with pytest.raises(StoreError):
                publish_if_absent(final, body, tmp_dir)

        # final must already have been hard-linked before the crash.
        assert final.exists()
        # Second call with same body → 'identical' (content_digest matches).
        outcome = publish_if_absent(final, body, tmp_path / "tmp2")
        assert outcome == "identical"

    # Crash after final dir fsync but before tmp unlink:
    # final is fully durable; second call returns 'identical'.
    def test_crash_after_dir_fsync_before_tmp_unlink(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        tmp_dir = tmp_path / "tmp"
        calls: list = []

        # fsync sequence: 1,2=mkdir fsyncs, 3=write_tmp, 4=final parent, 5=tmp_dir
        real_fsync = os.fsync

        def crash_on_third_fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) >= 5:
                raise OSError(errno.EIO, "crash before tmp-dir fsync")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=crash_on_third_fsync):
            with pytest.raises(StoreError):
                publish_if_absent(final, body, tmp_dir)

        assert final.exists()
        outcome = publish_if_absent(final, body, tmp_path / "tmp2")
        assert outcome == "identical"

    # 'created' followed immediately by 'identical' for same content.
    def test_idempotent_identical_after_created(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        body = _encode(_record())
        tmp_dir = tmp_path / "tmp"
        r1 = publish_if_absent(final, body, tmp_dir)
        r2 = publish_if_absent(final, body, tmp_path / "tmp2")
        assert r1 == "created"
        assert r2 == "identical"

    # Conflict is not idempotent (different content_digest).
    def test_conflict_leaves_original_intact(self, tmp_path: Path) -> None:
        final = tmp_path / "journal" / "rec.json"
        orig = _encode(_record(extra="orig"))
        interloper = _encode(_record(extra="interloper"))
        tmp_dir = tmp_path / "tmp"
        publish_if_absent(final, orig, tmp_dir)
        r = publish_if_absent(final, interloper, tmp_path / "tmp2")
        assert r == "conflict"
        assert final.read_bytes() == orig


# ---------------------------------------------------------------------------
# reap_tmp_files
# ---------------------------------------------------------------------------


class TestReapTmpFiles:
    def test_missing_dir_returns_zero(self, tmp_path: Path) -> None:
        assert reap_tmp_files(tmp_path / "nonexistent", 3600.0) == 0

    def test_empty_dir_returns_zero(self, tmp_path: Path) -> None:
        assert reap_tmp_files(tmp_path, 3600.0) == 0

    def test_reaps_stale_json_files(self, tmp_path: Path) -> None:
        stale = tmp_path / "stale.json"
        stale.write_bytes(b"{}")
        old_time = time.time() - 7200.0
        os.utime(str(stale), (old_time, old_time))
        removed = reap_tmp_files(tmp_path, 3600.0)
        assert removed == 1
        assert not stale.exists()

    def test_keeps_fresh_json_files(self, tmp_path: Path) -> None:
        fresh = tmp_path / "fresh.json"
        fresh.write_bytes(b"{}")
        removed = reap_tmp_files(tmp_path, 3600.0)
        assert removed == 0
        assert fresh.exists()

    def test_ignores_non_json_files(self, tmp_path: Path) -> None:
        other = tmp_path / "old.txt"
        other.write_bytes(b"data")
        old_time = time.time() - 7200.0
        os.utime(str(other), (old_time, old_time))
        removed = reap_tmp_files(tmp_path, 3600.0)
        assert removed == 0

    def test_tolerates_stat_error(self, tmp_path: Path) -> None:
        # Create a mock entry whose stat() raises; inject it via iterdir.
        mock_entry = mock.MagicMock()
        mock_entry.suffix = ".json"
        mock_entry.stat.side_effect = OSError(errno.EACCES, "denied")

        def mock_iterdir(self_: Path) -> Any:
            return iter([mock_entry])

        with mock.patch.object(Path, "iterdir", mock_iterdir):
            removed = reap_tmp_files(tmp_path, 0.0)
        assert removed == 0

    def test_tolerates_iterdir_error(self, tmp_path: Path) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=OSError(errno.EACCES, "denied")):
            assert reap_tmp_files(tmp_path, 0.0) == 0


# ---------------------------------------------------------------------------
# _RegistryLock
# ---------------------------------------------------------------------------


class TestRegistryLock:
    def test_creates_and_removes_lock_dir(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "registry.lock"
        with _RegistryLock(lock_path, timeout=1.0):
            assert lock_path.is_dir()
        assert not lock_path.exists()

    def test_context_manager_returns_self(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "registry.lock"
        with _RegistryLock(lock_path, timeout=1.0) as lock:
            assert isinstance(lock, _RegistryLock)

    def test_times_out_when_lock_held(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "registry.lock"
        lock_path.mkdir()  # Simulate a held lock.
        with pytest.raises(GrantRegistryError, match="registry lock busy"):
            with _RegistryLock(lock_path, timeout=0.1):
                pass

    def test_exit_tolerates_rmdir_failure(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "registry.lock"
        with mock.patch.object(Path, "rmdir", side_effect=OSError(errno.EACCES, "denied")):
            with _RegistryLock(lock_path, timeout=1.0):
                pass  # __exit__ should not raise even if rmdir fails

    def test_acquires_after_brief_contention(self, tmp_path: Path) -> None:
        """Lock is eventually acquired after a competing lock is released."""
        lock_path = tmp_path / "registry.lock"
        original_mkdir = Path.mkdir

        call_count: list = [0]

        def flaky_mkdir(self: Path, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise FileExistsError("already exists")
            original_mkdir(self, **kwargs)

        with mock.patch.object(Path, "mkdir", flaky_mkdir):
            with _RegistryLock(lock_path, timeout=1.0):
                pass
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# GrantRegistry
# ---------------------------------------------------------------------------


def _sample_grant(attempt_id: str = "att-1") -> Dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "project_id": "proj-1",
        "task_id": "task-1",
        "coordinator_run": "run-1",
        "ownership_generation": 1,
        "claims": [],
        "capability": "none",
        "capability_id": None,
        "granted_at": "2026-01-01T00:00:00+00:00",
        "body_digest": "abc123",
    }


class TestGrantRegistry:
    def test_scan_and_insert_writes_grant(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        grant = _sample_grant("att-1")
        reg.scan_and_insert("att-1", grant)
        path = tmp_path / ".execution-registry" / "grants" / "att-1.json"
        assert path.exists()
        assert json.loads(path.read_bytes())["attempt_id"] == "att-1"

    def test_read_all_grants_empty(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        assert reg.read_all_grants() == []

    def test_read_all_grants_returns_all(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        reg.scan_and_insert("att-1", _sample_grant("att-1"))
        reg.scan_and_insert("att-2", _sample_grant("att-2"))
        grants = reg.read_all_grants()
        ids = {g["attempt_id"] for g in grants}
        assert ids == {"att-1", "att-2"}

    def test_read_all_grants_skips_non_json(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        reg.scan_and_insert("att-1", _sample_grant("att-1"))
        # Plant a non-.json file.
        other = tmp_path / ".execution-registry" / "grants" / "note.txt"
        other.write_text("ignored")
        grants = reg.read_all_grants()
        assert len(grants) == 1

    def test_read_all_grants_skips_corrupt_json(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        grants_dir = tmp_path / ".execution-registry" / "grants"
        grants_dir.mkdir(parents=True)
        bad = grants_dir / "corrupt.json"
        bad.write_bytes(b"not valid json")
        grants = reg.read_all_grants()
        assert grants == []

    def test_read_all_grants_skips_unreadable_file(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        grants_dir = tmp_path / ".execution-registry" / "grants"
        grants_dir.mkdir(parents=True)
        f = grants_dir / "att.json"
        f.write_bytes(_encode(_sample_grant()))
        with mock.patch.object(Path, "read_bytes", side_effect=OSError(errno.EACCES, "denied")):
            grants = reg.read_all_grants()
        assert grants == []

    def test_update_grant_replaces_in_place(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        grant = _sample_grant("att-1")
        reg.scan_and_insert("att-1", grant)
        updated = dict(grant)
        updated["capability"] = "issued"
        reg.update_grant("att-1", updated)
        path = tmp_path / ".execution-registry" / "grants" / "att-1.json"
        assert json.loads(path.read_bytes())["capability"] == "issued"

    def test_update_grant_missing_raises(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        with pytest.raises(GrantRegistryError, match="grant not found"):
            reg.update_grant("nonexistent", _sample_grant())

    def test_scan_and_remove_deletes_grant(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        reg.scan_and_insert("att-1", _sample_grant("att-1"))
        reg.scan_and_remove("att-1")
        assert reg.read_all_grants() == []

    def test_scan_and_remove_idempotent(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path)
        reg.scan_and_remove("nonexistent")  # should not raise

    def test_registry_lock_path(self, tmp_path: Path) -> None:
        reg = GrantRegistry(tmp_path, lock_timeout=1.0)
        assert reg._lock_path == tmp_path / ".execution-registry" / "registry.lock"


# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    def test_writes_json_to_path(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        _atomic_write_json(target, {"key": "value"})
        data = json.loads(target.read_bytes())
        assert data == {"key": "value"}

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "data.json"
        _atomic_write_json(target, {"a": 1})
        assert target.exists()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        _atomic_write_json(target, {"v": 1})
        _atomic_write_json(target, {"v": 2})
        assert json.loads(target.read_bytes())["v"] == 2

    def test_tmp_cleaned_up_on_error(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        with mock.patch("os.replace", side_effect=OSError(errno.EIO, "I/O error")):
            with pytest.raises(OSError):
                _atomic_write_json(target, {"a": 1})
        # The .tmp file must be gone.
        assert list(tmp_path.glob("*.tmp")) == []

    def test_keys_sorted_in_output(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        _atomic_write_json(target, {"z": 1, "a": 2})
        raw = target.read_text(encoding="utf-8")
        assert raw.index('"a"') < raw.index('"z"')

    def test_output_ends_with_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        _atomic_write_json(target, {"x": 1})
        assert target.read_bytes().endswith(b"\n")
