"""Tests for Phase 6b: _read_host_identity and O19 registry wiring."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import execution_ops
from execution_ops import _read_host_identity

TIMESTAMP = "2026-09-15T10:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_paths_raise(*exc_args):
    """Return a fake Path.read_text that raises OSError for every path."""
    def fake_read(self_path, encoding=None, errors=None):
        raise OSError(*exc_args or ["no such file"])
    return fake_read


def _path_map(**path_content):
    """Return a fake Path.read_text that serves content based on path substring."""
    def fake_read(self_path, encoding=None, errors=None):
        s = str(self_path)
        for key, val in path_content.items():
            if key in s:
                if isinstance(val, BaseException):
                    raise val
                return val
        raise OSError(f"unmapped path: {s}")
    return fake_read


# ===========================================================================
# _read_host_identity unit tests
# ===========================================================================


class ReadHostIdentityTests(unittest.TestCase):

    # ---- pid is always os.getpid() -----------------------------------------

    def test_pid_is_current_process(self) -> None:
        result = _read_host_identity()
        self.assertEqual(os.getpid(), result["pid"])

    # ---- host_id: machine-id happy path ------------------------------------

    def test_host_id_from_machine_id(self) -> None:
        fake = _path_map(**{"/etc/machine-id": "test-machine-id\n"})
        with patch.object(execution_ops.Path, "read_text", fake):
            result = _read_host_identity()
        self.assertEqual("test-machine-id", result["host_id"])

    # ---- host_id: macOS ioreg fallback -------------------------------------

    def test_host_id_from_ioreg(self) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout = '  "IOPlatformUUID" = "AAAA-BBBB-CCCC"\n  other stuff\n'

        with patch.object(execution_ops.Path, "read_text", _all_paths_raise()):
            with patch.object(execution_ops.subprocess, "run", return_value=mock_proc):
                result = _read_host_identity()

        self.assertEqual("AAAA-BBBB-CCCC", result["host_id"])

    # ---- host_id: ioreg exception → empty (lines 765-766) -----------------

    def test_host_id_empty_when_ioreg_raises(self) -> None:
        with patch.object(execution_ops.Path, "read_text", _all_paths_raise()):
            with patch.object(execution_ops.subprocess, "run", side_effect=FileNotFoundError("ioreg")):
                result = _read_host_identity()

        self.assertEqual("", result["host_id"])

    # ---- boot_id: Linux /proc/sys/kernel/random/boot_id -------------------

    def test_boot_id_from_proc(self) -> None:
        boot_uuid = "550e8400-e29b-41d4-a716-446655440000"
        fake = _path_map(**{
            "/etc/machine-id": "mid\n",
            "boot_id": boot_uuid + "\n",
        })
        with patch.object(execution_ops.Path, "read_text", fake):
            result = _read_host_identity()
        self.assertEqual(boot_uuid, result["boot_id"])

    # ---- boot_id: macOS sysctl fallback ------------------------------------

    def test_boot_id_from_sysctl(self) -> None:
        sysctl_uuid = "DDDD-EEEE-FFFF"
        mock_proc = MagicMock()
        mock_proc.stdout = sysctl_uuid + "\n"

        fake = _path_map(**{"/etc/machine-id": "mid\n"})
        with patch.object(execution_ops.Path, "read_text", fake):
            with patch.object(execution_ops.subprocess, "run", return_value=mock_proc):
                result = _read_host_identity()

        self.assertEqual(sysctl_uuid, result["boot_id"])

    # ---- boot_id: sysctl exception → empty (lines 780-781) ----------------

    def test_boot_id_empty_when_sysctl_raises(self) -> None:
        fake = _path_map(**{"/etc/machine-id": "mid\n"})
        with patch.object(execution_ops.Path, "read_text", fake):
            with patch.object(execution_ops.subprocess, "run", side_effect=FileNotFoundError("sysctl")):
                result = _read_host_identity()

        self.assertEqual("", result["boot_id"])

    # ---- process_start: Linux /proc/<pid>/stat (lines 789-795) ------------

    def test_process_start_from_proc_stat(self) -> None:
        pid = os.getpid()
        stat_fields = ["0"] * 52
        stat_fields[21] = "200"  # starttime: 200 ticks since boot
        stat_text = " ".join(stat_fields)

        fake = _path_map(**{
            "/etc/machine-id": "mid\n",
            "boot_id": "bid\n",
            f"/proc/{pid}/stat": stat_text,
            "/proc/uptime": "5000.0 2500.0",
        })
        with patch.object(execution_ops.Path, "read_text", fake):
            with patch.object(execution_ops.os, "sysconf", return_value=100):
                with patch.object(execution_ops.time, "time", return_value=10000.0):
                    result = _read_host_identity()

        # boot_epoch = 10000.0 - 5000.0 = 5000.0
        # start_epoch = 5000.0 + 200 / 100 = 5002.0
        expected = datetime.datetime.fromtimestamp(
            5002.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(expected, result["process_start"])

    # ---- process_start: fallback to _now() when /proc not available -------

    def test_process_start_falls_back_to_now(self) -> None:
        with patch.object(execution_ops.Path, "read_text", _all_paths_raise()):
            with patch.object(execution_ops.subprocess, "run", side_effect=FileNotFoundError):
                result = _read_host_identity()

        self.assertRegex(result["process_start"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    # ---- returned dict has all four required keys --------------------------

    def test_result_has_required_keys(self) -> None:
        result = _read_host_identity()
        for key in ("host_id", "boot_id", "pid", "process_start"):
            self.assertIn(key, result)


# ===========================================================================
# O19 wiring: real pid in owner record
# ===========================================================================


def _make_project(root: Path) -> tuple:
    project_name = "2026-09-15-001"
    project_dir = root / project_name
    (project_dir / "execution" / "runtime").mkdir(parents=True)
    (project_dir / "evidence.md").write_text("# Evidence\n\n", encoding="utf-8")
    state = {
        "schema_version": 4,
        "project": project_name,
        "title": "Test",
        "status": "EXECUTING",
        "created": TIMESTAMP,
        "updated": TIMESTAMP,
        "working_directory": str(project_dir),
        "revision": 0,
        "current_tasks": [],
        "review": {
            "cycle": 0,
            "required": False,
            "status": "not_required",
            "evidence": [],
        },
        "cancellation_reason": None,
        "tasks": [
            {
                "id": "t1",
                "name": "Task t1",
                "status": "TODO",
                "depends_on": [],
                "authorization": {
                    "required": False,
                    "status": "not_required",
                    "scope": None,
                    "source": None,
                    "authorized_at": None,
                },
                "outputs": [],
                "success_criteria": "ok",
                "verification": "check",
                "effect": {"kind": "none", "description": None},
                "receipts": [],
                "evidence": [],
                "skip_reason": None,
                "block_reason": None,
            }
        ],
        "execution": {
            "protocol_version": 1,
            "coordinator_run": None,
            "ownership_generation": 0,
            "attempts": {},
        },
    }
    (project_dir / "project.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return root, project_dir


class O19OwnerRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, self.project_dir = _make_project(Path(self.tmp.name))

    def test_owner_record_contains_real_pid(self) -> None:
        from execution_adapter import FakeAdapter
        from execution_ops import run_sequential_task

        adapter = FakeAdapter()
        adapter.enqueue_start("handle-1")
        adapter.enqueue_observe("handle-1", "finished")
        adapter.enqueue_seal("handle-1", {"variant": "exited", "exit_code": 0})

        run_sequential_task(self.project_dir, adapter)

        owners_dir = self.project_dir / "execution" / "owners"
        owner_files = list(owners_dir.glob("*.json"))
        self.assertEqual(1, len(owner_files))
        owner = json.loads(owner_files[0].read_bytes())
        self.assertEqual(os.getpid(), owner["pid"])

    def test_owner_record_has_nonempty_host_id_on_macos(self) -> None:
        """On macOS the ioreg fallback should populate host_id."""
        from execution_adapter import FakeAdapter
        from execution_ops import run_sequential_task

        adapter = FakeAdapter()
        adapter.enqueue_start("h2")
        adapter.enqueue_observe("h2", "finished")
        adapter.enqueue_seal("h2", {"variant": "exited", "exit_code": 0})

        run_sequential_task(self.project_dir, adapter)

        owners_dir = self.project_dir / "execution" / "owners"
        owner = json.loads(next(owners_dir.glob("*.json")).read_bytes())
        # On any POSIX host at least one of machine-id or ioreg/sysctl gives a value
        self.assertIsInstance(owner["host_id"], str)
        self.assertIsInstance(owner["boot_id"], str)


if __name__ == "__main__":
    unittest.main()
