"""Fake host adapter for the parallel execution protocol (§17, Phase 4 testing).

The adapter protocol has four operations: start, observe, seal, terminate.  This module
ships a FakeAdapter whose responses are injected up-front — one queue per handle for
observe and seal — so tests can exercise delayed starts, ambiguous starts, unknown
outcomes, failed checks, late results, and configurable seal attestations without any
real process management.

Phase 5 will replace this with a real POSIX adapter (startling_new_session + waitpid +
killpg), proven by the Phase 2 feasibility spike.  FakeAdapter shares the protocol so
the same test infrastructure works in both phases.
"""
from __future__ import annotations

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
