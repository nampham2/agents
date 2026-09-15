"""Adapters for the parallel execution protocol (§17).

The adapter protocol has four operations: start, observe, seal, terminate.  This module
ships two implementations:

  FakeAdapter   — queue-based controllable fake for testing; injected responses per handle
  SubprocessAdapter — production POSIX adapter; spawns a real child process per task attempt

Phase 4/5 used FakeAdapter.  Phase 6 introduces SubprocessAdapter for production use.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdapterError(RuntimeError):
    """Raised when the adapter cannot perform the requested operation."""


# ---------------------------------------------------------------------------
# Return-value sentinels (§17, §8.2)
# ---------------------------------------------------------------------------

START_AMBIGUOUS = "ambiguous"   # start returned ambiguous; capability was consumed

OBSERVE_RUNNING = "running"
OBSERVE_FINISHED = "finished"
OBSERVE_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# FakeAdapter
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Controllable fake adapter for Phase 4 testing.

    All queues are drain-first: the first enqueued item is the first result returned.
    The final item in a queue is **sticky** — it repeats once the queue is exhausted.
    An empty queue defaults to a hard-coded safe value (see each method's docstring).

    Queues are keyed by handle string for observe and seal; start maintains its own
    ordered queue irrespective of what handle will be returned.

    Usage::

        adapter = FakeAdapter()
        adapter.enqueue_start("h-1")            # first start returns "h-1"
        adapter.enqueue_start(START_AMBIGUOUS)  # second start returns ambiguous
        adapter.enqueue_observe("h-1", OBSERVE_RUNNING)
        adapter.enqueue_observe("h-1", OBSERVE_FINISHED)
        adapter.enqueue_seal("h-1", {"tree_exited": True, "resumable": False})
    """

    def __init__(self) -> None:
        self._start_queue: List[str] = []
        self._observe_queues: Dict[str, List[str]] = {}
        self._seal_queues: Dict[str, List[Dict[str, Any]]] = {}
        self._terminate_raises: Optional[AdapterError] = None

        # Inspection records — tests may assert on these.
        self.started: List[Dict[str, Any]] = []     # one entry per start call
        self.observed: List[str] = []               # handles passed to observe
        self.sealed: List[str] = []                 # handles passed to seal
        self.terminated: List[str] = []             # handles passed to terminate

    # ------------------------------------------------------------------
    # Queue-loading helpers
    # ------------------------------------------------------------------

    def enqueue_start(self, result: str) -> "FakeAdapter":
        """Append a start result.  result is a handle string or START_AMBIGUOUS."""
        self._start_queue.append(result)
        return self

    def enqueue_observe(self, handle: str, result: str) -> "FakeAdapter":
        """Append an observe result for handle."""
        self._observe_queues.setdefault(handle, []).append(result)
        return self

    def enqueue_seal(self, handle: str, attestation: "Dict[str, Any]") -> "FakeAdapter":
        """Append a seal attestation for handle."""
        self._seal_queues.setdefault(handle, []).append(dict(attestation))
        return self

    def set_terminate_raises(self, exc: Optional[AdapterError] = None) -> "FakeAdapter":
        """If exc is not None, terminate will raise it on every call."""
        self._terminate_raises = exc or AdapterError("fake adapter: terminate refused")
        return self

    # ------------------------------------------------------------------
    # Protocol implementation (§17)
    # ------------------------------------------------------------------

    def start(self, plan: "Dict[str, Any]", attempt_dir: str) -> str:
        """Begin execution.  Returns a handle string, or START_AMBIGUOUS.

        If the start queue is empty, returns a handle derived from attempt_dir so
        the default case requires no configuration.  Never raises: a start that
        cannot proceed returns START_AMBIGUOUS rather than raising.
        """
        if self._start_queue:
            result = self._start_queue.pop(0)
        else:
            result = "handle-" + attempt_dir.replace("/", "_").replace("\\", "_")[-40:]
        self.started.append({"plan": plan, "attempt_dir": attempt_dir, "handle": result})
        return result

    def observe(self, handle: str) -> str:
        """Report liveness without side-effects.  Returns OBSERVE_RUNNING/FINISHED/UNKNOWN.

        Default when no result is queued: OBSERVE_UNKNOWN (the safe direction — the caller
        cannot terminate or release without more information).
        """
        self.observed.append(handle)
        queue = self._observe_queues.get(handle, [])
        if not queue:
            return OBSERVE_UNKNOWN
        result = queue.pop(0)
        if not queue:
            queue.append(result)  # make sticky
            self._observe_queues[handle] = queue
        return result

    def seal(self, handle: str) -> "Dict[str, Any]":
        """Attest that the process tree exited and is not resumable (I2 barrier).

        Default when no attestation is queued: raises AdapterError so tests that
        do not configure a seal cannot accidentally pass I2's check.
        """
        self.sealed.append(handle)
        queue = self._seal_queues.get(handle, [])
        if not queue:
            raise AdapterError(f"fake adapter: no seal result queued for {handle!r}")
        attestation = queue.pop(0)
        if not queue:
            queue.append(attestation)  # make sticky
            self._seal_queues[handle] = queue
        return attestation

    def terminate(self, handle: str) -> None:
        """Request termination of the whole process tree.  Idempotent by handle (§8.3)."""
        self.terminated.append(handle)
        if self._terminate_raises is not None:
            raise self._terminate_raises


# ---------------------------------------------------------------------------
# SubprocessAdapter
# ---------------------------------------------------------------------------


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class _ProcState:
    __slots__ = ("attempt_dir", "exit_code", "pgid", "pid", "reaped")

    def __init__(self, pid: int, pgid: int, attempt_dir: str) -> None:
        self.pid = pid
        self.pgid = pgid
        self.reaped: bool = False
        self.exit_code: Optional[int] = None
        self.attempt_dir = attempt_dir


class SubprocessAdapter:
    """Production POSIX adapter that spawns a real child process per task attempt.

    The command to run is injected at construction.  The ``plan`` dict passed to
    ``start`` is reserved for future overrides and is currently ignored.

    Handles are opaque strings encoding ``"pid:<N>:pgid:<N>"``; internal state is
    tracked per handle so the protocol methods remain stateless from the caller's
    perspective.
    """

    def __init__(self, command: List[str]) -> None:
        self._command = list(command)
        self._procs: Dict[str, _ProcState] = {}

    def start(self, plan: Dict[str, Any], attempt_dir: str) -> str:
        stdout_path = os.path.join(attempt_dir, "stdout.txt")
        stderr_path = os.path.join(attempt_dir, "stderr.txt")
        with open(stdout_path, "w") as stdout_fh, open(stderr_path, "w") as stderr_fh:
            proc = subprocess.Popen(
                self._command,
                stdout=stdout_fh,
                stderr=stderr_fh,
                start_new_session=True,
                cwd=attempt_dir,
            )
        pid = proc.pid
        pgid = os.getpgid(pid)
        handle = f"pid:{pid}:pgid:{pgid}"
        self._procs[handle] = _ProcState(pid, pgid, attempt_dir)
        return handle

    def observe(self, handle: str) -> str:
        state = self._procs.get(handle)
        if state is None:
            return OBSERVE_UNKNOWN
        if state.reaped:
            return OBSERVE_FINISHED
        try:
            os.kill(state.pid, 0)
        except ProcessLookupError:
            return OBSERVE_FINISHED
        except PermissionError:
            return OBSERVE_UNKNOWN
        # Process exists in the kernel table (may be a zombie). Use waitpid(WNOHANG)
        # to detect an un-reaped exit without blocking.
        try:
            pid, wstatus = os.waitpid(state.pid, os.WNOHANG)
            if pid != 0:
                state.exit_code = os.waitstatus_to_exitcode(wstatus)
                state.reaped = True
                try:
                    os.killpg(state.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return OBSERVE_FINISHED
        except ChildProcessError:
            state.reaped = True
            return OBSERVE_FINISHED
        return OBSERVE_RUNNING

    def seal(self, handle: str) -> Dict[str, Any]:
        state = self._procs.get(handle)
        if state is None:
            raise AdapterError(f"unknown handle: {handle!r}")

        if state.reaped and state.exit_code is not None:
            return {
                "variant": "exited",
                "exit_code": state.exit_code,
                "stdout_bytes": _file_size(os.path.join(state.attempt_dir, "stdout.txt")),
                "stderr_bytes": _file_size(os.path.join(state.attempt_dir, "stderr.txt")),
            }

        try:
            pid, wstatus = os.waitpid(state.pid, os.WNOHANG)
        except ChildProcessError:
            raise AdapterError(f"process {state.pid} was already reaped externally") from None

        if pid == 0:
            raise AdapterError(f"process {state.pid} has not exited yet")

        exit_code = os.waitstatus_to_exitcode(wstatus)

        try:
            os.killpg(state.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        state.reaped = True
        state.exit_code = exit_code

        return {
            "variant": "exited",
            "exit_code": exit_code,
            "stdout_bytes": _file_size(os.path.join(state.attempt_dir, "stdout.txt")),
            "stderr_bytes": _file_size(os.path.join(state.attempt_dir, "stderr.txt")),
        }

    def terminate(self, handle: str) -> None:
        state = self._procs.get(handle)
        if state is None or state.reaped:
            return

        try:
            os.killpg(state.pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            state.reaped = True
            return

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(state.pid, os.WNOHANG)
                if pid != 0:
                    state.reaped = True
                    return
            except ChildProcessError:
                state.reaped = True
                return
            time.sleep(0.05)

        try:
            os.killpg(state.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            state.reaped = True
            return

        try:
            os.waitpid(state.pid, 0)
        except ChildProcessError:
            pass
        state.reaped = True
