"""Tests for Phase 5 PR B additions: _make_commit_fn and run_sequential_task."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_workspace
from execution_adapter import OBSERVE_RUNNING, OBSERVE_UNKNOWN, FakeAdapter
from execution_ops import OperationError, _make_commit_fn, run_sequential_task
from workspace_lib import READER_ONLY_REFUSAL, WorkspaceError, commit_candidate

TIMESTAMP = "2026-09-15T10:00:00Z"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _todo_task(
    task_id: str = "t1",
    depends_on: list | None = None,
    *,
    required: bool = False,
    auth_status: str = "not_required",
) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "status": "TODO",
        "depends_on": depends_on or [],
        "authorization": {
            "required": required,
            "status": auth_status,
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


def _make_project(root: Path, tasks: list | None = None, *, project_name: str = "2026-09-15-001") -> tuple:
    """Create a minimal v4 project dir. Returns (workspace_root, project_dir)."""
    ws = root / "ws"
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
# _make_commit_fn
# ===========================================================================


class MakeCommitFnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "proj"
        self.project_dir.mkdir()
        self._reset_state()

    def _reset_state(self, extra: dict | None = None) -> None:
        state = {
            "schema_version": 4,
            "project": "proj",
            "revision": 0,
            "updated": TIMESTAMP,
            "tasks": [],
            "current_tasks": [],
            "execution": {
                "protocol_version": 1,
                "coordinator_run": None,
                "ownership_generation": 0,
                "attempts": {},
            },
        }
        if extra:
            state.update(extra)
        (self.project_dir / "project.json").write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    def _read(self) -> dict:
        return json.loads((self.project_dir / "project.json").read_bytes())

    # ---- ACQUIRE path -------------------------------------------------------

    def test_acquire_sets_coordinator_run_and_generation(self) -> None:
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        rev, _ = commit(
            "", "ACQUIRE", None, 0, None, None,
            {"coordinator_run": "run-abc", "ownership_generation": 1},
        )
        self.assertEqual(1, rev)
        state = self._read()
        self.assertEqual("run-abc", state["execution"]["coordinator_run"])
        self.assertEqual(1, state["execution"]["ownership_generation"])

    # ---- RELINQUISH path (no ownership_generation key in receipt) -----------

    def test_relinquish_clears_coordinator_run_preserves_generation(self) -> None:
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        commit(
            "", "ACQUIRE", None, 0, None, None,
            {"coordinator_run": "run-abc", "ownership_generation": 1},
        )
        commit2 = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        rev, _ = commit2("", "RELINQUISH", None, 1, None, None, {"coordinator_run": None})
        state = self._read()
        self.assertIsNone(state["execution"]["coordinator_run"])
        # ownership_generation not in receipt → unchanged
        self.assertEqual(1, state["execution"]["ownership_generation"])
        self.assertEqual(2, rev)

    # ---- RUNNING path -------------------------------------------------------

    def test_running_sets_attempt_and_current_tasks(self) -> None:
        self._reset_state({"tasks": [{"id": "T01", "status": "TODO"}]})
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        commit("T01", "RUNNING", "att-xyz", 0, None, None, {})
        state = self._read()
        self.assertEqual("RUNNING", state["tasks"][0]["status"])
        self.assertEqual("att-xyz", state["execution"]["attempts"]["T01"])
        self.assertEqual(["T01"], state["current_tasks"])

    # ---- non-RUNNING / DONE path (pops attempt) -----------------------------

    def test_done_pops_attempt_and_current_tasks(self) -> None:
        self._reset_state({
            "tasks": [{"id": "T01", "status": "RUNNING"}],
            "current_tasks": ["T01"],
            "execution": {
                "protocol_version": 1,
                "coordinator_run": None,
                "ownership_generation": 0,
                "attempts": {"T01": "att-xyz"},
            },
        })
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        commit("T01", "DONE", None, 0, None, None, {})
        state = self._read()
        self.assertEqual("DONE", state["tasks"][0]["status"])
        self.assertNotIn("T01", state["execution"]["attempts"])
        self.assertEqual([], state["current_tasks"])

    # ---- block_reason -------------------------------------------------------

    def test_block_reason_written_to_task(self) -> None:
        self._reset_state({"tasks": [{"id": "T01", "status": "RUNNING"}]})
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        commit("T01", "BLOCKED", None, 0, "dep failed", None, {})
        state = self._read()
        self.assertEqual("dep failed", state["tasks"][0]["block_reason"])

    # ---- skip_reason --------------------------------------------------------

    def test_skip_reason_written_to_task(self) -> None:
        self._reset_state({"tasks": [{"id": "T01", "status": "TODO"}]})
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        commit("T01", "SKIPPED", None, 0, None, "not needed", {})
        state = self._read()
        self.assertEqual("not needed", state["tasks"][0]["skip_reason"])

    # ---- lock timeout -------------------------------------------------------

    def test_lock_timeout_raises_operation_error(self) -> None:
        lock = self.project_dir / ".project.lock"
        lock.mkdir()
        try:
            commit = _make_commit_fn(self.project_dir, lock_timeout=0.0)
            with self.assertRaises(OperationError) as ctx:
                commit("", "ACQUIRE", None, 0, None, None, {"coordinator_run": "r"})
            self.assertIn("project lock busy", str(ctx.exception))
        finally:
            lock.rmdir()

    # ---- revision conflict --------------------------------------------------

    def test_revision_conflict_raises_operation_error(self) -> None:
        commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
        with self.assertRaises(OperationError) as ctx:
            commit("", "ACQUIRE", None, 99, None, None, {"coordinator_run": "r"})
        self.assertIn("revision conflict", str(ctx.exception))

    # ---- lock retry sleep (line 2388) ---------------------------------------

    def test_lock_retries_sleep_then_acquired(self) -> None:
        lock = self.project_dir / ".project.lock"
        lock.mkdir()

        def release_on_sleep(secs: float) -> None:
            lock.rmdir()  # release lock when first sleep is reached

        with patch("time.sleep", side_effect=release_on_sleep):
            commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
            rev, _ = commit(
                "", "ACQUIRE", None, 0, None, None,
                {"coordinator_run": "run-x", "ownership_generation": 1},
            )

        self.assertEqual(1, rev)
        self.assertEqual("run-x", self._read()["execution"]["coordinator_run"])

    # ---- write failure → tmp unlinked, lock released (lines 2431-2436) ----

    def test_write_failure_removes_tmp_and_releases_lock(self) -> None:
        original_replace = Path.replace

        def failing_replace(self_path: Path, target: Path) -> Path:
            if ".tmp" in self_path.name:
                raise OSError("simulated disk full")
            return original_replace(self_path, target)

        with patch.object(Path, "replace", failing_replace):
            commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
            with self.assertRaises(OSError):
                commit("", "ACQUIRE", None, 0, None, None, {"coordinator_run": "r"})

        # No leftover tmp files
        self.assertEqual([], list(self.project_dir.glob(".project.json.*.tmp")))
        # Lock released (finally block ran)
        self.assertFalse((self.project_dir / ".project.lock").exists())

    # ---- tmp.unlink() fails (lines 2434-2435) --------------------------------

    def test_write_failure_with_unlink_error_silenced(self) -> None:
        original_replace = Path.replace
        original_unlink = Path.unlink

        def failing_replace(self_path: Path, target: Path) -> Path:
            if ".tmp" in self_path.name:
                raise OSError("disk full")
            return original_replace(self_path, target)

        def failing_unlink(self_path: Path, *, missing_ok: bool = False) -> None:
            if ".tmp" in self_path.name:
                raise OSError("cannot unlink tmp")
            return original_unlink(self_path, missing_ok=missing_ok)

        with patch.object(Path, "replace", failing_replace), \
                patch.object(Path, "unlink", failing_unlink):
            commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
            with self.assertRaises(OSError) as ctx:
                commit("", "ACQUIRE", None, 0, None, None, {"coordinator_run": "r"})

        # The original replace error (not the unlink error) is re-raised
        self.assertIn("disk full", str(ctx.exception))
        # Lock is still released (finally block ran)
        self.assertFalse((self.project_dir / ".project.lock").exists())

    # ---- lock rmdir() fails in finally (lines 2440-2441) --------------------

    def test_lock_cleanup_rmdir_failure_silenced(self) -> None:
        original_rmdir = Path.rmdir

        def failing_rmdir(self_path: Path) -> None:
            if ".project.lock" in self_path.name:
                raise OSError("cannot rmdir lock")
            return original_rmdir(self_path)

        with patch.object(Path, "rmdir", failing_rmdir):
            commit = _make_commit_fn(self.project_dir, lock_timeout=5.0)
            rev, _ = commit(
                "", "ACQUIRE", None, 0, None, None,
                {"coordinator_run": "run-y", "ownership_generation": 1},
            )

        self.assertEqual(1, rev)
        # project.json updated despite rmdir failure
        self.assertEqual("run-y", self._read()["execution"]["coordinator_run"])


# ===========================================================================
# run_sequential_task
# ===========================================================================


class RunSequentialTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _state(self, project_dir: Path) -> dict:
        return json.loads((project_dir / "project.json").read_bytes())

    # ---- happy path: observe=UNKNOWN, seal raises → empty attestation ------

    def test_happy_path_runs_task_returns_true(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()  # default: observe=UNKNOWN, seal raises
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        state = self._state(project_dir)
        self.assertEqual("DONE", state["tasks"][0]["status"])
        self.assertIsNone(state["execution"]["coordinator_run"])
        self.assertEqual({}, state["execution"]["attempts"])

    # ---- no READY task → O20 relinquish, return False -----------------------

    def test_no_ready_task_returns_false(self) -> None:
        _, project_dir = _make_project(self.root, tasks=[_done_task("t0")])
        adapter = FakeAdapter()
        result = run_sequential_task(project_dir, adapter)
        self.assertFalse(result)
        # O20 still ran → coordinator_run cleared
        state = self._state(project_dir)
        self.assertIsNone(state["execution"]["coordinator_run"])

    # ---- handle="" → observe and seal are skipped ---------------------------

    def test_empty_handle_skips_observe_and_seal(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()
        adapter.enqueue_start("")  # start returns "" → handle=""
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        self.assertEqual([], adapter.observed)
        self.assertEqual([], adapter.sealed)

    # ---- observe loop: RUNNING until non-RUNNING ----------------------------

    def test_observe_loop_running_then_unknown(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()
        adapter.enqueue_start("h-1")
        adapter.enqueue_observe("h-1", OBSERVE_RUNNING)
        adapter.enqueue_observe("h-1", OBSERVE_UNKNOWN)
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        self.assertEqual(2, len(adapter.observed))

    # ---- seal with attestation ----------------------------------------------

    def test_adapter_seal_with_attestation(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()
        adapter.enqueue_start("h-1")
        adapter.enqueue_observe("h-1", OBSERVE_UNKNOWN)
        adapter.enqueue_seal("h-1", {"tree_exited": True, "resumable": False})
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        self.assertEqual(["h-1"], adapter.sealed)

    # ---- auth_in_force: required=True + status=explicit --------------------

    def test_auth_in_force_required_and_explicit(self) -> None:
        task = _todo_task(required=True, auth_status="explicit")
        _, project_dir = _make_project(self.root, tasks=[task])
        adapter = FakeAdapter()
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        self.assertEqual("DONE", self._state(project_dir)["tasks"][0]["status"])

    # ---- evidence.md receipt appended by O12 --------------------------------

    def test_evidence_md_receipt_appended(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()
        run_sequential_task(project_dir, adapter)
        ev = (project_dir / "evidence.md").read_text(encoding="utf-8")
        self.assertIn("receipt:", ev)
        self.assertIn("DONE", ev)

    # ---- evidence.md deduplication: two tasks → two receipt lines ----------

    def test_evidence_md_two_tasks_two_receipt_lines(self) -> None:
        _, project_dir = _make_project(self.root, tasks=[_todo_task("t1"), _todo_task("t2")])
        adapter = FakeAdapter()
        run_sequential_task(project_dir, adapter)  # runs t1
        run_sequential_task(project_dir, adapter)  # runs t2
        ev = (project_dir / "evidence.md").read_text(encoding="utf-8")
        self.assertEqual(2, ev.count("receipt:"))

    # ---- task with done dependency is READY --------------------------------

    def test_task_with_done_dependency_is_ready(self) -> None:
        done = _done_task("t0")
        todo = _todo_task("t1", depends_on=["t0"])
        _, project_dir = _make_project(self.root, tasks=[done, todo])
        adapter = FakeAdapter()
        result = run_sequential_task(project_dir, adapter)
        self.assertTrue(result)
        state = self._state(project_dir)
        # t0 still DONE, t1 now DONE
        self.assertEqual("DONE", state["tasks"][0]["status"])
        self.assertEqual("DONE", state["tasks"][1]["status"])

    # ---- reopening: second run on same project after first completes --------

    def test_second_run_on_completed_project_returns_false(self) -> None:
        _, project_dir = _make_project(self.root)
        adapter = FakeAdapter()
        ran = run_sequential_task(project_dir, adapter)  # completes the one task
        self.assertTrue(ran)
        # All tasks done; second run returns False
        ran2 = run_sequential_task(project_dir, adapter)
        self.assertFalse(ran2)


# ===========================================================================
# Old-reader refusal after activation
# ===========================================================================


class ReaderOnlyRefusalTests(unittest.TestCase):
    """workspace_lib.commit_candidate refuses when coordinator_run or attempts are set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        ws = root / "ws"
        self.project_dir = ws / "2026-09-15-001"
        self.project_dir.mkdir(parents=True)
        for sub in ("reviews", "tasks", "artifacts"):
            (self.project_dir / sub).mkdir()
        content = (
            "# fn\n\n## Current specification\n\nContent.\n\n"
            "## Decision history\n\nDecision.\n"
        )
        for fn in ("spec.md", "evidence.md", "reflection.md", "briefing.md"):
            (self.project_dir / fn).write_text(content, encoding="utf-8")
        target = root / "target"
        target.mkdir()
        (target / "out.txt").write_text("result\n", encoding="utf-8")
        self.target_dir = target

    def _v4_state(self, *, coordinator_run: object = None, attempts: dict | None = None) -> dict:
        return {
            "schema_version": 4,
            "project": "2026-09-15-001",
            "title": "Test",
            "status": "EXECUTING",
            "created": TIMESTAMP,
            "updated": TIMESTAMP,
            "working_directory": str(self.target_dir),
            "revision": 1,
            "current_tasks": [],
            "review": {
                "cycle": 0,
                "required": False,
                "status": "not_required",
                "evidence": [],
            },
            "cancellation_reason": None,
            "tasks": [],
            "execution": {
                "protocol_version": 1,
                "coordinator_run": coordinator_run,
                "ownership_generation": 1,
                "attempts": attempts or {},
            },
        }

    def _write_candidate(self, state: dict) -> Path:
        path = self.project_dir / "candidate.json"
        candidate = dict(state)
        candidate["revision"] = state["revision"] + 1
        path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        return path

    def _write_project(self, state: dict) -> None:
        (self.project_dir / "project.json").write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    def test_commit_candidate_refuses_active_coordinator_run(self) -> None:
        state = self._v4_state(coordinator_run="run-xyz")
        self._write_project(state)
        candidate_path = self._write_candidate(state)
        with self.assertRaises(WorkspaceError) as ctx:
            commit_candidate(self.project_dir, candidate_path, expected_revision=1)
        self.assertEqual(READER_ONLY_REFUSAL, str(ctx.exception))

    def test_commit_candidate_refuses_non_empty_attempts(self) -> None:
        state = self._v4_state(attempts={"t1": "att-abc"})
        self._write_project(state)
        candidate_path = self._write_candidate(state)
        with self.assertRaises(WorkspaceError) as ctx:
            commit_candidate(self.project_dir, candidate_path, expected_revision=1)
        self.assertEqual(READER_ONLY_REFUSAL, str(ctx.exception))

    def test_commit_candidate_allows_quiesced_v4(self) -> None:
        state = self._v4_state()  # coordinator_run=None, attempts={}
        self._write_project(state)
        # Candidate just bumps title (valid change)
        candidate = dict(state)
        candidate["revision"] = 2
        candidate["title"] = "Updated title"
        candidate_path = self.project_dir / "candidate.json"
        candidate_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        # Should NOT raise READER_ONLY_REFUSAL (may raise for other validation, but not that)
        try:
            commit_candidate(self.project_dir, candidate_path, expected_revision=1)
        except WorkspaceError as e:
            self.assertNotEqual(READER_ONLY_REFUSAL, str(e))


# ===========================================================================
# run-once CLI handler
# ===========================================================================


class RunOnceCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_once_ran_true_returns_0(self) -> None:
        _, project_dir = _make_project(self.root)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            result = _call_manage(["run-once", str(project_dir)])
        self.assertEqual(0, result)
        self.assertIn("Task completed", out.getvalue())
        self.assertIn(str(project_dir.resolve()), out.getvalue())

    def test_run_once_no_ready_task_returns_2(self) -> None:
        _, project_dir = _make_project(self.root, tasks=[_done_task("t0")])
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            result = _call_manage(["run-once", str(project_dir)])
        self.assertEqual(2, result)
        self.assertIn("No READY task found", err.getvalue())

    def test_run_once_operation_error_returns_1(self) -> None:
        _, project_dir = _make_project(self.root)
        with patch("manage_workspace.run_sequential_task", side_effect=OperationError("lock busy")):
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                result = _call_manage(["run-once", str(project_dir)])
        self.assertEqual(1, result)
        self.assertIn("lock busy", err.getvalue())


if __name__ == "__main__":
    unittest.main()
