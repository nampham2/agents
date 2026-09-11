# Parallel task execution

> **Status: design awaiting implementation.** Nothing in this repository implements this protocol.
> No script, schema field, MCP server, or `SKILL.md` step exists for it, and this document is not
> linked from `SKILL.md` precisely so that no coordinator is told to follow a protocol the tooling
> cannot execute. It is a specification for a successor project to build against. Until that project
> ships, `research:project` executes tasks sequentially and delegation is out of policy.
>
> **Revision 3, 2026-09-11.** This document is the normative reference: current rules, operations and
> failure behaviour. The revision history, the measurement error that selected revision 1's
> architecture, the disposition of both expert reviews, and the rejected and deferred alternatives are
> at [`docs/parallel-execution-decisions.md`](../../../../../docs/parallel-execution-decisions.md).
> Read that document to learn why a rule is what it is; read this one to implement it.
>
> Its structural claims are checked by `tests/plugins/research/test_parallel_execution_doc.py`, which
> re-reads every cited source range rather than trusting §21.

## 1. Scope and objective

A protocol for executing the tasks of one `research:project` plan **concurrently**: independent tasks
dispatched by one coordinator to Claude Code subagents, with results published through a durable inbox
and integrated into canonical state by that same coordinator, which remains the sole writer of it.

It is a protocol, not an engine. The machinery it needs mostly exists already; §3 says exactly what.

The objective is **lower completion time and lower coordinator context consumption, at preserved
quality and controlled total cost.** Two things follow that are easy to get wrong:

- **Wider plans are a technique, not the goal.** §2 shows this workspace's plans are shaped like
  chains, so guidance on authoring is part of the deliverable (§16) — but a plan fragmented to raise
  average level width trades one coherent artifact for a metric, and increases assembly and review
  work while the number goes up.
- **Context economy is a hypothesis with a benchmark attached, not an established benefit.** A
  worker's tool output never enters the coordinator's context window, and on a long project that is
  plausibly the larger win. Nobody has measured it. §20 says what would settle it, and until that
  runs, this document may not sell it as fact.

## 2. What the measurement says, before any design

Dependency levels were computed for all 31 projects in the reference workspace with five or more
tasks, and achievable speedup modelled with effect-bearing tasks serialized on the coordinator.

| Quantity | Value |
|---|---|
| Mean average dependency-level width | 1.86 |
| Mean maximum level width | 3.87 |
| Fully linear projects (width 1.0 throughout) | 2 of 31 |
| Modelled speedup, unlimited workers | 1.45x mean, 1.33x median, 2.21x best, 1.00x worst |
| Projects at or below 1.2x | 13 of 31 |
| Concurrency cap 2 / 4 / 8 / unlimited | 1.24x / 1.41x / 1.44x / 1.45x mean |

The model is **generous**: it charges a batch the cost of one task rather than its slowest member, and
charges nothing for spawning, prompt authoring, result integration, extra commits, or contention.
Loaded honestly, some of these projects would run *slower* in parallel.

**What this measures, and what it does not.** Dependency-level width is a *structural* property of a
plan. Achieved scheduling depends on task durations, resource conflicts, verification and integration
cost, and host capacity — none of which appears above. So the table is evidence against extravagant
speedup claims and evidence for a modest ceiling. It is not evidence that any particular worker count
is optimal, and this document does not claim it is. The script and graph fixtures behind the table are
committed by Stage 2 (§20.2); until then, the figures are reproducible in principle and
not yet reproducible in this repository.

Three consequences shape everything below.

- **Wall-clock speedup is not the justification.** A design sold on 1.45x, measured optimistically,
  against real overheads it does not count, would not survive contact with a real project.
- **Four is a cheap ceiling.** It is within 0.04x of unlimited across all 31 projects, and mean
  maximum level width is 3.87, so a higher bound buys nothing this sample can see. §8.4 states it as
  a ceiling with that reason, not as a required worker count and not as an optimum.
- **The structural limit this sample demonstrates is in the plans, not in any executor.** Executor
  overhead was never measured, so no comparison between the two is available; what the sample shows is
  that these plans leave little concurrency to exploit. §16 exists for that reason.

A worked example: the project that produced this document has 5 tasks in 5 levels — average width
exactly 1.0. Fetch a branch, write a document, check it changed nothing, commit it, publish it. Each
step genuinely requires the previous one. **Not every plan can be widened**, and a design that
assumes otherwise mistakes a property of the work for a defect in the planner.

## 3. What already exists

The protocol is thin because most of its foundations are shipped and tested.

- **Concurrent `RUNNING` tasks are already legal.** `current_tasks` is a list validated against the
  exact set of `RUNNING` task ids, not against a maximum of one [C1]. Two running tasks need no
  schema change.
- **The scheduler's input is already computed.** `_dependency_levels` assigns each task a level by
  Kahn's algorithm — 0 with no prerequisite, else one past the deepest [C2] — and `build_task_graph`
  orders tasks by `(level, id)` [C3] while `TaskGraph.levels` counts distinct levels [C4]. Levels are
  useful for *presentation*; §8 schedules on satisfied dependencies rather than on level barriers.
- **The ownership model is already normative.** One coordinator owns writes to `project.json`, shared
  records and `INDEX.md`; workers may write only assigned non-overlapping output paths and must not
  edit canonical state [C5], stated identically in the schema reference [C6]. The planner is already
  told to use a dependency graph only where independent work can run in parallel, to assign
  non-overlapping outputs, and to keep one canonical writer [C7].
- **Cross-process safety already exists.** A `mkdir`-based `DirectoryLock` [C8] plus
  `commit --expected-revision` gives optimistic concurrency: two writers means one loses, reloads,
  and reconciles.
- **Single-file atomic replacement already exists and is correct.** `atomic_write_text` writes a
  sibling temporary file, flushes it, `fsync`s the file descriptor, `os.replace`s it into position and
  then `fsync`s the containing directory [C9]; `atomic_write_json` is that function plus
  `json.dumps`. A reader never observes a half-written file. What it does not provide is **ordering
  across files**, which is why §7.1 exists.

So six things are missing, and they are what this document supplies: a **scheduler**, a **worker
contract**, a **publication and acceptance protocol**, a **recovery contract**, a **resource model**,
and a **host-adapter contract**.

## 4. Architecture

```
                 ┌──────────────────────────────────────────────┐
   canonical ────│  project.json   (revision-checked commit)     │  coordinator writes only
                 └──────────────────────────────────────────────┘
                                    ▲ integrates accepted results
                                    │
                 ┌──────────────────────────────────────────────┐
   durable   ────│  <project-dir>/execution/                    │  coordinator + assigned writers
                 │  attempts/ · receipts/ · runtime.json        │
                 └──────────────────────────────────────────────┘
                     ▲ dispatch (down)      ▲ result (up, published per §7.1)
                     │                      │
              ┌──────┴─────┐  ┌────────────┐  ┌────────────┐
              │  worker 1  │  │  worker 2  │  │  worker N  │   N ≤ 4 (§8.4)
              └────────────┘  └────────────┘  └────────────┘
                 filesystem mode chosen per §9, not assumed shared
```

Three properties define it:

1. **The coordinator assigns; workers do not claim.** At four workers the coordinator already owns
   every decision before execution (eligibility, authorization, resources, capacity) and after it
   (verification, evidence, commit). A worker discovering its own next task adds a distributed
   protocol to a problem that has none.
2. **The middle layer is durable, not derived.** Its justification is *recovery*: a coordinator that
   dies holding an uncommitted result must not lose completed work. That means the store's contents
   are classified (§5.4), retained under a stated policy (§5.5), and published in a defined order
   (§7.1).
3. **There is one scheduler.** Sequential execution is this scheduler at capacity one — not a second
   mechanism. A separate wave-synchronous path would be the code that runs on every degraded host and
   is therefore exercised least, which is how a fallback rots.

## 5. The execution store

### 5.1 Layout

```text
<project-dir>/execution/
    attempts/<attempt-id>/
        dispatch.json              # coordinator-written, immutable once released
        state.json                 # coordinator-written attempt state, the only mutable record here
        captures/<capture-id>/     # one directory per verification capture
            capture.json
            stdout.log
            stderr.log
        result.json                # published last, by the assigned writer (§7.1)
    receipts/<receipt-id>.json     # immutable result-and-verification receipts
    ack/<receipt-id>.json          # coordinator acknowledgement, written after the canonical commit
    coordinator.lock/              # ownership guard (§12)
    runtime.json                   # coordinator bookkeeping
```

Inside the project directory, matching how every existing lock and file is scoped: it keeps a
project's execution record inside the directory that *is* its record, and keeps concurrent projects
from sharing a mutable file. `<attempt-id>` is opaque, unique per attempt, and never reused — a
retried task has several attempt directories, not one overwritten pair of timestamps.

**One capture directory per verification command.** A task with two checks, or one check that was run
twice, produces two capture directories, both preserved. A single `verification.json` beside a single
pair of logs cannot represent that, and silently overwrites a failed first attempt with a passing
second one. `result.json` carries a manifest naming the capture ids it relies on.

**Ownership within the store.** The coordinator owns `dispatch.json`, `state.json`, `runtime.json`,
`receipts/`, `ack/` and `coordinator.lock/`. Exactly one assigned writer — a worker, or the capture
helper acting for it (§11) — owns `result.json` and everything under `captures/` **for its own
attempt**. That sentence scopes ownership inside the execution store only: the same worker also writes
the declared outputs its resource grant allows (§10), which are not store records.

**Atomic visibility.** Every record is written to a temporary path in the same directory and made
visible by atomic replacement [C9], so no reader ever observes a half-written record. Ordering
*between* records is not a property of atomic replacement and is specified separately in §7.1.

### 5.2 Record schemas

Every record is a JSON object carrying `schema` (§5.3) and the fields below. Fields marked optional
may be absent; a field that is present is never `null` unless the table says so.

**`dispatch.json`** — the coordinator's assignment, immutable from the moment the host is called.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `dispatch/<major>.<minor>` |
| `project` | string | Project id, as in `project.json` |
| `task` | string | Canonical task id |
| `attempt` | string | Attempt id, unique and never reused |
| `coordinator_run` | string | The coordinator run identity that created it (§12) |
| `generation` | integer | Attempt generation, monotonic per task |
| `contract` | object | The execution contract object (§6.4), embedded verbatim |
| `contract_hash` | string | `sha256` over the canonical serialization of `contract` |
| `grant` | array | Resource claims the worker may touch, each `{namespace, ref, mode}` (§8.2) |
| `mode` | string | Filesystem mode (§9) |
| `deadline` | object | `{observation_seconds, heartbeat_seconds}`; `heartbeat_seconds` optional |
| `result_path` | string | Absolute path the worker publishes `result.json` to |
| `created` | string | RFC 3339, timezone-aware |

**`state.json`** — the coordinator's view of the attempt. The only record in `attempts/` it rewrites.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `attempt-state/<major>.<minor>` |
| `attempt` | string | Attempt id |
| `state` | string | One of the states in §6.1 |
| `handle` | string or null | Host handle, absent until recorded; `null` means "no handle exists", which is not the same as absent (§6.2) |
| `launch_key` | string or null | Idempotent launch key, when the host supports one (§17) |
| `reservations` | array | Resource claims currently held |
| `transitions` | array | Append-only `{at, from, to, event}` entries |
| `unresolved_reason` | string, optional | Present in `UNCERTAIN` and `QUARANTINED` |

**`captures/<capture-id>/capture.json`** — one execution of one check.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `capture/<major>.<minor>` |
| `capture` | string | Capture id, unique within the attempt |
| `attempt` | string | Attempt id it was produced under |
| `contract_hash` | string | The contract the capture was taken against |
| `method` | string | `command`, `inspection` or `review` (§11) |
| `actor` | string | `coordinator`, `worker` or `human` (§11) |
| `argv` | array, optional | Present when `method` is `command`: the actual argument vector |
| `cwd` | string, optional | Present when `method` is `command`: the resolved working directory |
| `assessment` | string, optional | Present when `method` is `inspection` or `review` |
| `exit_status` | integer or null | `null` when the process did not run to completion |
| `failure` | string, optional | `launch_error`, `timeout`, `decode_error`, `output_overflow` |
| `started` / `ended` | string | RFC 3339, timezone-aware |
| `duration_seconds` | number | Measured on a monotonic clock |
| `subject_digests` | object | Path → `sha256`, taken **after** the check ran, for every artifact the check was required to cover |
| `stdout_ref` / `stderr_ref` | object | `{path, bytes, sha256, truncated}` |

A capture is written **for every outcome**, including a launch error and a timeout. A check that could
not run is a recorded fact, not an absence.

**`result.json`** — the worker's manifest, published last.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `result/<major>.<minor>` |
| `attempt` | string | Attempt id |
| `contract_hash` | string | Must equal the dispatch's |
| `outcome` | string | `done`, `failed` or `blocked` |
| `reason` | string, optional | Required for `failed` and `blocked` |
| `artifacts` | array | `{namespace, ref, sha256}` per declared output actually produced |
| `captures` | array | `{capture, sha256}` per capture directory this result relies on |
| `handoff` | string | A concise summary for the coordinator's context, not a transcript |
| `published` | string | RFC 3339, timezone-aware |

**`receipts/<receipt-id>.json`** — immutable, and the identity the canonical record points at.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `receipt/<major>.<minor>` |
| `receipt` | string | `receipt-id`, equal to the `sha256` of this object with `receipt` removed |
| `attempt` / `task` / `project` | string | Identity of what was executed |
| `contract_hash` | string | The contract accepted |
| `outcome` | string | Copied from the result |
| `artifacts` / `captures` | array | Copied, with digests |
| `adequacy` | object | The §11.4 comparison and its verdict |
| `accepted_at` | string | When the coordinator accepted it |

**A receipt contains no revision.** It cannot: it is written before the commit that would supply one,
and a receipt whose content changed on a lost revision race would change its own identity and defeat
deduplication. The revision lives in the acknowledgement.

**`ack/<receipt-id>.json`** — written after a successful canonical commit.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `ack/<major>.<minor>` |
| `receipt` | string | The receipt this acknowledges |
| `revision` | integer | The `project.json` revision actually observed after the commit |
| `evidence` | object | The canonical evidence reference committed, `{root, path, anchor}` |
| `acknowledged_at` | string | RFC 3339 |

**`runtime.json`** — coordinator bookkeeping.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `runtime/<major>.<minor>` |
| `coordinator_run` | string | Current run identity (§12) |
| `ownership_generation` | integer | Monotonic; every mutation checks it (§12) |
| `dispatch_paused` | object or null | `{at, reason, attempt}` when paused (§14) |
| `active_attempts` | array | Attempt ids not in a terminal state |
| `reservations` | array | All held claims, with their attempt ids |
| `unresolved` | array | Attempts left `UNCERTAIN` or `QUARANTINED` at a bound (§14) |
| `capacity` | object | The host capacity established at startup (§17) |

Timestamps are timezone-aware RFC 3339, matching the rule canonical state already enforces on
authorization and receipt stamps. **Elapsed durations are measured on a monotonic clock and reported
in seconds**; a wall-clock difference across an NTP step is not a duration, and a duration has no
timezone.

Dispatch, actual start, finish and integration are recorded **separately** wherever each is
observable. Collapsing them loses exactly the distinction the closing report needs (§15).

### 5.3 Schema versioning and reader compatibility

Every record's `schema` is `<name>/<major>.<minor>`. The rules are fail-closed:

- A reader accepts a record whose `major` it knows. Within that major it ignores fields it does not
  recognise, so a `minor` bump is additive and backward-compatible.
- A reader that meets an **unknown `major`** treats the record as **unreadable, not absent.** It does
  not re-prepare, re-dispatch or discard. It pauses dispatch (§14) and reports. Treating an unreadable
  record as a missing one is how a second worker gets launched for work that already ran.
- A record whose JSON does not parse is handled identically to an unknown major.
- `major` increases only when a field's meaning changes or a required field is added.

**The old-installation gate.** A project that has ever enabled this protocol is not safe to operate
with an installed host that predates it: such a host reads `project.json` happily and ignores
`execution/` entirely, so it can commit over active attempts. Enabling the protocol for a project
therefore **bumps `project.json`'s `schema_version`**, which makes an older installation refuse the
project outright — `detect_schema_version` reads the integer and validation returns
`unsupported schema_version` for anything it does not implement [C21]. That refusal is the fail-closed
marker; a sidecar file in `execution/` would not be one, because the old reader never looks there.

### 5.4 Durability classes

| Content | Class | Why |
|---|---|---|
| A mirror of task ids, levels, statuses | **Disposable** | Reconstructible from `project.json` at any revision |
| `dispatch.json` for an attempt whose **non-execution is established** | **Disposable** | Re-preparable. A missing start timestamp does not establish non-execution; §13 says what does |
| `dispatch.json` for an attempt whose execution is unestablished | **Durable** | It is the only record of what a possibly-running worker was told to do |
| `state.json` | **Durable** | It carries the reservations and the handle; losing it loses the ability to reconcile |
| `runtime.json` | **Durable** | It carries the dispatch pause. Losing a pause resumes a run that a failure stopped |
| A reservation record | **Durable** | It is the only evidence that a resource is claimed by an attempt that may still be writing |
| **A published result not yet integrated** | **Durable** | Canonical state cannot reconstruct it. Losing it discards work that was actually performed |
| **A capture not yet imported into evidence** | **Durable** | It attests an execution that happened once |
| A receipt | **Durable, immutable** | It is the identity a restart uses to recognise already-accepted work (§13) |
| An acknowledgement | **Durable, immutable** | It carries the revision that accepted a receipt |

**A store need not be authoritative for task status to hold irreplaceable evidence.** Any later
substrate inherits that obligation: retention and recovery rules, not just a schema.

### 5.5 Retention

Durability says what a crash must not lose. Retention says what a *later* operation may remove, and
they are different questions.

- **Nothing under `execution/` is removed while the project is not `DONE` or `CANCELLED`.** Collection
  is a closure activity, never a scheduling one.
- **A receipt referenced by canonical evidence is never collected**, and neither is any capture it
  names. The reference is a canonical fact; a dangling one would make `research-validate` fail on the
  existence check it already performs [C22]. This is the one retention rule that is mechanically
  observable today.
- **Logs survive closure** when a capture they belong to is referenced. Unreferenced captures — a
  discarded attempt's, a superseded retry's — may be collected at closure, and their collection is
  recorded in the project's evidence rather than performed silently.
- **A missing record that retention permitted** is reported as collected, not as lost. A missing
  record that retention did not permit is a `WorkspaceError`, and the project does not resume
  dispatch until an operator reconciles it.
- **A corrupt record** is handled as an unknown major (§5.3): unreadable, pause, report.

## 6. Attempts

### 6.1 The attempt lifecycle

```text
PREPARED ─→ DISPATCHING ─→ DISPATCHED ─→ RUNNING ─→ RESULT_READY ─→ VERIFIED ─→ INTEGRATED ✓
    │            │              │           │             │             │
    │            │              └───────────┴──→ UNCERTAIN ┤             │
    │            │                                 │       │            │
    │            └──→ LAUNCH_FAILED ✓               │       └──→ QUARANTINED ✓
    │                                               │
    └──→ ABANDONED ✓                                └──→ RECONCILED ✓
```

Terminal states are marked `✓`: `INTEGRATED`, `ABANDONED`, `LAUNCH_FAILED`, `RECONCILED`,
`QUARANTINED`. Nine states in total, and every non-terminal one has at least one row in §6.2 that
leaves it.

These are **execution-attempt** states. They never replace canonical task statuses, and
`project.json` remains the only answer to "what is this task's status" [C1]. A `failed` or `blocked`
result is a first-class result: it reaches `RESULT_READY`, is verified for *adequacy of its record*
rather than for success, and is integrated as a `BLOCKED` canonical task. An outcome must not become
unreachable merely because it cannot follow the successful path.

**The reservation invariant.** Reservations are acquired at `PREPARED` and released on entry to a
terminal state — with one stated exception: `QUARANTINED` holds its reservations until an operator
reconciles it. That is safe only because entering `QUARANTINED` also pauses dispatch (§14), so no
later task is waiting on the held claim. Every other terminal state releases.

### 6.2 The transition table

Each row is an obligation on the implementation and a test in §20.1. "Durable writes" are in the order
they must occur; a crash between any two of them is covered by §13.

| # | Event | From | Preconditions | Durable writes, in order | Canonical transition | Reservations |
|---|---|---|---|---|---|---|
| T1 | Task admitted | — | All seven conditions of §8.1; claims reservable per §8.3 | reservation records → `dispatch.json` → `state.json` `PREPARED` | none | **acquired**, complete set including verification (§8.3) |
| T2 | Preparation discarded | `PREPARED` | Dispatch not yet released; contract invalid, capacity withdrawn, or operator cancellation | `state.json` `ABANDONED` with reason | none; task stays `TODO` | **released** |
| T3 | Canonical `RUNNING` committed | `PREPARED` | `project.json` commit succeeded at the expected revision | canonical commit → `state.json` `DISPATCHING` | `TODO → RUNNING` | held |
| T4 | Host call made | `DISPATCHING` | `state.json` is `DISPATCHING` **before** the call; `launch_key` written if the host supports one | `state.json` `DISPATCHED` with `handle` | none | held |
| T5 | Host refused definitively | `DISPATCHING` | The adapter reports that no worker was started, per §17 | `state.json` `LAUNCH_FAILED` | `RUNNING → TODO` | **released** |
| T6 | Restart finds `DISPATCHING` | `DISPATCHING` | No `handle` recorded; no `launch_key` and no discovery by attempt id | `state.json` `UNCERTAIN`, reason `dispatch_interrupted` | none; task stays `RUNNING` | **held** |
| T7 | Restart finds `DISPATCHING` with a launch key | `DISPATCHING` | Host supports idempotent launch or attempt-id discovery | adapter query → `state.json` `DISPATCHED` or `LAUNCH_FAILED` | per T4 or T5 | held or released accordingly |
| T8 | Worker activity observed | `DISPATCHED` | Adapter reports running, or a capture appears | `state.json` `RUNNING` | none | held |
| T9 | Complete publication observed | `RUNNING`, `DISPATCHED` | `result.json` present, `contract_hash` matches, every named capture present and digest-matching (§7.1) | `state.json` `RESULT_READY` | none | held |
| T10 | Incomplete publication observed | `RUNNING` | `result.json` absent or a named capture missing or digest-mismatched | none; re-read later | none | held |
| T11 | Differing republication | any non-terminal | A `result.json` for this attempt differs from one already observed as complete | `state.json` `QUARANTINED`, reason `conflicting_publication` → dispatch pause | none | **held** until reconciled |
| T12 | Adequacy passed | `RESULT_READY` | §11.4 comparison passes against the frozen contract | `receipts/<id>.json` → `state.json` `VERIFIED` | none | held |
| T13 | Adequacy failed | `RESULT_READY` | Receipt mismatch, inadequate check, malformed result, or capture failure | `state.json` `QUARANTINED` with reason → dispatch pause | none | **held** until reconciled |
| T14 | Integration committed | `VERIFIED` | Canonical candidate carries the evidence reference naming the receipt | canonical commit → `ack/<id>.json` → `state.json` `INTEGRATED` | `RUNNING → DONE` or `→ BLOCKED` | **released** |
| T15 | Observation bound reached | `DISPATCHED`, `RUNNING` | No complete publication and no adapter confirmation within `observation_seconds` | `state.json` `UNCERTAIN`, reason `deadline` → `runtime.json` `unresolved` | none; task stays `RUNNING` | **held** |
| T16 | Contact lost | `DISPATCHED`, `RUNNING` | Adapter reports the worker gone with no complete publication | `state.json` `UNCERTAIN`, reason `worker_gone` | none | **held** |
| T17 | Non-execution established | `UNCERTAIN` | §13's confinement or termination check shows the worker never wrote and has stopped | `state.json` `RECONCILED` | `RUNNING → TODO` | **released** |
| T18 | Late valid result adopted | `UNCERTAIN` | Complete publication, `contract_hash` matches the frozen contract, and **no replacement attempt for this task has reached `INTEGRATED`** | `state.json` `RESULT_READY` | none | held |
| T19 | Late result superseded | `UNCERTAIN` | Complete publication, but a replacement attempt already reached `INTEGRATED` | `state.json` `QUARANTINED`, reason `superseded` | none | held until reconciled |
| T20 | Contract invalidated mid-flight | `DISPATCHED`, `RUNNING`, `RESULT_READY` | The executing task's own definition changed (§6.4) | `state.json` `QUARANTINED`, reason `contract_invalidated` → dispatch pause | none | **held** until reconciled |
| T21 | Authorization withdrawn | any non-terminal | The task's `authorization.status` is no longer `authorized` for the frozen scope | `state.json` `QUARANTINED`, reason `authorization_revoked` → dispatch pause | none | held until reconciled |
| T22 | Operator cancellation | `PREPARED` | — | as T2 | none | **released** |
| T23 | Operator cancellation | `DISPATCHED`, `RUNNING` | In-flight work is never cancelled silently (§14) | `state.json` `UNCERTAIN`, reason `cancelled` | none | **held**, then T17 |
| T24 | Operator cancellation | `RESULT_READY`, `VERIFIED` | The work already happened | proceed to T12/T14 | as T14 | as T14 |
| T25 | Operator reconciliation | `QUARANTINED` | An explicit, dated decision recording the disposition | `state.json` `RECONCILED` or `ABANDONED` | `RUNNING → TODO` or `→ BLOCKED` | **released** |

**Why T24 exists.** Cancelling a project must not discard a result that was produced and verified.
Completed sibling work is committed; discarding a true record to make a tidier cancellation story is
the one thing this protocol may never do.

**Why T19 is not a retry.** A superseded late result is not evidence that the replacement was wrong.
It is quarantined because two executions produced two artifact sets and only one is in canonical
state; reconciliation is a human reading, not a rule.

### 6.3 Attempt identity, and what it is not

Every heartbeat and every submission carries the **attempt id and generation the coordinator
issued**. A message whose identity does not match a live attempt for that task is rejected as stale —
with the explicit exception of T18, where a result whose contract hash matches a still-unreplaced
`UNCERTAIN` attempt is exactly the record recovery needs. "Live attempt" alone would reject it.

**[UNENFORCED]** Attempt identity rejects *stale protocol messages*. It is not filesystem isolation
and must never be described as such. A worker with shell access to the same user-owned files can
write outside its assignment, and can generally invoke the launcher too, unless the host actually
restricts its tools. Tokens and tool restrictions prevent accidental misuse *within* the protocol.
Isolation, where it is needed, comes from §9.

Attempt identity is separate from the coordinator's ownership generation (§12). A takeover raises the
ownership generation and does **not** raise attempt generations, because a takeover must be able to
adopt the results of workers the previous coordinator launched.

### 6.4 The execution contract object

The contract is a versioned object embedded verbatim in `dispatch.json`, and the hash is over that
object. Hashing a list of ingredients described in prose is how an execution-relevant value gets
forgotten.

| Field | Source | Why it is in the contract |
|---|---|---|
| `task` | canonical task id | Identity |
| `name` | canonical `name` | It is in the worker's prompt; changing it changes the instruction |
| `depends_on` | canonical `depends_on` | A changed dependency set changes what "ready" meant |
| `success_criteria` | canonical | The standard the result is judged against |
| `verification` | canonical | The check that must pass |
| `verification_method` | derived (§6.5) | `command`, `inspection` or `review` (§11) |
| `verification_argv` | derived (§6.5) | Present when the method is `command` |
| `effect` | canonical `effect.kind` | Governs delegability (§8.1) |
| `authorization_scope` | canonical `authorization.scope` | Bounds what the worker may do |
| `outputs` | canonical `outputs`, **resolved** | The declared deliverables |
| `resolved_roots` | derived | `target`, `workspace`, `workspace_root` mapped to absolute real paths |
| `working_directory` | project-level `working_directory` | A project-level change relocates every `target` output |
| `resources` | derived (§6.5) | The claim set, including verification's (§8.3) |
| `mode` | scheduler decision | Filesystem mode (§9) |
| `input_identities` | derived | Path → `sha256` for every mutable input the task reads |

`resolved_roots` and `outputs` are both present on purpose: hashing a textual output reference such as
`target:docs/x.md` does not detect that `target` now resolves somewhere else.

**Serialization and hashing.** The contract is serialized as UTF-8 JSON with keys sorted
lexicographically at every level, no insignificant whitespace, `ensure_ascii` false, and numbers in
their shortest round-trip form. Absent optional values are **omitted**, never written as `null`, so
"absent" and "null" cannot hash alike. The hash is `sha256` over those bytes, lowercase hex. A field
added in a later minor version participates in the hash, so the contract object carries its own
`schema` and an attempt's hash is only ever compared against a hash computed at the same major.

The rules that follow:

- The contract is **frozen for the life of the attempt**. A result is accepted only against the
  contract it ran under.
- **A sibling completing does not invalidate an attempt.** The project revision increments for
  legitimate unrelated reasons; treating a revision bump as invalidation would make concurrency
  self-defeating. Invalidation is decided by recomputing the contract for *this* task and comparing
  hashes, never by comparing revisions.
- If the recomputed hash differs, the attempt is **invalidated and its result quarantined** (T20) —
  not silently accepted, and not silently dropped.
- **Authorization revocation is a separate rule, not a hash comparison.** A frozen contract hash can
  still match after the user has withdrawn authorization, because `authorization.status` is not what
  the contract commits to; the contract commits to the *scope*. The coordinator therefore re-checks
  `authorization.status` against the frozen `authorization_scope` at every one of T4, T12 and T14, and
  a revoked authorization quarantines the attempt (T21). A matching hash never authorizes an action.

### 6.5 Deriving contract fields that canonical tasks do not carry

Canonical tasks have no structured resources, no input identities and no verification method: the
schema's `verification` is one free-text field defined as a "Command, inspection, or review that
demonstrates success" [C12]. So three contract fields are **derived**, and the derivation is itself
recorded.

| Field | Derivation | Recorded as |
|---|---|---|
| `verification_method` | `command` when the coordinator can parse the string into an argument vector without a shell; `review` when the task's own text asks for an independent reader; `inspection` otherwise | `contract.derivation.verification_method = {rule, input}` |
| `verification_argv` | The parsed vector. A string that needs shell metacharacters is **not** silently wrapped in `bash -lc`; it is either declared `inspection` or rejected at admission with an actionable error | `contract.derivation.verification_argv = {parser_version, input}` |
| `resources` | The union of resolved `outputs`, the resolved paths named in `verification_argv`, the coarse exclusive resource when the check is repository-wide, and every `input_identities` key as a read claim | `contract.derivation.resources = {rule_version, contributions}` |

`contract.derivation` is part of the hashed object. Two consequences, both intended: a change to the
derivation rules invalidates in-flight attempts, which is correct because they were prepared under
different rules; and a reader of a receipt can see *why* a resource was claimed, rather than having to
re-derive it.

**[UNENFORCED]** A derived resource set is a lower bound. It cannot see a resource the task touches
without declaring, and §10 says what that means.

## 7. Publication and acceptance

### 7.1 The publication protocol

Atomic replacement makes one file's contents safe [C9]. It gives no ordering across files, and it
explicitly *permits* replacement, so on its own it provides neither the completeness nor the
immutability this protocol needs. Publication is therefore a defined sequence:

1. **Write every log and capture first.** Each `captures/<capture-id>/` directory is completed —
   `stdout.log`, `stderr.log`, then `capture.json` — before anything refers to it.
2. **Finalize and hash.** `capture.json` carries the `sha256` of each log it names and of every
   artifact in `subject_digests`. After this step the capture directory is never written again.
3. **Publish the manifest last.** `result.json` names each capture id with the `sha256` of its
   `capture.json`, and each artifact with its digest. Because it is published last and atomically,
   its presence is the signal that everything it references is complete.
4. **An identical repeated publication is success.** Byte-identical `result.json` for the same
   attempt is idempotent: the publisher retried, and the coordinator accepts.
5. **A differing publication for the same attempt is rejected**, not applied (T11). The attempt is
   quarantined and dispatch pauses. "Last writer wins" here would silently discard a result the
   coordinator may already have verified.
6. **Incomplete publication is distinguishable from a valid failed result.** A `failed` outcome is a
   present, complete `result.json` whose `outcome` is `failed`. An interrupted publication is a
   *missing or unreferenceable* manifest. The coordinator never infers failure from absence (T10).

The coordinator's completeness check is exactly: `result.json` parses at a known major, its
`contract_hash` equals the dispatch's, and every capture and artifact it names is present with a
matching digest. Anything else is T10 or T11.

### 7.2 Three records, three questions

The reviewed revision folded these together and made acceptance unrecoverable. They are separate:

| Record | Answers | Identity depends on |
|---|---|---|
| **Receipt** (§5.2) | "What was executed and verified?" | Its own content only — never on a future revision |
| **Canonical evidence reference** in `project.json` | "Which receipt did this project accept?" | The receipt id, committed in the task's `evidence` |
| **Acknowledgement** (`ack/`) | "Which revision accepted it?" | The revision observed *after* the commit returned |

So a lost revision race costs a retry of the commit and nothing else: the receipt is unchanged, its id
is unchanged, and evidence deduplication still works. And recovery reads acceptance from the
**canonical reference**, never from a receipt's self-report — finding a receipt on disk establishes
that a result was verified, not that any commit accepted it.

**[UNENFORCED]** `_validate_evidence_reference` checks an evidence reference's shape and that the file
exists [C22]. It does not check receipt semantics, digests, or that a named anchor is present.
Validating receipts is an explicit implementation task in Stage 3 (§19), not a property inherited from
the current validator.

### 7.3 Acceptance

Acceptance is the coordinator's, and it is a comparison rather than a reading:

1. Publication is complete (§7.1).
2. The contract hash matches the frozen contract (§6.4), and authorization is still in force (T21).
3. The adequacy comparison of §11.4 passes.
4. A receipt is written. Its id is the digest of its own content, so writing it twice is idempotent.
5. The canonical candidate is built with the task's `evidence` naming that receipt, and committed.
6. The acknowledgement is written.

Evidence appending is idempotent on the receipt id: a restart that finds evidence appended but no
acknowledgement retries the commit without writing a second entry.

## 8. Scheduling

### 8.1 Eligibility

A task is **admissible** when all seven of these hold:

1. Its own status is `TODO` (or `BLOCKED` and explicitly being retried) — not `RUNNING`, `DONE` or
   `SKIPPED`.
2. Every id in `depends_on` is `DONE` in canonical state.
3. Its authorization requirements are satisfied. Effects of kind `destructive` or `external` stay
   with the coordinator, because authorization must be confirmed immediately before the action [C16]
   and confirmed explicit, current and exactly scoped at the moment the task starts [C17] — a
   subagent cannot hold that conversation with the user. **A `local_write` task that nevertheless
   records `authorization.required` is equally undelegable**, since the rule keys on the requirement,
   not on the effect kind.
4. No other attempt for it is active, `UNCERTAIN`, or awaiting reconciliation.
5. The project permits execution and dispatch is not paused (§14).
6. Its complete resource claim set can be reserved (§8.3).
7. Host capacity is available (§17).

### 8.2 Resources

A claim is `{namespace, ref, mode}`.

**Namespaces**, because a URL has no absolute path and pretending otherwise loses information:

| Namespace | `ref` | Comparison |
|---|---|---|
| `path` | An absolute, resolved filesystem path | Ancestry-aware, per the rules below |
| `external` | A durable identifier: a URL, a pull request number, a remote ref, a BigQuery table name | Exact string equality after a namespace-specific normalization, never path ancestry |
| `exclusive` | A named coarse resource, e.g. `repo:<resolved-root>`, `git-index:<resolved-root>` | Exact string equality |

**Modes** are `read` and `write`, and the conflict matrix is:

| | other `read` | other `write` |
|---|---|---|
| **`read`** | compatible | **conflict** |
| **`write`** | **conflict** | **conflict** |

Two readers never conflict — including two readers of the same directory, and a reader of a directory
alongside a reader of a file inside it. Only a writer creates a conflict, which is what makes a survey
of five subjects (§16) admissible five-wide.

**Path comparison rules:**

- **Resolve before comparing.** `target`, `workspace`, `workspace_root` and `external` references
  become absolute paths first; two spellings of one location are one resource.
- **Resolve symlinks, and record both forms.** The claim carries the resolved path; ancestry is
  computed on the resolved path so a symlinked directory cannot alias past a reservation.
- **A missing path is claimable.** Most write claims name files that do not exist yet. Resolution
  walks to the nearest existing ancestor, resolves that, and appends the remainder literally — it
  never fails because a leaf is absent.
- **Directory ancestry is overlap for writers.** A `write` claim on a directory conflicts with any
  claim on anything beneath it, in either direction.
- **Case sensitivity follows the filesystem, and is probed rather than assumed.** On a
  case-insensitive volume — the macOS default — comparison is case-folded, so `Docs/x.md` and
  `docs/x.md` are one resource. The probe result is recorded in `runtime.json`; assuming either
  behaviour is a correctness bug on one of the two platforms this repository supports [C13].
- **Canonical and coordinator-owned records are protected by one rooted policy**, stated once here and
  referenced everywhere else rather than re-listed:

  ```text
  PROTECTED = { <project-dir>/project.json, spec.md, evidence.md, briefing.md,
                memory-staging.md, reflection.md,
                <workspace-root>/INDEX.md, <workspace-root>/MEMORY.md,
                <project-dir>/execution/**  except the attempt's own result.json
                                            and its own captures/ subtree,
                <project-dir>/.project.lock, <project-dir>/execution/coordinator.lock }
  ```

  No grant may include a `write` claim on a protected path. A grant that would is a scheduler bug and
  is rejected at admission, not at dispatch. §10 restates the prohibition for the worker's benefit and
  points here for the list.

- **Count reads when the input is mutable.** A reader of a file another attempt writes conflicts by
  the matrix above; that is the whole reason `read` is a mode rather than an annotation.
- **One coarse exclusive resource for repository-wide operations** — a repo-wide test run, lint, or
  anything touching git index or branch state. Modelling git precisely is a research project; taking
  an exclusive resource is correct, cheap, and honest about what it costs.

### 8.3 The acquisition policy

Admission is ordered, complete, and free of upgrades.

```text
Order ready tasks by: ascending dependency level, then ascending id.
For each in that order:
  admit it when its complete claim set conflicts with neither
    the reservations of non-terminal attempts
    nor the claims of tasks already admitted in this dispatch round;
  otherwise leave it ready for the next round.
```

The sort key is stated exactly because "dependency depth" is ambiguous: **ascending
`_dependency_levels` value** [C2], which runs shallowest-first, then ascending task id as the
deterministic tiebreak. Remaining-critical-path-descending is a different policy and is not this one.

**Acquisition is upfront and total; there are no upgrades.** A task's claim set at `PREPARED` includes
everything the *execution and its verification* need, which for a repository-wide check means the
`exclusive` resource is held from `PREPARED`, not acquired later:

> Two tasks, `A` writing `a.py` and `B` writing `b.py`, whose verifications both run the repository
> suite. Under upgrade-on-verify, `A` holds `path:a.py`, `B` holds `path:b.py`, both finish, and
> neither can acquire `exclusive:repo` while the other's write reservation stands. That is a deadlock,
> and it is reachable with two tasks.

Upfront acquisition makes it unreachable: `A` and `B` both claim `exclusive:repo` at admission, so
they conflict at admission and run one after the other. **The cost is real and is the point** — a task
whose verification is repository-wide serializes against every writer, so a plan that gives every task
a repo-wide check gets no concurrency at all. §16 treats task-scoped verification as an authoring
technique for that reason.

Two corollaries:

- **No attempt ever acquires a claim it did not hold at `PREPARED`.** A coordinator that discovers a
  needed resource mid-flight does not upgrade: it quarantines the attempt (T20, the derivation changed)
  and re-prepares with the wider claim set. Quarantine plus re-prepare is slower than an upgrade and
  cannot deadlock.
- **The coordinator's own actions take claims.** Its verification re-runs (§11.3) and its local
  actions are admitted through the same policy against the same reservations. "Coordinator actions
  participate in conflicts" is only meaningful because acquisition order and totality are defined
  here.

The two-task scenario above is a required test (§20.1).

### 8.4 Capacity

**Four concurrent attempts, as a default ceiling.** §2 shows four is within 0.04x of unlimited across
every measured project while mean maximum level width is 3.87, so a higher bound buys nothing this
sample can see, and costs a wider blast radius and more concurrent writers.

Four is **not a required worker count**, and the host may offer fewer: one observed host exposes four
total agent slots *including the coordinator*, leaving three for workers. The scheduler takes its
capacity from the host contract (§17) and treats four as an upper bound on it, never as a target.
Capacity one is the sequential path.

### 8.5 Cross-project targets

Two projects whose `working_directory` resolves to the same tree, or to nested trees, have no shared
scheduling state: per-project reservations give no cross-project guarantee. This protocol therefore
takes a **target claim at the workspace root**, which is the only place two projects can meet:

```text
<workspace-root>/.targets/<sha256 of resolved target root>.lock/
```

- The claim is taken by the coordinator, as a `DirectoryLock` [C8], for the duration of parallel
  execution, and carries the project id and coordinator run identity.
- **Nested roots conflict.** The claim set is the resolved target root plus every ancestor up to the
  filesystem root, compared by the ancestry rule of §8.2, so a project targeting `/repo` and one
  targeting `/repo/plugins` conflict.
- **Failure to acquire is not an error.** The project runs at **capacity one** — the sequential path,
  which is what it would have done before this protocol existed.

**[UNENFORCED]** This governs concurrency introduced by *this protocol*. Two coordinators running
sequentially in one target collide exactly as much as they do today; the claim does not fix that, and
this document does not claim it does.

## 9. Filesystem modes

Isolation is a property introduced progressively, and the mode is part of an attempt's contract:

| Mode | Suitable first use | Limits to state plainly | Status |
|---|---|---|---|
| Read-only workers, separate result files | Surveys, comparative analysis, independent review | Readers still observe inputs that other attempts may change, which is why a mutable input is a `read` claim (§8.2), and each worker still owns its own result path exclusively | **Ships first** |
| Shared target with explicit reservations | Cooperative workers producing bounded, disjoint files | Reservations are enforced by admission, not by the filesystem | **Ships first** |
| Git worktrees | Repository tasks benefiting from independent working copies and integration checks | Separates working copies; **does not** prevent a worker from reading or writing other paths. Turns integration into a merge | Available, per project |
| Isolated staging directories | Plain-directory projects wanting safer retries and controlled promotion | See below | **Deferred** |

**Staging is deferred, not offered.** A staging directory is a changed working directory, and a changed
working directory is not a confinement capability: a worker can still write an absolute path outside
it, or leave a subprocess running that does. That is the same limitation stated for worktrees, and it
means staging cannot license the automatic retry of §13. Staging returns only with two things this
revision does not specify: a **verified confinement capability** from the host contract (§17), and a
**promotion protocol** — re-check that the target baseline still matches, identify which outputs were
already promoted before an interruption, and define recovery after a partial multi-file promotion.

## 10. The worker contract

A dispatch record gives a worker: its task id, name, success criteria and verification definition
verbatim from canonical state; its resource grant, as the complete list of what it may touch; its
attempt id and generation; its observation deadline and any heartbeat obligation; and its filesystem
mode.

A worker publishes exactly one `result.json` at its assigned path, through the sequence in §7.1, plus
the captures and logs for its own attempt. It returns a **concise handoff** rather than a transcript:
keeping worker output out of the coordinator's context is half the objective (§1).

The prohibitions:

- **Never write canonical state**, nor any other protected path. The list is §8.2's `PROTECTED` set,
  stated once there so the two cannot drift apart.
- **Never write outside the resource grant.**

**[UNENFORCED]** Both are prompt text plus, at best, host tool restriction. A worker with a shell can
write canonical state; this document does not claim otherwise.

**[UNENFORCED]** A grant is also not verifiable as *complete*: an undeclared write is invisible to any
inference over declarations. A post-task comparison of touched paths against the grant is worth
building as **diagnostics** — and must not be presented as enforcement: with several concurrent
attempts it cannot reliably attribute a change to one of them, and it cannot see a write that was
later reverted.

## 11. Verification: method, actor, capture, adequacy

### 11.1 Three independent axes

The reviewed revision collapsed these into one ranking and forced a coordinator's own inspection into
a class named for workers. They are independent:

| Axis | Values | What it says |
|---|---|---|
| `method` | `command`, `inspection`, `review` | What kind of check it is |
| `actor` | `coordinator`, `worker`, `human` | Who performed it |
| `capture` | `process-record`, `structured-assessment` | What kind of record exists |

Every capture declares all three, and the closing report shows all three. Admissible combinations:

| `method` | `actor` | `capture` | Admissible |
|---|---|---|---|
| `command` | `coordinator` | `process-record` | Yes. The coordinator ran it through the existing capture path |
| `command` | `worker` | `process-record` | Yes, when the command is inside the grant |
| `command` | any | `structured-assessment` | **No.** A command with no process record is a claim about a command |
| `inspection` | `coordinator` | `structured-assessment` | Yes |
| `inspection` | `worker` | `structured-assessment` | Yes, when the contract's method is `inspection` |
| `review` | `human` | `structured-assessment` | Yes, and the only combination that satisfies a contract requiring independent review |
| `review` | `worker` | `structured-assessment` | **No** for a task requiring independent review; the worker would be reviewing itself |
| `review` | `coordinator` | `structured-assessment` | Self-review, recorded as such; never satisfies an independent-review requirement |

**What a process record establishes, and what it does not.** A `process-record` establishes which
argument vector ran, in which directory, and what it returned. It does not establish that the command
was the *required* check, or that it examined the artifact that was submitted. Those are §11.4's job.
So there is no strength ranking here: a coordinator can execute an inadequate command and get a
perfect process record, and an independent human review can be much stronger substantive evidence than
either. Provenance and adequacy are different questions, and a report that conflates them overstates
itself.

### 11.2 Capture, then import

1. A **capture helper** runs the actual command and records its argument vector, working directory,
   start and end times, exit status, output references, and the digests of the artifacts the check was
   required to cover — taken *after* the command ran.
2. It emits a structured **capture** bound to the attempt and contract identities.
3. The coordinator **imports** it into `evidence.md` without re-running the command.
4. The evidence entry names the capture's `method`, `actor` and `capture` values.

No step asks a language model to author its own process result: the exit status in a capture came from
a process, not from a sentence.

**[UNENFORCED]** A capture written into a worker-writable location is not tamper-proof. This document
states that limit rather than implying otherwise; strengthening it is what §9's later modes and a
host's tool restrictions are for.

### 11.3 Re-running, and when it is valid

Coordinator re-execution stays available and is the right choice for a cheap independent check. One
condition: it must run **against stable inputs**, which is why reservations are held through
integration (§6.1). A re-run performed while sibling attempts are modifying the tree checks a
different state from the one the worker produced. The re-run takes its own claims through §8.3.

### 11.4 The adequacy comparison

Before a receipt is written, the coordinator compares the capture against the frozen contract. All of
these must hold, and a failure quarantines the attempt (T13):

| Check | Compares |
|---|---|
| Method | `capture.method` equals `contract.verification_method` |
| Command identity | `capture.argv` equals `contract.verification_argv`, element by element |
| Working directory | `capture.cwd` equals the contract's resolved working directory |
| Attempt binding | `capture.attempt` and `capture.contract_hash` match the dispatch |
| Subject coverage | `capture.subject_digests` covers every artifact in `result.artifacts` that the contract required the check to examine |
| Subject identity | Each digest in `capture.subject_digests` equals the digest of that artifact **now** |
| Completion | `capture.exit_status` is present and zero, or the result's outcome is `failed`/`blocked` and the capture records the failure mode |
| Actor adequacy | The `method`/`actor`/`capture` triple is admissible per §11.1 for this contract |

Two failures this catches, both of which a bare process record passes: a capture for `true`, or for
any command other than the required one, fails **command identity**; a passing test run followed by a
modification of the artifact fails **subject identity**, because the digest recorded after the check
no longer matches the artifact being submitted.

## 12. Coordinator identity and lock order

### 12.1 Run identity and ownership generation

A PID is not an ownership token for a session-long coordinator: the CLI process that took the lock
exits while the coordinator keeps working, and a PID-based lock would look stale while the owner is
live. Timeout-based takeover has the mirror failure — it can hand ownership to a second coordinator
while the first is still running.

So ownership is two values:

- **`coordinator_run`**, a durable, opaque identity minted once per coordinator session and written
  into `runtime.json`.
- **`ownership_generation`**, a monotonic integer raised only by a successful acquisition or takeover.

The operations:

| Operation | Definition |
|---|---|
| **Acquire** | Create `execution/coordinator.lock/` [C8]. On success, write `runtime.json` with a new `coordinator_run` and `ownership_generation + 1`. |
| **Own** | Every coordinator mutation — of `runtime.json`, any `state.json`, any receipt, and every canonical commit — re-reads `runtime.json` under the store lock and proceeds only if `coordinator_run` and `ownership_generation` are still its own. |
| **Expire** | The lock directory carries a heartbeat file the owner refreshes. A stale heartbeat makes the lock *takeable*; it does not make the previous coordinator stopped. |
| **Take over** | Acquire, raising `ownership_generation`. Then, **before dispatching anything**, walk every non-terminal attempt and apply §13. Attempt generations are untouched (§6.3), so a worker the previous coordinator launched can still be adopted through T18. |
| **Release** | Drop to no active attempts, write `runtime.json`, remove the lock directory. |

**A superseded coordinator cannot act.** Its next mutation fails the ownership check, and it stops
rather than writing. That is enforced for everything reached through the supported protocol and, being
a check rather than a capability restriction, is **[UNENFORCED]** against a process that writes files
directly.

### 12.2 Lock order

Deadlock between the store, canonical state and target claims is prevented by a total order. Locks are
acquired in this order and released in reverse, and no code path takes them out of order:

```text
1. <workspace-root>/.targets/<hash>.lock/       (§8.5, held for the run)
2. <project-dir>/execution/coordinator.lock/    (§12.1, held for the run)
3. <project-dir>/execution/.store.lock/         (short: one store mutation)
4. <project-dir>/.project.lock                  (short: taken inside commit)
```

Two rules that follow, and that the implementation must not violate:

- **The project lock is never held across agent execution or verification.** It is a short lock taken
  inside `commit`, whose default timeout is five seconds [C8].
- **The store lock is never held across a canonical commit.** Level 3 is released before level 4 is
  taken, because `commit` regenerates the index under its own lock after releasing the project lock
  [C10], and holding the store lock across that would serialize every worker behind an index rebuild.

## 13. The recovery contract

The specification is not finished until every interruption has a defined outcome. Each row is an
obligation and a test in §20.1; the transition it produces is named from §6.2.

| Interruption point | Required recovery | Transition |
|---|---|---|
| Attempt `PREPARED`, task not committed `RUNNING` | **No execution permitted.** Reconcile or discard the preparation; never treat a dispatch record as evidence of work | T2 |
| `state.json` is `DISPATCHING`, no handle, host offers no launch key or discovery | **`UNCERTAIN`.** Absence of a handle never authorizes redispatch: the host may have started a worker before the crash | T6 |
| `state.json` is `DISPATCHING`, host offers a launch key or attempt-id discovery | Query the adapter and resolve to `DISPATCHED` or `LAUNCH_FAILED` | T7 |
| Task `RUNNING`, adapter definitively reports no worker was started | Return the task to `TODO` under a new attempt id if retried | T5 |
| Worker finished, coordinator has not read the result | The published result remains **pending** and is read again. Reading never consumes | T9 |
| Publication incomplete | Re-read later; never infer failure from absence | T10 |
| A second, differing `result.json` for one attempt | Quarantine and pause; do not overwrite | T11 |
| Receipt written, canonical commit absent | Retry the commit; the evidence append is idempotent on the receipt id (§7.3) | T14 |
| Canonical commit landed, acknowledgement absent | Recognise the receipt named in the **canonical evidence reference** and write the acknowledgement. **Do not re-run and do not re-record** | T14 |
| Canonical commit landed, index rebuild failed | The execution is committed. Repair the index; `project.json` remains authoritative [C10] | — |
| Worker contact lost, or the observation bound reached | `UNCERTAIN`; **reservations held** until termination or confinement is established (§13.1) | T15, T16 |
| A late, valid result from an `UNCERTAIN` attempt | Adopt it if no replacement was integrated; otherwise quarantine as superseded | T18, T19 |
| The executing task's contract changed | Quarantine for reconciliation | T20 |
| Authorization withdrawn while an attempt is live | Quarantine; a frozen hash is not an authorization | T21 |
| A store record is unreadable or its major is unknown | Treat as unreadable, **not absent**: pause dispatch and report (§5.3) | — |
| A coordinator takeover | Walk every non-terminal attempt through this table before dispatching (§12.1) | — |

### 13.1 Lost contact is not a free retry

An expired deadline does not establish that the previous worker stopped, and an agent can miss a
deadline while legitimately blocked on a long tool call. Rejecting the old worker's eventual
submission protects the record and does nothing about its filesystem writes; two writers in one tree
is exactly the failure the design was meant to prevent.

So:

1. The bound passes, or the adapter reports the worker gone → the attempt becomes `UNCERTAIN`.
2. Its reservations **remain held**.
3. The coordinator establishes that the old execution stopped, and inspects partial outputs.
4. A replacement attempt starts only after that reconciliation, under a new attempt id.

**A host reporting an agent as finished does not establish that everything it launched stopped.** An
agent that started a background process leaves that process running past its own termination, so
"finished" from the adapter satisfies step 3 only when the host's contract says its termination is
recursive (§17). Where it does not, step 3 needs an explicit check of the resources at issue.

**[UNENFORCED]** A missed deadline is not evidence of a stopped worker, and a delivered heartbeat is
not evidence of progress: no deadline distinguishes useful work from a loop.

**Automatic retry without reconciliation requires a verified confinement capability** — a host
capability (§17) establishing that the attempt's writes could not have escaped a disposable location,
and that its descendants are terminated with it. No filesystem mode in §9 currently provides it, so no
automatic retry is available in this revision. Changing a working directory does not qualify.

## 14. Failure semantics and the bounded observation period

On a result of `failed` or `blocked`, or on **any** of the following — coordinator verification
failure, an adequacy failure (T13), a malformed or unreadable result, a receipt or digest mismatch, a
capture failure (`launch_error`, `timeout`, `decode_error`, `output_overflow`), a contract
invalidation (T20), an authorization revocation (T21), or a conflicting republication (T11):

1. The coordinator **persists a dispatch pause** in `runtime.json` before doing anything else, so an
   interruption cannot resume a run the failure should have stopped.
2. In-flight attempts are **not cancelled**; a half-finished task that has already written part of its
   outputs is worse than a finished one. Independent siblings may continue where continuing is
   appropriate.
3. Completed sibling work is committed. It happened, and discarding a true record to make a tidier
   failure story is the one thing this protocol may never do.
4. **Recovery scans for pending failures before admitting anything**, and does not rely on
   `dispatch_paused` alone: a crash between the failure and the pause write would leave the flag clear.
   The scan is over attempt states — any `QUARANTINED` attempt, or any `UNCERTAIN` attempt without a
   recorded reconciliation, pauses dispatch regardless of what `runtime.json` says.

### 14.1 What the bound actually bounds

The design requires no cancellation of in-flight attempts, reservations retained until execution is
reconciled, a bounded shutdown, and the project remaining `EXECUTING` while tasks are `RUNNING`. Those
four are consistent only for a bounded **observation** period. They cannot bound *termination*: if a
worker's status is unknown, nothing the coordinator can do makes it known.

So the bound has a defined terminal action, and it is not completion:

1. Stop waiting.
2. Persist the attempt as `UNCERTAIN` with its reason, and list it in `runtime.json.unresolved`.
3. **Retain its reservations.**
4. Report the unresolved execution to the user, naming the attempt, the task, the resources still
   claimed, and what would establish termination.
5. Exit.

This document does not promise that shutdown or reconciliation completes. An unresolved execution is a
state the protocol can be left in, and saying so is the difference between a bound and a wish.

**The project stays `EXECUTING` while any task is `RUNNING`.** This is not a preference: validation
rejects a `BLOCKED`, `PLANNING`, `ALIGNING`, `REVIEW` or `DONE` project that has `RUNNING` tasks
[C11]. `BLOCKED` is entered only once running work is reconciled and nothing is `RUNNING`, and a
`BLOCKED` project must contain at least one `BLOCKED` task [C11].

## 15. Timing and measurement

The task schema carries no start, no end, and no duration; a span is derived from the stamps
`record_evidence` wrote, and the code says so — it "measures verification, and only for tasks whose
verification was recorded at all" [C18].

**Timing lives first in captures and attempt records, not in the task schema.** Dispatch, start,
finish and integration times are already in the store (§5.2), which makes achieved parallelism
measurable *without* a schema change — so splitting `TASK_FIELDS` is not an implementation dependency.
The cost, stated plainly: readers and reports must be taught to consult these records. The existing
task graph will not discover their timing on its own.

If per-task timing fields are added later:

- `TASK_FIELDS` is used as both the required set and the allowed set in the same two lines [C19]
  [C20], so a field cannot be optional until they are split:

  ```python
  TASK_FIELDS = {...}                                               # allowed
  TASK_REQUIRED_FIELDS = TASK_FIELDS - {"started_at", "ended_at"}   # required
  ```

- **Making a field optional in a new reader does not make new records readable by an old
  installation** that rejects unknown fields. Reader/writer compatibility must be stated explicitly,
  not assumed from the direction of the change — and the fail-closed gate for a project running this
  protocol is the `schema_version` bump of §5.3, not an optional field.
- **Terminal tasks are never backfilled.** A retried task carries several attempt records; there is
  no single correct start time to write into it.

## 16. Authoring wider plans

§2 says these plans, not the executor, are where the measured limit sits, so this section is
load-bearing — as a **technique**, not as an objective (§1). `SKILL.md` already tells the planner to
use a graph only where independent work can run in parallel and to assign non-overlapping outputs
[C7]. What it does not say is how to find that independent work. Five patterns account for most
avoidable narrowing:

1. **A false chain through a shared artifact.** Three tasks each appending a section to one document
   are ordered only because they name the same output path. Give each its own file and add an assembly
   task: three tasks at one level plus a join, instead of a chain of three.
2. **Verification folded into a successor.** A task whose verification is owned by a later task cannot
   be delegated, and cannot even reach `DONE` independently — the commit refuses a `RUNNING` dependent
   of a non-`DONE` dependency. Give every task a check it can run itself.
3. **Repository-wide verification by default.** A task whose check is the whole test suite claims the
   `exclusive` resource from admission (§8.3) and therefore serializes against every writer. A check
   scoped to the task's own subject keeps the task admissible alongside its siblings. This is the
   authoring consequence of the no-upgrade rule, and it is the cheapest width to recover.
4. **A survey serialized by habit.** Reading five subjects to compare them is five independent tasks
   with five `read` claims, which do not conflict (§8.2), and it is the shape that benefits most from
   context economy: five workers' worth of file contents never enter the coordinator's window.
5. **Setup tasks that are actually independent.** Fetching, branching, and scaffolding are often
   ordered by narrative rather than necessity.

And the counter-rule, because this guidance is easy to over-apply: **a plan that cannot be widened
should not be.** Splitting a document into artificial pieces raises average level width while adding
assembly and review work — the metric improves and the objective does not. The project that produced
this document is 5 tasks in 5 levels for good reasons (§2).

## 17. Host adapter contracts

An adapter exposes four operations. Each has a contract, and each has a defined meaning when the host
cannot provide it. The distinction that matters: **a missing worker capability degrades execution; a
missing correctness prerequisite forbids this protocol.**

| Operation | Contract |
|---|---|
| `spawn(dispatch) → handle \| definitive_failure` | Starts one worker with the dispatch's prompt. Must either accept a `launch_key` and be idempotent under it, or provide `discover(attempt)`, or report `definitive_failure` meaning **no worker was started**. An ambiguous error is not a `definitive_failure`; it is T6 |
| `observe(handle) → running \| finished \| gone` | Distinguishes the three. Must state whether `finished` is **recursive** — whether it establishes that processes the worker started have also stopped (§13.1). If it does not, `finished` is not sufficient for reconciliation step 3 |
| `deliver` | The worker can write `dispatch.result_path` and the coordinator can read it, both durably, on the same filesystem the store is on. This is not an operation the coordinator calls; it is a property it must establish |
| `interrupt(handle) → requested` | Requests termination. Its contract is what it guarantees on return — request delivered, or process gone — and whether it is recursive. Nothing in this protocol requires `interrupt` to succeed |

Capabilities and what their absence means:

| Capability | If absent |
|---|---|
| `spawn` | **Inline executor.** The coordinator performs the task itself, at capacity one, and still writes dispatches, captures, receipts and acknowledgements. The protocol's recovery guarantees are preserved; only concurrency is lost |
| `observe` | Concurrency is unavailable: every interruption would be permanently `UNCERTAIN`. Fall back to the inline executor |
| Durable `deliver` | **The protocol does not start.** Without durable result delivery there is no repeatable delivery, no recovery, and no receipt — and capacity one does not supply any of them. Report and stop; do not "degrade" |
| Durable store persistence | **The protocol does not start**, for the same reason |
| `interrupt` | Available; §14's bounded observation never depended on it |
| Bounded worker context | Concurrency is permitted, but half the objective (§1) is unavailable and the report says so |
| Capacity report | Assume one |
| Recursive termination | Reconciliation step 3 (§13.1) requires an explicit resource check rather than trusting `finished` |
| Confinement | Automatic retry (§13.1) and staging (§9) remain unavailable. No host currently asserts it |
| Tool restriction | Never assumed. The threat model in §6.3 relies on none |

Cross-host parity is a repository requirement, not an aspiration [C13], and shipped scripts must run
stdlib-only on stock macOS Python 3.9.6 [C14] — which a file-based store satisfies with nothing but
`json`, `os` and `hashlib`. That is part of why it goes first.

**[UNENFORCED]** Nothing in this repository can inspect a host's agent configuration, so a
misconfigured worker fails at dispatch rather than at launch.

**Takeover behaviour is part of the adapter contract.** An adapter that cannot let a new coordinator
`observe` a handle the previous one recorded turns every takeover into a set of `UNCERTAIN` attempts.
Such an adapter must declare it, and a project on that host runs at capacity one.

## 18. What this design does not enforce

Collected so that no reader has to infer it. Each appears marked **[UNENFORCED]** where it is stated.

| # | Rule | Why code cannot enforce it | Where |
|---|---|---|---|
| U1 | A worker writes only within its resource grant | Prompt text plus, at best, host tool restriction; the filesystem is shared in the modes that share it | §10 |
| U2 | A resource grant is the task's complete touch set | An undeclared write is invisible to an inference over declarations, and post-task comparison is diagnostics, not attribution | §10 |
| U3 | A derived resource set covers what the task touches | Derivation reads declarations and a verification string; it cannot read the work | §6.5 |
| U4 | A capture was not tampered with | The coordinator reads a file, in a location the worker may be able to write | §11.2 |
| U5 | Attempt identity isolates filesystem writes | It rejects stale messages only; a shell with file access bypasses the protocol | §6.3 |
| U6 | A heartbeat means progress | No deadline distinguishes useful work from a loop | §13.1 |
| U7 | A superseded coordinator cannot write | Ownership is a check in the supported path, not a capability restriction | §12.1 |
| U8 | A worker's agent type exposes what the protocol needs | Host agent configuration is outside a plugin's reach | §17 |
| U9 | Two projects sharing a target directory do not collide | The target claim binds coordinators running this protocol, not pre-existing sequential ones | §8.5 |
| U10 | A receipt's semantics are validated by existing tooling | `_validate_evidence_reference` checks shape and existence only [C22] | §7.2 |

## 19. Implementation stages

Ordered so that each stage is testable before the next, and so that nothing behavioural ships until
everything under it is verified.

| Stage | Changes | Exit condition |
|---|---|---|
| 1. **Specify** | This document and its decision record | Every state in §6.1 is left by a §6.2 row, and every §6.2 row has a §13 outcome |
| 2. **Measure** | The graph-shape script and its fixtures, committed; the timing harness | §2's figures reproduce from the committed fixtures |
| 3. **Execution core** | `execution_lib.py`: admission, the conflict matrix, the contract object and its hash, derivation, adequacy comparison | Deterministic tests pass with no host and no subagent, including §8.3's two-task scenario |
| 4. **Durable handoff** | `execution_store.py`: record schemas and version gate, the publication protocol, receipts, acknowledgements, idempotent evidence import, retention | Restart tests preserve both pending and committed work; receipt validation is explicit, not inherited [C22] |
| 5. **Capture** | The capture helper, reusing the existing process path, and the `method`/`actor`/`capture` model | A failed, stale or inadequate check cannot become accepted success |
| 6. **Host wiring** | Per-host adapters implementing §17, the capability checks, the target claim, coordinator ownership and takeover | Each host demonstrates parallel operation, an inline executor, or a documented refusal |
| 7. **Enable the skill** | The worker contract, the coordinator loop, report changes, the `schema_version` gate, aligned release versions across all three manifests and `uv.lock` | End-to-end scenarios pass on every affected host surface |
| 8. **Reassess infrastructure** | Benchmark context, duration, integration overhead and cost; only then reconsider a database or an MCP transport against the recorded conditions | A demonstrated need, or the stage closes with no change |

Stage 5 reuses the existing process-capture path, and that reuse is a change rather than a call:
`record_evidence` runs the command outside any lock [C23] and raises `WorkspaceError` on a launch
error or timeout **before** appending evidence [C24], and it buffers captured output in memory. The
capture helper must instead **persist a capture for every outcome** (§5.2), bound the output it holds
and record `truncated`, decode explicitly and record `decode_error` rather than raising, and state
what it does about descendant processes — Python documents `timeout` handling for the direct child,
which is not a guarantee about an arbitrary descendant tree [C25]. Terminating a process group is a
host concern (§17), not a subprocess flag.

Constraints that bind every stage: new behaviour in focused modules rather than a substantially larger
`workspace_lib.py`, reached through the existing launcher conventions; stdlib-only on Python 3.9
[C14]; public functions typed while project state stays `dict[str, Any]`; malformed JSON producing
findings or `WorkspaceError` rather than a traceback; canonical commits keeping their revision check
and `project.json` commit point; post-commit index rebuilding still going through
`_rebuild_index_after_commit` [C10]; new tests under `tests/plugins/research/project/`; the 100%
coverage gate over the scripts package [C15] [C26]; and skill activation strictly after implementation
and host validation.

**Stage 7 is the one that changes behaviour, and it is the last activation prerequisite.** Until it
lands, an agent may not spawn subagents unprompted, so the skill text asking for them is what makes
this design in-policy rather than an interesting document. Stage 2 comes early on purpose: if
efficiency is the objective, the instrumentation that measures it should exist before the thing it
measures.

## 20. Verification and benchmark plan

### 20.1 The tests worth writing

Failures and interleavings, not happy paths:

- **Every row of §6.2 and every row of §13 resumes correctly.** This is the highest-value block in the
  suite, and the two tables exist in that shape so the tests can be enumerated from them.
- The §8.3 two-task verification scenario: `A` and `B` with disjoint writes and repository-wide
  checks are admitted in sequence, never concurrently, and never deadlock.
- A crash between the canonical `RUNNING` commit and the host call leaves the attempt `UNCERTAIN` and
  launches nothing (T6); with a launch key it resolves without a second worker (T7).
- A crash midway through publication is read as incomplete, never as failure (T10), and a second
  differing publication is rejected rather than applied (T11).
- A late result from an `UNCERTAIN` attempt is adopted when nothing replaced it (T18) and quarantined
  when something did (T19).
- Adequacy: a capture for `true`, a capture for the wrong command, and a passing capture followed by an
  artifact modification are all rejected (§11.4).
- Simultaneous reservations cannot exceed capacity or acquire conflicting resources; two `read` claims
  on one path are admitted together; a `read` and a `write` are not.
- Path aliasing: a symlinked directory, a case-differing spelling on a case-insensitive volume, a
  missing leaf, and a directory-versus-child claim are each detected.
- A protected path in a grant is rejected at admission.
- A sibling revision change leaves an attempt valid; a change to the executing task's own contract does
  not; a withdrawn authorization quarantines it even though the hash still matches.
- An unknown record major pauses dispatch and never re-dispatches.
- A superseded coordinator's mutation fails the ownership check; a takeover walks §13 before
  dispatching; attempt generations survive it.
- Retention: a receipt named by canonical evidence survives closure; an unreferenced capture may be
  collected and its collection is recorded.
- A worker failure pauses dispatch while successful siblings remain recordable, and the project stays
  `EXECUTING` until running work is reconciled [C11]. A crash between the failure and the pause write
  still pauses on the next start (§14.4).
- Old project records and immutable terminal history are unchanged.
- Each host executes a small independent pair, a dependency join, and a failure-and-restart scenario.

Use an **injected clock** and controlled process synchronization rather than sleeps, and assert
observable invariants rather than mirroring helper implementations.

### 20.2 The benchmark

Two of this document's claims are unmeasured — that context economy is the larger benefit (§1), and
that lock contention under concurrency is material — and §2's figures are reproducible in
principle but not yet reproducible in this repository. So, as Stage 2 commitments:

- The graph-shape script and its fixtures **will be committed** with the figures. They are not in this
  branch, and no sentence here should be read as saying they are.
- Coordinator context consumption will be measured **separately** from total model usage.
- Worker startup, verification, integration, retries and end-to-end completion will be recorded
  individually, so overhead is visible rather than absorbed.
- Capacity one will be compared against larger capacities **under identical task contracts and
  verification requirements**. A comparison that lets the parallel run skip work is not a comparison.

## 21. Citations

Every claim above about current behaviour cites a row here. Each row names a file, a line range, and
text that must appear within that range; `tests/plugins/research/test_parallel_execution_doc.py`
re-reads each range rather than trusting the table.

| # | Source | Expected text within range |
|---|---|---|
| C1 | `plugins/research/skills/project/scripts/workspace_lib.py:1346-1349` | `does not match RUNNING tasks` |
| C2 | `plugins/research/skills/project/scripts/workspace_lib.py:2769-2776` | `Kahn's algorithm` |
| C3 | `plugins/research/skills/project/scripts/workspace_lib.py:2791-2793` | `def build_task_graph` |
| C4 | `plugins/research/skills/project/scripts/workspace_lib.py:2687-2688` | `def levels` |
| C5 | `plugins/research/skills/project/SKILL.md:181-185` | `Workers must not edit canonical state.` |
| C6 | `plugins/research/skills/project/references/workspace-schema.md:11-14` | `sole writer of` |
| C7 | `plugins/research/skills/project/SKILL.md:384-385` | `Use a dependency graph only when independent work can run in parallel` |
| C8 | `plugins/research/skills/project/scripts/workspace_lib.py:319-326` | `A cross-process lock based on atomic directory creation` |
| C9 | `plugins/research/skills/project/scripts/workspace_lib.py:296-316` | `os.fsync(handle.fileno())` |
| C10 | `plugins/research/skills/project/scripts/workspace_lib.py:3226-3229` | `a failed index rebuild must not read as a failed commit` |
| C11 | `plugins/research/skills/project/scripts/workspace_lib.py:1362-1365` | `project cannot have RUNNING tasks` |
| C12 | `plugins/research/skills/project/references/workspace-schema.md:57` | `Command, inspection, or review` |
| C13 | `AGENTS.md:43-45` | `must work in Claude Code, Codex, and Kimi Code` |
| C14 | `AGENTS.md:73-76` | `stdlib-only` |
| C15 | `pyproject.toml:66` | `fail_under = 100` |
| C16 | `plugins/research/skills/project/SKILL.md:39-41` | `authorization immediately before` |
| C17 | `plugins/research/skills/project/SKILL.md:430-434` | `explicit, current, and scoped to the exact action` |
| C18 | `plugins/research/skills/project/scripts/workspace_lib.py:2612-2615` | `holds no start, no end, and no duration` |
| C19 | `plugins/research/skills/project/scripts/workspace_lib.py:82-96` | `TASK_FIELDS = {` |
| C20 | `plugins/research/skills/project/scripts/workspace_lib.py:1266-1267` | `_unexpected_fields(task, TASK_FIELDS` |
| C21 | `plugins/research/skills/project/scripts/workspace_lib.py:2244-2248` | `unsupported schema_version` |
| C22 | `plugins/research/skills/project/scripts/workspace_lib.py:487-491` | `evidence file does not exist` |
| C23 | `plugins/research/skills/project/scripts/workspace_lib.py:2542-2551` | `completed = subprocess.run(` |
| C24 | `plugins/research/skills/project/scripts/workspace_lib.py:2584-2596` | `must not see a half-written one` |
| C25 | `plugins/research/skills/project/scripts/workspace_lib.py:2552-2560` | `TimeoutExpired` |
| C26 | `pyproject.toml:49-53` | `--cov=plugins/research/skills/project/scripts` |
