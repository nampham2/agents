#!/usr/bin/env python3
"""
posix_adapter_probe.py — Phase 2 feasibility spike: POSIX subprocess adapter

Probes all four adapter operations defined in §17 of
plugins/research/skills/project/references/parallel-execution.md against a
simple worker that writes a file to a temporary directory.

Operations probed:
  start(plan, attempt_dir) -> handle
  observe(handle) -> running | finished | unknown
  seal(handle) -> attestation
  terminate(handle)

Requires: Python 3.9+, POSIX OS (macOS or Linux), stdlib only.

Exit 0 if every operation produces the expected result.
Exit 1 on any unexpected failure.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Adapter types
# ---------------------------------------------------------------------------

RUNNING = "running"
FINISHED = "finished"
UNKNOWN = "unknown"


class Handle:
    """Identifies a started worker: process id and its process group id."""
    def __init__(self, pid: int, pgid: int) -> None:
        self.pid = pid
        self.pgid = pgid
        self._reaped: bool = False  # set by seal/terminate after waitpid returns

    def mark_reaped(self) -> None:
        self._reaped = True

    def __repr__(self) -> str:
        return f"Handle(pid={self.pid}, pgid={self.pgid}, reaped={self._reaped})"


class Attestation:
    """Proof that the process tree exited and cannot be resumed."""
    def __init__(self, pgid: int, waited: bool) -> None:
        self.pgid = pgid
        self.waited = waited  # True: waitpid returned; False: kill(pgid,0) raised ProcessLookupError

    def __repr__(self) -> str:
        return f"Attestation(pgid={self.pgid}, waited={self.waited})"


# ---------------------------------------------------------------------------
# Adapter operations
# ---------------------------------------------------------------------------

def start(worker_cmd: list[str], attempt_dir: str) -> Handle:
    """
    start(plan, attempt_dir) -> handle

    Spawn the worker in a new process group so the whole tree can be
    reaped atomically.  Returns immediately; worker runs asynchronously.
    """
    proc = subprocess.Popen(
        worker_cmd,
        stdout=open(os.path.join(attempt_dir, "stdout.txt"), "w"),
        stderr=open(os.path.join(attempt_dir, "stderr.txt"), "w"),
        start_new_session=True,   # creates a new process group (pgid == pid)
        cwd=attempt_dir,
    )
    pgid = os.getpgid(proc.pid)
    return Handle(pid=proc.pid, pgid=pgid)


def observe(handle: Handle) -> str:
    """
    observe(handle) -> running | finished | unknown

    Non-destructive liveness check.  kill(pid, 0) tests process existence
    without sending a signal.  A ProcessLookupError means the process is gone.

    Note: 'finished' here means 'no longer running', NOT 'sealed'.
    §17.3 prohibits conflating observe(finished) with seal(attestation).
    """
    # A child that has exited but not yet been reaped (zombie) still appears in
    # the kernel process table, so kill(pid, 0) returns 0 — a false RUNNING.
    # Track reap state explicitly (set by seal/terminate after waitpid) to
    # return the correct FINISHED answer without a destructive waitpid here.
    if handle._reaped:
        return FINISHED
    try:
        os.kill(handle.pid, 0)
        return RUNNING
    except ProcessLookupError:
        return FINISHED
    except PermissionError:
        # Process exists under a different uid — should not happen for our own children
        return UNKNOWN


def seal(handle: Handle) -> Optional[Attestation]:
    """
    seal(handle) -> attestation | None

    Attest that the process tree has exited AND cannot be restarted.

    Strategy: waitpid on the leader first (collects the exit status),
    then confirm the group is gone via kill(pgid, 0).  Both clauses of
    §17's seal definition must hold before we return an Attestation.

    Returns None if the process is still running (seal precondition unmet).
    """
    try:
        # Wait for the process group leader
        pid, _ = os.waitpid(handle.pid, os.WNOHANG)
        if pid == 0:
            # Leader still running — precondition unmet
            return None
        # Leader exited; confirm group is gone
        try:
            os.killpg(handle.pgid, 0)
            # Group still exists (orphaned children?) — not fully sealed
            return None
        except ProcessLookupError:
            # Group gone: both clauses satisfied
            handle.mark_reaped()
            return Attestation(pgid=handle.pgid, waited=True)
    except ChildProcessError:
        # Already reaped (e.g. SIGCHLD handler collected it)
        try:
            os.killpg(handle.pgid, 0)
            return None  # group still exists
        except ProcessLookupError:
            handle.mark_reaped()
            return Attestation(pgid=handle.pgid, waited=False)


def terminate(handle: Handle) -> None:
    """
    terminate(handle)

    Request termination of the whole tree via SIGTERM to the process group,
    then wait for the leader to confirm exit.  Falls back to SIGKILL if the
    leader has not exited within 2 s.
    """
    try:
        os.killpg(handle.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # already gone

    # Wait for the group leader specifically; avoids pgid-reuse false positives
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(handle.pid, os.WNOHANG)
            if pid != 0:
                handle.mark_reaped()
                return  # leader exited after SIGTERM
        except ChildProcessError:
            handle.mark_reaped()
            return  # already reaped
        time.sleep(0.05)

    # Leader still alive — force kill the group
    try:
        os.killpg(handle.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    # Reap the leader after SIGKILL
    try:
        os.waitpid(handle.pid, 0)
        handle.mark_reaped()
    except ChildProcessError:
        pass


# ---------------------------------------------------------------------------
# Probe runs
# ---------------------------------------------------------------------------

def run_probe() -> dict[str, Any]:
    """
    Execute all four operations against a trivial worker and return
    observed outcomes keyed by operation name.
    """
    results: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="phase2-posix-probe-") as attempt_dir:

        # Worker: write a canary file and exit cleanly
        worker = [
            sys.executable, "-c",
            "import time, sys; "
            "open('/tmp/posix-probe-canary.txt', 'w').write('canary'); "
            "time.sleep(0.1); "     # brief pause so observe() can see RUNNING
            "sys.exit(0)"
        ]

        # --- start ---
        handle = start(worker, attempt_dir)
        results["start"] = {
            "outcome": "started",
            "pid": handle.pid,
            "pgid": handle.pgid,
            "same_pgid": handle.pid == handle.pgid,
        }

        # --- observe (while running) ---
        obs_while_running = observe(handle)
        results["observe_while_running"] = obs_while_running

        # Wait for the worker to finish, then seal (which reaps the zombie)
        time.sleep(0.5)

        # --- seal (reaps the zombie; must precede observe_after_exit) ---
        attestation = seal(handle)
        results["seal"] = {
            "outcome": "attested" if attestation is not None else "precondition_unmet",
            "attestation": repr(attestation),
        }

        # --- observe (after seal has reaped) ---
        # Note: on POSIX, kill(pid, 0) returns 0 for un-reaped zombies, so
        # observe() correctly returns FINISHED only after seal() has called
        # waitpid and marked the handle reaped.
        obs_after_exit = observe(handle)
        results["observe_after_exit"] = obs_after_exit

        canary_written = os.path.exists("/tmp/posix-probe-canary.txt")
        results["canary_written"] = canary_written

    # --- terminate (separate run against a long-lived worker) ---
    with tempfile.TemporaryDirectory(prefix="phase2-posix-term-") as attempt_dir:
        long_worker = [sys.executable, "-c", "import time; time.sleep(30)"]
        handle2 = start(long_worker, attempt_dir)

        # Confirm it is running
        results["terminate_observe_before"] = observe(handle2)

        terminate(handle2)
        time.sleep(0.2)

        results["terminate_observe_after"] = observe(handle2)

    return results


def main() -> int:
    print("=== POSIX adapter probe ===")
    results = run_probe()

    all_pass = True

    def check(label: str, got: object, want: object) -> None:
        nonlocal all_pass
        ok = got == want
        if not ok:
            all_pass = False
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: got={got!r}, want={want!r}")

    print("\nstart:")
    print(f"  [INFO] pid={results['start']['pid']} pgid={results['start']['pgid']}")
    check("start: new session (pid==pgid)", results["start"]["same_pgid"], True)

    print("\nobserve:")
    check("observe while running -> running", results["observe_while_running"], RUNNING)
    check("observe after exit -> finished", results["observe_after_exit"], FINISHED)

    print("\nseal:")
    seal_res = results["seal"]
    check("seal: attested", seal_res["outcome"], "attested")
    print(f"  [INFO] {seal_res['attestation']}")
    check("canary written by worker", results["canary_written"], True)

    print("\nterminate:")
    check("observe before terminate -> running", results["terminate_observe_before"], RUNNING)
    check("observe after terminate -> finished", results["terminate_observe_after"], FINISHED)

    print()
    if all_pass:
        print("All checks passed.")
        return 0
    else:
        print("One or more checks failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
