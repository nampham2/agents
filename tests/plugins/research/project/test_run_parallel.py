"""Tests for Phase 6 PR A: SubprocessAdapter, run_parallel_tasks, run-parallel CLI."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import execution_adapter
import manage_workspace
from execution_adapter import (
    OBSERVE_FINISHED,
    OBSERVE_RUNNING,
    OBSERVE_UNKNOWN,
    AdapterError,
    SubprocessAdapter,
    _file_size,
)
from execution_ops import OperationError, run_parallel_tasks

TIMESTAMP = "2026-09-15T10:00:00Z"

_FAST_CMD = [sys.executable, "-c", "pass"]
_SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(30)"]


# ---------------------------------------------------------------------------
# Fixture helpers (mirrored from test_run_once.py)
# ---------------------------------------------------------------------------


def _todo_task(task_id: str = "t1", depends_on: list | None = None) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "status": "TODO",
        "depends_on": depends_on or [],
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


def _done_task(task_id: str = "t0") -> dict:
    t = _todo_task(task_id)
    t["status"] = "DONE"
    return t


def _make_project(root: Path, tasks: list | None = None) -> tuple:
    ws = root / "ws"
    project_name = "2026-09-15-001"
    project_dir = ws / project_name
    project_dir.mkdir(parents=True)
    (project_dir / "execution").mkdir()
    (project_dir / "execution" / "runtime").mkdir()
    (project_dir / "evidence.md").write_text("# Evidence\n\n", encoding="utf-8")
    state = {
        "schema_version": 4,
        "project": project_name,
        "title": "Test project",
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
        "tasks": tasks if tasks is not None else [_todo_task()],
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
    return ws, project_dir


def _call_manage(args: list) -> int:
    with patch.object(sys, "argv", ["manage", *args]):
        return manage_workspace.main()


# ===========================================================================
# SubprocessAdapter
# ===========================================================================


class SubprocessAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.attempt_dir = self.tmp.name

    def _wait_finished(self, adapter: SubprocessAdapter, handle: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if adapter.observe(handle) != OBSERVE_RUNNING:
                return
            time.sleep(0.05)
        self.fail(f"process did not finish within {timeout}s")

    # ---- start returns a well-formed handle string --------------------------

    def test_start_returns_handle_string(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        self.assertIsInstance(handle, str)
        self.assertTrue(handle.startswith("pid:"))
        self.assertIn(":pgid:", handle)
        # Clean up
        adapter.terminate(handle)

    # ---- stdout/stderr files are created ------------------------------------

    def test_start_creates_stdout_stderr_files(self) -> None:
        adapter = SubprocessAdapter([sys.executable, "-c", "print('hello')"])
        adapter.start({}, self.attempt_dir)
        self.assertTrue(Path(self.attempt_dir, "stdout.txt").exists())
        self.assertTrue(Path(self.attempt_dir, "stderr.txt").exists())

    # ---- observe: RUNNING while alive, FINISHED after exit ------------------

    def test_observe_running_while_process_alive(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        try:
            obs = adapter.observe(handle)
            self.assertEqual(OBSERVE_RUNNING, obs)
        finally:
            adapter.terminate(handle)

    def test_observe_finished_after_seal(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        self._wait_finished(adapter, handle)
        adapter.seal(handle)
        self.assertEqual(OBSERVE_FINISHED, adapter.observe(handle))

    # ---- observe: unknown handle --------------------------------------------

    def test_observe_unknown_handle_returns_unknown(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        self.assertEqual(OBSERVE_UNKNOWN, adapter.observe("pid:99999999:pgid:99999999"))

    # ---- seal: happy path ---------------------------------------------------

    def test_seal_returns_exited_dict(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        self._wait_finished(adapter, handle)
        attestation = adapter.seal(handle)
        self.assertEqual("exited", attestation["variant"])
        self.assertEqual(0, attestation["exit_code"])
        self.assertIn("stdout_bytes", attestation)
        self.assertIn("stderr_bytes", attestation)

    def test_seal_nonzero_exit_code(self) -> None:
        adapter = SubprocessAdapter([sys.executable, "-c", "import sys; sys.exit(42)"])
        handle = adapter.start({}, self.attempt_dir)
        self._wait_finished(adapter, handle)
        attestation = adapter.seal(handle)
        self.assertEqual(42, attestation["exit_code"])

    # ---- seal: process still running raises AdapterError -------------------

    def test_seal_raises_when_process_running(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        try:
            with self.assertRaises(AdapterError):
                adapter.seal(handle)
        finally:
            adapter.terminate(handle)

    # ---- seal: unknown handle raises AdapterError --------------------------

    def test_seal_unknown_handle_raises_adapter_error(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        with self.assertRaises(AdapterError):
            adapter.seal("pid:99999999:pgid:99999999")

    # ---- terminate: kills a running process ---------------------------------

    def test_terminate_kills_running_process(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        self.assertEqual(OBSERVE_RUNNING, adapter.observe(handle))
        adapter.terminate(handle)
        # After terminate, process should be gone
        self.assertEqual(OBSERVE_FINISHED, adapter.observe(handle))

    # ---- terminate: idempotent after already terminated --------------------

    def test_terminate_idempotent(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        adapter.terminate(handle)
        adapter.terminate(handle)  # must not raise

    # ---- terminate: unknown handle is a no-op ------------------------------

    def test_terminate_unknown_handle_noop(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        adapter.terminate("pid:99999999:pgid:99999999")  # must not raise

    # ---- full lifecycle: start → observe → seal ----------------------------

    def test_full_lifecycle(self) -> None:
        adapter = SubprocessAdapter([sys.executable, "-c", "print('lifecycle')"])
        handle = adapter.start({}, self.attempt_dir)
        self._wait_finished(adapter, handle)
        attestation = adapter.seal(handle)
        self.assertEqual("exited", attestation["variant"])
        self.assertEqual(0, attestation["exit_code"])
        self.assertGreater(attestation.get("stdout_bytes", 0), 0)
        self.assertEqual(OBSERVE_FINISHED, adapter.observe(handle))

    # ---- seal without prior observe (direct waitpid path) ------------------

    def test_seal_without_prior_observe(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        time.sleep(0.3)  # process exits to zombie; observe() not called
        attestation = adapter.seal(handle)
        self.assertEqual("exited", attestation["variant"])
        self.assertEqual(0, attestation["exit_code"])

    # ---- seal ChildProcessError: process reaped externally -----------------

    def test_seal_raises_when_externally_reaped(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        with patch.object(execution_adapter.os, "waitpid", side_effect=ChildProcessError):
            with self.assertRaises(AdapterError):
                adapter.seal(handle)
        adapter.terminate(handle)

    # ---- observe PermissionError → UNKNOWN ---------------------------------

    def test_observe_permission_error_returns_unknown(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        try:
            with patch.object(execution_adapter.os, "kill", side_effect=PermissionError):
                obs = adapter.observe(handle)
            self.assertEqual(OBSERVE_UNKNOWN, obs)
        finally:
            adapter.terminate(handle)

    # ---- observe ChildProcessError after kill succeeds → FINISHED ----------

    def test_observe_child_process_error_in_waitpid(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        try:
            with patch.object(execution_adapter.os, "waitpid", side_effect=ChildProcessError):
                obs = adapter.observe(handle)
            self.assertEqual(OBSERVE_FINISHED, obs)
        finally:
            adapter.terminate(handle)

    # ---- terminate: ProcessLookupError on SIGTERM --------------------------

    def test_terminate_process_already_gone(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        time.sleep(0.3)  # let process exit naturally
        # killpg will raise ProcessLookupError since group is gone
        adapter.terminate(handle)
        state = adapter._procs[handle]
        self.assertTrue(state.reaped)

    # ---- terminate: ChildProcessError in wait loop -------------------------

    def test_terminate_child_process_error_in_loop(self) -> None:
        adapter = SubprocessAdapter(_SLEEP_CMD)
        handle = adapter.start({}, self.attempt_dir)
        try:
            with patch.object(execution_adapter.os, "waitpid", side_effect=ChildProcessError):
                adapter.terminate(handle)
        except Exception:
            adapter.terminate(handle)
        state = adapter._procs[handle]
        self.assertTrue(state.reaped)

    # ---- terminate: SIGKILL fallback when SIGTERM ignored ------------------

    def test_terminate_sigkill_fallback(self) -> None:
        adapter = SubprocessAdapter([
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
        ])
        handle = adapter.start({}, self.attempt_dir)
        # Fast-forward time so the 2s deadline expires on first loop iteration
        call_num = [0]
        def fast_mono() -> float:
            call_num[0] += 1
            return 0.0 if call_num[0] == 1 else 100.0
        with patch.object(execution_adapter.time, "monotonic", fast_mono):
            with patch.object(execution_adapter.time, "sleep"):
                adapter.terminate(handle)
        self.assertEqual(OBSERVE_FINISHED, adapter.observe(handle))

    # ---- terminate: SIGKILL lands cleanly, waitpid reaps (lines 308-309, 312) --

    def test_terminate_sigkill_waitpid_path(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        state = adapter._procs[handle]
        time.sleep(0.1)  # process has exited; state.reaped still False

        call_num = [0]
        def fast_mono() -> float:
            call_num[0] += 1
            return 0.0 if call_num[0] == 1 else 100.0

        with patch.object(execution_adapter.time, "monotonic", fast_mono):
            with patch.object(execution_adapter.time, "sleep"):
                with patch.object(execution_adapter.os, "killpg"):  # SIGTERM + SIGKILL both succeed
                    with patch.object(execution_adapter.os, "waitpid", return_value=(state.pid, 0)):
                        adapter.terminate(handle)

        self.assertTrue(state.reaped)

    # ---- terminate: SIGKILL lands, waitpid raises ChildProcessError (lines 310-311) --

    def test_terminate_sigkill_waitpid_child_process_error(self) -> None:
        adapter = SubprocessAdapter(_FAST_CMD)
        handle = adapter.start({}, self.attempt_dir)
        state = adapter._procs[handle]
        time.sleep(0.1)

        call_num = [0]
        def fast_mono() -> float:
            call_num[0] += 1
            return 0.0 if call_num[0] == 1 else 100.0

        with patch.object(execution_adapter.time, "monotonic", fast_mono):
            with patch.object(execution_adapter.time, "sleep"):
                with patch.object(execution_adapter.os, "killpg"):
                    with patch.object(execution_adapter.os, "waitpid", side_effect=ChildProcessError):
                        adapter.terminate(handle)

        self.assertTrue(state.reaped)


# ===========================================================================
# _file_size helper
# ===========================================================================


class FileSizeTests(unittest.TestCase):
    def test_missing_file_returns_zero(self) -> None:
        self.assertEqual(0, _file_size("/nonexistent/path/that/does/not/exist.txt"))

    def test_existing_file_returns_size(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello")
            path = f.name
        try:
            self.assertEqual(5, _file_size(path))
        finally:
            import os
            os.unlink(path)


# ===========================================================================
# run_parallel_tasks
# ===========================================================================


class RunParallelTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _state(self, project_dir: Path) -> dict:
        return json.loads((project_dir / "project.json").read_bytes())

    # ---- zero READY tasks → returns 0, O20 runs ----------------------------

    def test_zero_ready_tasks_returns_0(self) -> None:
        _, project_dir = _make_project(self.root, tasks=[_done_task("t0")])
        done = run_parallel_tasks(
            project_dir,
            lambda: SubprocessAdapter(_FAST_CMD),
        )
        self.assertEqual(0, done)
        state = self._state(project_dir)
        self.assertIsNone(state["execution"]["coordinator_run"])

    # ---- single READY task → returns 1 ------------------------------------

    def test_single_ready_task_returns_1(self) -> None:
        _, project_dir = _make_project(self.root)
        done = run_parallel_tasks(
            project_dir,
            lambda: SubprocessAdapter(_FAST_CMD),
        )
        self.assertEqual(1, done)
        state = self._state(project_dir)
        self.assertEqual("DONE", state["tasks"][0]["status"])
        self.assertIsNone(state["execution"]["coordinator_run"])

    # ---- two concurrent READY tasks → both DONE, returns 2 ----------------

    def test_two_tasks_run_concurrently(self) -> None:
        _, project_dir = _make_project(
            self.root, tasks=[_todo_task("t1"), _todo_task("t2")]
        )
        done = run_parallel_tasks(
            project_dir,
            lambda: SubprocessAdapter(_FAST_CMD),
            max_concurrent=2,
        )
        self.assertEqual(2, done)
        state = self._state(project_dir)
        statuses = {t["id"]: t["status"] for t in state["tasks"]}
        self.assertEqual("DONE", statuses["t1"])
        self.assertEqual("DONE", statuses["t2"])
        self.assertIsNone(state["execution"]["coordinator_run"])

    # ---- max_concurrent=1 runs only one task --------------------------------

    def test_max_concurrent_1_runs_one_task(self) -> None:
        _, project_dir = _make_project(
            self.root, tasks=[_todo_task("t1"), _todo_task("t2")]
        )
        done = run_parallel_tasks(
            project_dir,
            lambda: SubprocessAdapter(_FAST_CMD),
            max_concurrent=1,
        )
        self.assertEqual(1, done)
        state = self._state(project_dir)
        statuses = {t["id"]: t["status"] for t in state["tasks"]}
        # Exactly one task is DONE
        done_count = sum(1 for s in statuses.values() if s == "DONE")
        self.assertEqual(1, done_count)


# ===========================================================================
# run-parallel CLI
# ===========================================================================


class RunParallelCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_parallel_tasks_ran_returns_0(self) -> None:
        _, project_dir = _make_project(self.root)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            result = _call_manage([
                "run-parallel", str(project_dir),
                "--", sys.executable, "-c", "pass",
            ])
        self.assertEqual(0, result)
        self.assertIn("Tasks completed", out.getvalue())
        self.assertIn("1 task(s)", out.getvalue())

    def test_run_parallel_no_ready_task_returns_2(self) -> None:
        _, project_dir = _make_project(self.root, tasks=[_done_task("t0")])
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            result = _call_manage([
                "run-parallel", str(project_dir),
                "--", sys.executable, "-c", "pass",
            ])
        self.assertEqual(2, result)
        self.assertIn("No READY task found", err.getvalue())

    def test_run_parallel_operation_error_returns_1(self) -> None:
        _, project_dir = _make_project(self.root)
        with patch("manage_workspace.run_parallel_tasks", side_effect=OperationError("boom")):
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                result = _call_manage([
                    "run-parallel", str(project_dir),
                    "--", sys.executable, "-c", "pass",
                ])
        self.assertEqual(1, result)
        self.assertIn("boom", err.getvalue())

    def test_run_parallel_default_command(self) -> None:
        _, project_dir = _make_project(self.root)
        result = _call_manage(["run-parallel", str(project_dir)])
        self.assertEqual(0, result)

    def test_run_parallel_two_tasks_concurrency_2(self) -> None:
        _, project_dir = _make_project(
            self.root, tasks=[_todo_task("t1"), _todo_task("t2")]
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            result = _call_manage([
                "run-parallel", str(project_dir), "--concurrency", "2",
                "--", sys.executable, "-c", "pass",
            ])
        self.assertEqual(0, result)
        self.assertIn("2 task(s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
