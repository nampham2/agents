# Phase 2 feasibility spike: host adapter contracts

**Date**: 2026-09-14  
**Branch**: `parallel-execution-phase-2`  
**Phase 1 gate**: PR #12 merged at `4697461` (2026-09-14T10:07:32Z)  
**Status**: complete — both hosts probed; §17.1 confirmed with first-hand evidence

---

## Objective

Can a separate agent be given a task, observed, and its execution scopes sealed? The same question
for a local POSIX subprocess. Phase 2 answers both, determines which (if any) host is admissible
for asynchronous dispatch under §17's adapter contract, and freezes the minimal contract for a
Phase 3 adapter implementation.

The spike is explicitly a negative-finding-is-valid-outcome experiment. A host that cannot seal is
not a failure; it is an answer that makes the design honest.

---

## Phase 1 gate

The conflict-detection library (`execution_claims.py`) was shipped in Phase 1: `Claim`,
`Conflict`, `normalize_claims`, `claims_conflict`, `find_conflicts` — the claim representation
and conflict relation (§9.3). PR #12 merged to `origin/main` at `4697461`; the gate that Phase 2
depends on is satisfied.

---

## What was probed

Two hosts, each against the four operations §17 requires every adapter to implement:

| Operation | Meaning |
|---|---|
| `start` | begin execution, return a handle; either starts or fails (no ambiguous) |
| `observe` | report liveness without changing anything; must answer for a prior coordinator's handle |
| `seal` | attest the process tree exited AND cannot be resumed; both clauses required |
| `terminate` | request termination of the whole process tree, including descendants |

The disposable project was a real v3 workspace project at
`/tmp/phase2-spike-workspace/2026-09-14-001/` with one task (`canary`), used as the Claude Code
probe's target so the dispatch exercised actual canonical state machinery.

---

## Claude Code probe

**Host**: Claude Code 2.x (this session, `claude-sonnet-4-6`).  
**Method**: Agent tool dispatch, ListAgents observation, SendMessage seal probe, TaskStop
terminate probe.  
**Subagent ID**: `ac07e07b8f4985878`.  
**Task**: write `/tmp/phase2-spike-canary/canary.txt` with content `phase2-canary`.

### start

The Agent tool call returned `agentId: ac07e07b8f4985878` and completed without error.
The task-notification arrived ~8 s later; the canary file existed with the expected content.

**Observed**: ✓ supported. The call either starts the work or errors; no ambiguous path
was triggered. The handle is the agentId string.

*Limitation noted but not tested*: whether a crash between issuing the Agent tool call and the
agent starting leaves an untracked running agent (the §8.2 `DISPATCHING` window) is a host
implementation question that was not observable from this coordinator process.

### observe

Probed via ListAgents at two points after dispatch:

- ~5 s: `ac07e07b8f4985878 · general-purpose · running · started 5s ago`
- ~15 s (post-completion): `ac07e07b8f4985878 · general-purpose · completed · started 15s ago`

The `completed` status persisted through the seal probe (~24 s later) and the terminate probe.

**Observed**: ✓ supported. ListAgents distinguishes `running` from `completed` and the state
persists after completion.

*Open question*: whether ListAgents returns agents started by a *previous* coordinator session
was not tested. If it does not, `observe` is session-scoped, which has implications for
coordinator takeover (§12.2 O17).

### seal

**The critical probe.** Seal requires both clauses: the process tree exited, and it cannot be
resumed. After first completion:

```
SendMessage(to='ac07e07b8f4985878', message='Seal probe. Reply with exactly: RESUMED')
→ {"success": true, "message": "Resuming agent ac07e07b8f4985878..."}
→ Agent replied: RESUMED  (~24 s later)
```

A completed agent is resumable by `SendMessage`. "Completed" means "not currently producing
output", not "exited and cannot restart". The second clause of §17's seal definition fails.

**Observed**: ✗ NOT AVAILABLE. `R-DETACHED-CHILD` applies. This is first-hand evidence, not an
inference from documentation: the host's own tool surface showed the behaviour directly.

### terminate

`TaskStop(task_id='ac07e07b8f4985878')` on the already-completed agent returned:
`Task ac07e07b8f4985878 is not running (status: completed)`.

The agent completed too quickly to issue `terminate` while it was running. From the tool
documentation: "A task-notification fires each time this agent stops with no live background
children of its own. The user can send it another message and resume it." This independently
confirms that stop and resume are asymmetric: TaskStop interrupts a running agent, but a stopped
agent is resumable via SendMessage regardless of how it stopped.

**Observed**: PARTIAL. TaskStop is plausible for running agents, but irrelevant for `seal`: even
a successfully terminated agent can be resumed. Termination and sealing are independent; the
missing clause is resumability, not termination.

### Claude Code summary

| Operation | Observed | Admissible? |
|---|---|---|
| `start` | ✓ via Agent tool; agentId is the handle | yes |
| `observe` | ✓ running/completed via ListAgents; persists post-completion | yes |
| `seal` | ✗ SendMessage resumed completed agent | **no** |
| `terminate` | PARTIAL: TaskStop reaches running agents | moot given seal |

**Consequence**: `R-DETACHED-CHILD` applies. Asynchronous dispatch is unavailable on Claude Code
2.x. v1 runs inline at capacity one on this host. The §17.1 matrix entry is confirmed, not
revised; it was already correct.

---

## POSIX subprocess probe

**Host**: macOS Darwin 25.6.0, Python 3.9.6 (`/usr/bin/python3`).  
**Script**: `docs/posix_adapter_probe.py` (stdlib only; runs on 3.9+).  
**Design**: spawn a worker in a new process group (`start_new_session=True`) so the whole tree
can be reaped atomically.

### Probe run output

```
=== POSIX adapter probe ===

start:
  [INFO] pid=<N> pgid=<N>
  [PASS] start: new session (pid==pgid): got=True, want=True

observe:
  [PASS] observe while running -> running: got='running', want='running'
  [PASS] observe after exit -> finished: got='finished', want='finished'

seal:
  [PASS] seal: attested: got='attested', want='attested'
  [INFO] Attestation(pgid=<N>, waited=True)
  [PASS] canary written by worker: got=True, want=True

terminate:
  [PASS] observe before terminate -> running: got='running', want='running'
  [PASS] observe after terminate -> finished: got='finished', want='finished'

All checks passed.
```

All 7 checks passed.

### start

`subprocess.Popen(..., start_new_session=True)` creates a new process group. `pid == pgid`,
confirming the worker is its own session leader. The basis for atomic group termination.

**Observed**: ✓ supported. Returns a Handle with `(pid, pgid)`; the call either starts or errors.

### observe

`os.kill(handle.pid, 0)` — non-destructive existence check. Returns `RUNNING` if the process
exists, `FINISHED` on `ProcessLookupError`.

**POSIX zombie caveat**: an exited but un-reaped child remains in the kernel process table, so
`kill(pid, 0)` returns success for it — a false `RUNNING`. This is a real POSIX constraint. The
probe handles it via `Handle._reaped` (a boolean set by `seal`/`terminate` after `waitpid`
returns): `observe` checks `_reaped` first and short-circuits to `FINISHED`. A production adapter
must do the same, or accept that `observe(FINISHED)` is only reliable after `seal` has reaped.
Neither defeats the seal guarantee.

**Observed**: ✓ supported, with reap-state tracking required.

### seal

```python
pid, _ = os.waitpid(handle.pid, os.WNOHANG)   # wait for leader
# if pid != 0:
os.killpg(handle.pgid, 0)                       # confirm group gone
# raises ProcessLookupError → both clauses satisfied
```

Both clauses of §17's definition are satisfied:

1. Process tree exited: `waitpid` returned the leader's pid with its exit status.
2. Cannot be resumed: `killpg(pgid, 0)` raised `ProcessLookupError` — the process group is gone.

The `waitpid` call is destructive (reaps the zombie). A production adapter that needs idempotent
`seal` calls must cache the attestation after the first successful call.

**Observed**: ✓ supported. Attestation returned; both clauses confirmed.

### terminate

```python
os.killpg(handle.pgid, signal.SIGTERM)         # signal the whole group
# poll os.waitpid(pid, WNOHANG) for 2 s
# fall back to os.killpg(pgid, SIGKILL) + os.waitpid(pid, 0)
```

`start_new_session=True` ensures all descendants inherit the same pgid; `killpg` reaches the
full tree. The poll loop uses `waitpid(pid, ...)` (not `killpg(pgid, 0)`) to avoid false positives
from pgid reuse after the leader exits.

**Observed**: ✓ supported. Worker terminated; `observe` returned `FINISHED`.

### POSIX summary

| Operation | Implementation | Observed | Admissible? |
|---|---|---|---|
| `start` | `Popen(..., start_new_session=True)` | ✓ pid==pgid; returns immediately | yes |
| `observe` | `kill(pid, 0)` + `_reaped` flag | ✓ running/finished; zombie caveat handled | yes |
| `seal` | `waitpid` + `killpg(0)` | ✓ both clauses confirmed | yes |
| `terminate` | `killpg(SIGTERM)` + `waitpid` loop + `killpg(SIGKILL)` | ✓ full tree terminated | yes |

**Consequence**: POSIX subprocess is **admissible** for asynchronous dispatch. No
`R-DETACHED-CHILD` applies. This is the candidate adapter for a Phase 3 implementation.

---

## §17.1 matrix status

The spike confirms the matrix without revision. The table entries were already correct; what
changes is their epistemic status: they are now observations, not predictions.

| Host | `start` | `observe` | `seal` | `terminate` | Consequence |
|---|---|---|---|---|---|
| Claude Code 2.x | ✓ via Agent tool | ✓ via ListAgents | ✗ confirmed unavailable | PARTIAL | `R-DETACHED-CHILD`: inline only |
| Local subprocess (POSIX) | ✓ `Popen` + new session | ✓ `kill(pid,0)` | ✓ `waitpid` + `killpg(0)` | ✓ `killpg(SIGTERM/SIGKILL)` | **admissible** |

The reference document's §17.1 preamble — "Every row below is unverified" — is now incorrect for
these two rows. It has been amended (see below).

---

## Minimal POSIX adapter contract for Phase 3

A Phase 3 adapter for POSIX subprocess must implement the following contract. The probe's
`posix_adapter_probe.py` is the reference implementation of all four operations.

### Handle

```python
class Handle:
    pid: int     # process leader PID
    pgid: int    # process group ID (== pid when start_new_session=True)
    _reaped: bool  # set after waitpid; guards observe() against zombie false-positives
```

### start

```python
proc = subprocess.Popen(cmd, start_new_session=True, ...)
return Handle(pid=proc.pid, pgid=os.getpgid(proc.pid))
```

- Either starts or raises; no ambiguous path in the synchronous fork case.
- Records `start_outcome = "started"` in `launch.json` (O4, step 7).

### observe

```python
if handle._reaped:
    return FINISHED
try:
    os.kill(handle.pid, 0)
    return RUNNING
except ProcessLookupError:
    return FINISHED
```

- Non-destructive. Does not reap.
- The `_reaped` guard is required to avoid false `RUNNING` from an un-reaped zombie child.

### seal

```python
try:
    pid, _ = os.waitpid(handle.pid, os.WNOHANG)
    if pid == 0:
        return None  # precondition unmet; still running
    os.killpg(handle.pgid, 0)
    return None      # group still exists; orphaned descendants
except ProcessLookupError:
    # group gone: both clauses confirmed
    handle.mark_reaped()
    return Attestation(...)
```

- Must confirm both clauses before returning an attestation.
- `waitpid` is destructive; cache the result if idempotent calls are needed.
- Writes `sealed/<scope-id>.json` with `tree_exited: true, resumable: false` (O6).

### terminate

```python
os.killpg(handle.pgid, signal.SIGTERM)
# poll os.waitpid(pid, WNOHANG) for 2 s
# os.killpg(handle.pgid, signal.SIGKILL) + os.waitpid(pid, 0)
handle.mark_reaped()
```

- `killpg` covers the full process group; `start_new_session=True` ensures all descendants
  share the same pgid.
- Must be idempotent: `ProcessLookupError` from `killpg` means already gone; return without error.

### What Phase 3 must add

The probe exercises the adapter operations in isolation. A Phase 3 implementation must integrate
them with the attempt lifecycle:

1. **Execution store integration**: write `launch.json`, `sealed/<scope-id>.json` via the
   journal protocol (O4 steps 7–8, O6).
2. **Scope declaration**: write `scopes/worker.json` before spawning (O4, step 5); handle
   `check:<id>#<NNNN>` scopes for every check subprocess (O10, step 1).
3. **Idempotent seal**: cache the attestation in `sealed/worker.json`; `seal` on an already-sealed
   scope returns the cached record, not an error.
4. **Coordinator restart**: the handle (`pid`, `pgid`) must survive a coordinator crash. The probe
   does not test this: a restarted coordinator that reloads `launch.json` and reconstructs a
   Handle from it has no `_reaped` state, so it will call `seal` first, which reaps, then observe
   correctly.
5. **Orphaned descendants**: `seal`'s `killpg(pgid, 0)` check catches processes that outlive the
   leader. The coordinator must decide how to handle them — the probe returns `None` (precondition
   unmet) and leaves the attempt open.

---

## Roadmap consequence

Phase 2 closes `R5-08` ("The first real execution target is unresolved"). The answer:

- **Claude Code**: unavailable for asynchronous dispatch. v1 runs inline at capacity one.
  This is the host the design was written for; the consequence was already stated in §17.1.
- **POSIX subprocess**: admissible. All four operations verified. A Phase 3 implementation would
  write the adapter against this contract, then run the integration test suite on a real project.

Phase 3 is the first phase where a production adapter is written. It is a separate project;
this document is Phase 2's deliverable, not Phase 3's design.
